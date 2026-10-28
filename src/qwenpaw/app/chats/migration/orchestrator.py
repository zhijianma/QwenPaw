# -*- coding: utf-8 -*-
"""Recoverable, single-flight migration from legacy Chat persistence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from uuid import uuid4

from ..integrity import build_transcript_manifest
from ..storage.common import connect_database, immediate_transaction
from ..storage.conversation import validate_conversation_database
from ..storage.conversation_repository import (
    MaterializationResult,
    materialize_normalized_transcript,
    read_transcript,
)
from ..storage.paths import ChatStoragePaths
from ..storage.projection import (
    enqueue_projection_repair,
    project_conversation,
)
from ..storage.sequence import reserve_recall_sequences
from .normalize import (
    LegacyNormalizationError,
    NormalizedTranscript,
    normalize_legacy_snapshot,
)
from .snapshot import (
    LegacySnapshotError,
    LegacySnapshotManifest,
    create_legacy_snapshot,
)


class LegacyMigrationStateError(RuntimeError):
    """The durable migration job cannot safely enter the requested state."""


@dataclass(frozen=True)
class LegacyMigrationResult:
    """Published storage identity and transcript counts."""

    conversation_id: str
    turns: int
    messages: int
    blocks: int


@dataclass(frozen=True)
class _MigrationJob:
    conversation_id: str
    agent_id: str
    owner_scope: str
    lifecycle: str
    source_fingerprint: str
    state: str
    session_id: str
    sources: tuple[str, ...]
    created_at_us: int


PhaseHook = Callable[[str], None]
logger = logging.getLogger(__name__)


def _now_us() -> int:
    return time.time_ns() // 1_000


def _error_digest(exc: BaseException) -> str:
    payload = f"{type(exc).__name__}:{exc}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load_job(paths: ChatStoragePaths, conversation_id: str) -> _MigrationJob:
    with connect_database(paths.catalog, readonly=True) as connection:
        row = connection.execute(
            "SELECT e.conversation_id, e.agent_id, e.owner_scope, "
            "e.lifecycle, e.created_at_us, m.source_fingerprint, m.state, "
            "m.progress_json FROM chat_catalog_entries AS e "
            "JOIN chat_migrations AS m "
            "ON m.conversation_id = e.conversation_id "
            "WHERE e.conversation_id = ?",
            (conversation_id,),
        ).fetchone()
    if row is None:
        raise LegacyMigrationStateError(
            f"Legacy migration job does not exist: {conversation_id}",
        )
    progress = json.loads(str(row["progress_json"]))
    session_id = progress.get("session_id")
    sources = progress.get("sources")
    if not isinstance(session_id, str) or not isinstance(sources, list):
        raise LegacyMigrationStateError(
            f"Legacy migration inputs are invalid: {conversation_id}",
        )
    if not all(isinstance(source, str) for source in sources):
        raise LegacyMigrationStateError(
            f"Legacy migration sources are invalid: {conversation_id}",
        )
    return _MigrationJob(
        conversation_id=str(row["conversation_id"]),
        agent_id=str(row["agent_id"]),
        owner_scope=str(row["owner_scope"]),
        lifecycle=str(row["lifecycle"]),
        source_fingerprint=str(row["source_fingerprint"] or ""),
        state=str(row["state"]),
        session_id=session_id,
        sources=tuple(sources),
        created_at_us=int(row["created_at_us"]),
    )


def _transition(
    paths: ChatStoragePaths,
    conversation_id: str,
    *,
    state: str,
    now_us: int,
    lease_owner: str | None,
    error_digest: str | None = None,
    increment_attempt: bool = False,
) -> None:
    with connect_database(paths.catalog) as connection:
        with immediate_transaction(connection):
            cursor = connection.execute(
                "UPDATE chat_migrations SET state = ?, "
                "attempt = attempt + ?, lease_owner = ?, "
                "lease_expires_at_us = ?, error_digest = ?, "
                "updated_at_us = ? WHERE conversation_id = ?",
                (
                    state,
                    int(increment_attempt),
                    lease_owner,
                    now_us + 60_000_000 if lease_owner else None,
                    error_digest,
                    now_us,
                    conversation_id,
                ),
            )
            if cursor.rowcount != 1:
                raise LegacyMigrationStateError(
                    f"Legacy migration job disappeared: {conversation_id}",
                )


def _snapshot_fingerprint(manifest: LegacySnapshotManifest) -> str:
    return "|".join(
        f"{item.relative_path}:{item.size}:{item.mtime_ns}"
        for item in sorted(
            manifest.files,
            key=lambda value: value.relative_path,
        )
    )


def _allocated_sequence_count(normalized: NormalizedTranscript) -> int:
    allocated = 0
    legacy: set[int] = set()
    for message in normalized.messages:
        metadata = message.metadata or {}
        legacy_sequence = metadata.get("legacy_recall_seq")
        for block_index, _block in enumerate(message.content):
            if block_index == 0 and legacy_sequence is not None:
                sequence = int(legacy_sequence)
                if sequence <= 0 or sequence in legacy:
                    raise LegacyMigrationStateError(
                        "Legacy recall sequence is invalid or duplicated",
                    )
                legacy.add(sequence)
            else:
                allocated += 1
    return allocated


def _prepare_database_for_move(path: Path) -> None:
    with connect_database(path) as connection:
        checkpoint = connection.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)",
        ).fetchone()
        if checkpoint is None or int(checkpoint[0]) != 0:
            raise LegacyMigrationStateError(
                f"Could not checkpoint migrated Chat database: {path}",
            )
        mode = str(
            connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0],
        ).lower()
        if mode != "delete":
            raise LegacyMigrationStateError(
                f"Could not detach migrated Chat WAL: {path}",
            )
    for suffix in ("-wal", "-shm"):
        if path.with_name(f"{path.name}{suffix}").exists():
            raise LegacyMigrationStateError(
                f"Migrated Chat retained SQLite sidecar: {path}",
            )


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_temporary(path: Path) -> None:
    for candidate in (
        path,
        path.with_name(f"{path.name}-wal"),
        path.with_name(f"{path.name}-shm"),
    ):
        candidate.unlink(missing_ok=True)


def _published_result(
    path: Path,
    *,
    conversation_id: str,
    agent_id: str,
    owner_scope: str,
    backend: str,
    session_id: str,
    snapshot_sha256: str | None = None,
) -> LegacyMigrationResult:
    validate_conversation_database(
        path,
        expected_conversation_id=conversation_id,
        expected_agent_id=agent_id,
        expected_owner_scope=owner_scope,
        expected_backend=backend,
    )
    with connect_database(path, readonly=True) as connection:
        row = connection.execute(
            "SELECT COUNT(DISTINCT t.turn_id) AS turns, "
            "COUNT(DISTINCT m.message_id) AS messages, "
            "COUNT(DISTINCT k.block_id) AS blocks "
            "FROM chat_turns AS t "
            "LEFT JOIN chat_messages AS m ON m.turn_id = t.turn_id "
            "LEFT JOIN chat_blocks AS k ON k.message_id = m.message_id",
        ).fetchone()
        migration = connection.execute(
            "SELECT source_locator, source_fingerprint, "
            "transcript_manifest_json FROM chat_migration_record "
            "WHERE singleton_id = 1",
        ).fetchone()
    if row is None or migration is None:
        raise LegacyMigrationStateError(
            f"Published Chat is missing migration proof: {conversation_id}",
        )
    if str(migration["source_locator"]) != f"legacy:{session_id}":
        raise LegacyMigrationStateError(
            f"Published Chat migration source does not match: "
            f"{conversation_id}",
        )
    if (
        snapshot_sha256 is not None
        and str(migration["source_fingerprint"]) != snapshot_sha256
    ):
        raise LegacyMigrationStateError(
            f"Published Chat migration snapshot does not match: "
            f"{conversation_id}",
        )
    stored_manifest = build_transcript_manifest(
        read_transcript(path),
    )
    stored_manifest_json = json.dumps(
        stored_manifest.to_dict(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if stored_manifest_json != str(migration["transcript_manifest_json"]):
        raise LegacyMigrationStateError(
            f"Published Chat transcript proof does not match: "
            f"{conversation_id}",
        )
    return LegacyMigrationResult(
        conversation_id=conversation_id,
        turns=int(row["turns"]),
        messages=int(row["messages"]),
        blocks=int(row["blocks"]),
    )


def _publish_catalog(
    paths: ChatStoragePaths,
    job: _MigrationJob,
    *,
    now_us: int,
) -> None:
    with connect_database(paths.catalog) as connection:
        with immediate_transaction(connection):
            connection.execute(
                "UPDATE chat_catalog_entries SET lifecycle = 'ready', "
                "source_generation = 1, "
                "updated_at_us = ? WHERE conversation_id = ?",
                (now_us, job.conversation_id),
            )
            connection.execute(
                "UPDATE chat_alias_index SET source_generation = 1, "
                "updated_at_us = ? "
                "WHERE conversation_id = ?",
                (now_us, job.conversation_id),
            )
            connection.execute(
                "UPDATE chat_migrations SET state = 'ready_v2', "
                "lease_owner = NULL, lease_expires_at_us = NULL, "
                "error_digest = NULL, updated_at_us = ? "
                "WHERE conversation_id = ?",
                (now_us, job.conversation_id),
            )


def _project_after_publish(
    paths: ChatStoragePaths,
    conversation_id: str,
    *,
    now_us: int,
) -> None:
    """Project a published migration without weakening source durability."""
    try:
        project_conversation(
            paths,
            conversation_id,
            now_us=now_us,
        )
    except Exception:
        try:
            enqueue_projection_repair(
                paths,
                conversation_id,
                expected_generation=1,
                now_us=now_us,
            )
        except Exception:
            logger.exception(
                "Chat projection repair could not be queued: %s",
                conversation_id,
            )
        logger.warning(
            "Chat projection deferred after legacy migration: %s",
            conversation_id,
            exc_info=True,
        )


def _publish_and_project(
    paths: ChatStoragePaths,
    job: _MigrationJob,
) -> None:
    publish_time_us = _now_us()
    _publish_catalog(paths, job, now_us=publish_time_us)
    _project_after_publish(
        paths,
        job.conversation_id,
        now_us=publish_time_us,
    )


def _migrate_sync(
    workspace: Path,
    conversation_id: str,
    *,
    backend: str,
    phase_hook: PhaseHook | None,
) -> LegacyMigrationResult:
    paths = ChatStoragePaths(workspace)
    job = _load_job(paths, conversation_id)
    if job.state == "failed_terminal":
        raise LegacyMigrationStateError(
            f"Legacy migration is terminal: {conversation_id}",
        )
    target = paths.conversation(conversation_id)
    lease_owner = uuid4().hex
    now_us = _now_us()
    try:
        if (
            job.state == "ready_v2"
            and job.lifecycle == "ready"
            and target.is_file()
        ):
            result = _published_result(
                target,
                conversation_id=conversation_id,
                agent_id=job.agent_id,
                owner_scope=job.owner_scope,
                backend=backend,
                session_id=job.session_id,
            )
            return result
        _transition(
            paths,
            conversation_id,
            state="snapshotting",
            now_us=now_us,
            lease_owner=lease_owner,
            increment_attempt=True,
        )
        snapshot = create_legacy_snapshot(
            workspace,
            conversation_id=conversation_id,
            session_id=job.session_id,
            source_paths=list(job.sources),
        )
        if _snapshot_fingerprint(snapshot) != job.source_fingerprint:
            raise LegacySnapshotError(
                "Legacy sources changed after inventory; rescan is required",
            )
        if phase_hook:
            phase_hook("snapshot")
        if target.is_file():
            result = _published_result(
                target,
                conversation_id=conversation_id,
                agent_id=job.agent_id,
                owner_scope=job.owner_scope,
                backend=backend,
                session_id=job.session_id,
                snapshot_sha256=snapshot.manifest_sha256,
            )
            if phase_hook:
                phase_hook("catalog_publish")
            _publish_and_project(paths, job)
            return result
        _transition(
            paths,
            conversation_id,
            state="normalizing",
            now_us=_now_us(),
            lease_owner=lease_owner,
        )
        normalized = normalize_legacy_snapshot(workspace, snapshot)
        if phase_hook:
            phase_hook("normalize")
        _transition(
            paths,
            conversation_id,
            state="validating",
            now_us=_now_us(),
            lease_owner=lease_owner,
        )
        sequence_lease = reserve_recall_sequences(
            paths.catalog,
            workspace_id=str(workspace.resolve()),
            count=_allocated_sequence_count(normalized),
            high_water_floor=snapshot.history_max_seq or 0,
            lease_owner=lease_owner,
            now_us=_now_us(),
        )
        temporary = target.with_name(f".{target.name}.migrating")
        _remove_temporary(temporary)
        materialized: MaterializationResult = (
            materialize_normalized_transcript(
                temporary,
                conversation_id=conversation_id,
                agent_id=job.agent_id,
                owner_scope=job.owner_scope,
                backend=backend,
                normalized=normalized,
                snapshot=snapshot,
                now_us=job.created_at_us,
                allocated_recall_sequences=(
                    sequence_lease.values() if sequence_lease else ()
                ),
            )
        )
        validate_conversation_database(
            temporary,
            expected_conversation_id=conversation_id,
            expected_agent_id=job.agent_id,
            expected_owner_scope=job.owner_scope,
            expected_backend=backend,
        )
        if phase_hook:
            phase_hook("validate")
        _prepare_database_for_move(temporary)
        _fsync_file(temporary)
        if phase_hook:
            phase_hook("rename")
        os.replace(temporary, target)
        _fsync_directory(target.parent)
        result = LegacyMigrationResult(
            conversation_id=conversation_id,
            turns=materialized.turns,
            messages=materialized.messages,
            blocks=materialized.blocks,
        )
        _published_result(
            target,
            conversation_id=conversation_id,
            agent_id=job.agent_id,
            owner_scope=job.owner_scope,
            backend=backend,
            session_id=job.session_id,
            snapshot_sha256=snapshot.manifest_sha256,
        )
        if phase_hook:
            phase_hook("catalog_publish")
        _publish_and_project(paths, job)
        return result
    except BaseException as exc:
        terminal = isinstance(exc, LegacyNormalizationError)
        _transition(
            paths,
            conversation_id,
            state="failed_terminal" if terminal else "failed_retryable",
            now_us=_now_us(),
            lease_owner=None,
            error_digest=_error_digest(exc),
        )
        raise


class LegacyMigrationCoordinator:
    """Serialize migration callers per conversation within one process."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        self._guard = asyncio.Lock()
        self._tasks: dict[str, asyncio.Task[LegacyMigrationResult]] = {}

    async def migrate(
        self,
        conversation_id: str,
        *,
        backend: str = "qwenpaw",
        phase_hook: PhaseHook | None = None,
    ) -> LegacyMigrationResult:
        """Run or join one recoverable migration attempt."""
        async with self._guard:
            task = self._tasks.get(conversation_id)
            if task is None:
                task = asyncio.create_task(
                    asyncio.to_thread(
                        _migrate_sync,
                        self._workspace,
                        conversation_id,
                        backend=backend,
                        phase_hook=phase_hook,
                    ),
                )
                self._tasks[conversation_id] = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                async with self._guard:
                    if self._tasks.get(conversation_id) is task:
                        self._tasks.pop(conversation_id, None)
