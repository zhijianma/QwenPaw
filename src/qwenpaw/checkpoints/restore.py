# -*- coding: utf-8 -*-
"""Transactional restore orchestration for checkpoints."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from ..utils.io_utils import run_sync_io
from ..app.chats.storage import (
    ChatStoragePaths,
    interrupt_nonterminal_work,
    materialize_normalized_transcript,
    project_conversation,
    reserve_recall_sequences,
)
from ..app.chats.storage.common import (
    connect_database,
    immediate_transaction,
)

from .chat_artifact import (
    CHAT_ARTIFACT_DATABASE,
    CHAT_ARTIFACT_MANIFEST,
    ChatCheckpointManifest,
    create_chat_checkpoint_artifact,
    parse_chat_checkpoint_manifest,
    replace_chat_database,
    validate_chat_checkpoint_blob,
)
from ..app.chats.migration import (
    create_legacy_snapshot,
    normalize_legacy_snapshot,
)
from .policy import is_qwenpaw_state_path
from .policy import session_file_path, session_key
from .models import (
    CheckpointEntry,
    CheckpointError,
    RestorePlan,
    RestoreResult,
)

if TYPE_CHECKING:
    from .service import CheckpointService

logger = logging.getLogger("qwenpaw.checkpoints")


@dataclass(frozen=True)
class _PreparedRestore:
    entry: CheckpointEntry
    previous_head: str | None
    plan: RestorePlan
    conversation_blob: bytes | None
    touched: frozenset[str]
    current_tree: str | None = None
    chat_manifest: ChatCheckpointManifest | None = None
    chat_database_blob: bytes | None = None


def _changed_paths(
    repository,
    *,
    target_commit: str,
    current_commit: str | None,
) -> set[str]:
    """Return paths changed between two checkpoint trees."""
    if not current_commit or current_commit == target_commit:
        return set()
    output = repository.run_git(
        "diff-tree",
        "--name-status",
        "-r",
        "--no-commit-id",
        "-M",
        target_commit,
        current_commit,
    )
    paths: set[str] = set()
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            paths.add(parts[-1])
        if parts and parts[0].startswith("R") and len(parts) >= 3:
            paths.add(parts[1])
    return paths


class WorkspaceMutationGuard:
    """Pause cooperative workspace writers for one restore transaction."""

    def __init__(self, workspace, *, timeout: float) -> None:
        self.workspace = workspace
        self.timeout = timeout

    async def quiesce(self) -> list[Callable[[], None]]:
        """Pause cron and wait until tracked workspace tasks are idle."""
        resume_callbacks = self._pause_workspace_cron()
        try:
            await self._wait_workspace_idle()
        except BaseException:
            self.resume(resume_callbacks)
            raise
        return resume_callbacks

    def _pause_workspace_cron(self) -> list[Callable[[], None]]:
        resume_callbacks: list[Callable[[], None]] = []
        cron_executor = getattr(self.workspace, "cron_executor", None)
        if cron_executor is not None and hasattr(cron_executor, "pause"):
            try:
                cron_executor.pause()
                if hasattr(cron_executor, "resume"):
                    resume_callbacks.append(cron_executor.resume)
            except Exception as exc:
                raise CheckpointError(
                    "Checkpoint restore was cancelled because cron could "
                    "not be paused.",
                ) from exc
            return resume_callbacks

        cron_manager = getattr(self.workspace, "cron_manager", None)
        scheduler = getattr(cron_manager, "_scheduler", None)
        if scheduler is not None and hasattr(scheduler, "pause"):
            try:
                scheduler.pause()
                if hasattr(scheduler, "resume"):
                    resume_callbacks.append(scheduler.resume)
            except Exception as exc:
                raise CheckpointError(
                    "Checkpoint restore was cancelled because the cron "
                    "scheduler could not be paused.",
                ) from exc
        return resume_callbacks

    async def _wait_workspace_idle(self) -> None:
        task_tracker = getattr(self.workspace, "task_tracker", None)
        if task_tracker is not None and hasattr(
            task_tracker,
            "has_active_tasks_excluding",
        ):
            try:
                async with asyncio.timeout(self.timeout):
                    await self._wait_for_other_tasks(
                        task_tracker,
                        asyncio.current_task(),
                    )
            except TimeoutError as exc:
                raise CheckpointError(
                    "Checkpoint restore was cancelled because workspace "
                    f"tasks did not become idle within {self.timeout:.1f}s.",
                ) from exc
            except Exception as exc:
                raise CheckpointError(
                    "Checkpoint restore was cancelled because active tasks "
                    "could not be inspected.",
                ) from exc
        elif task_tracker is not None and hasattr(
            task_tracker,
            "wait_all_idle",
        ):
            try:
                await asyncio.wait_for(
                    task_tracker.wait_all_idle(),
                    timeout=self.timeout,
                )
            except asyncio.TimeoutError as exc:
                raise CheckpointError(
                    "Checkpoint restore was cancelled because workspace "
                    f"tasks did not become idle within {self.timeout:.1f}s.",
                ) from exc
            except Exception as exc:
                raise CheckpointError(
                    "Checkpoint restore was cancelled because workspace "
                    "idle state could not be verified.",
                ) from exc
        elif task_tracker is not None and hasattr(
            task_tracker,
            "list_active_tasks",
        ):
            try:
                await asyncio.wait_for(
                    self._wait_for_no_active_tasks(task_tracker),
                    timeout=self.timeout,
                )
            except asyncio.TimeoutError as exc:
                raise CheckpointError(
                    "Checkpoint restore was cancelled because workspace "
                    f"tasks did not become idle within {self.timeout:.1f}s.",
                ) from exc
            except Exception as exc:
                raise CheckpointError(
                    "Checkpoint restore was cancelled because active tasks "
                    "could not be inspected.",
                ) from exc

    @staticmethod
    async def _wait_for_other_tasks(
        task_tracker,
        restore_task: asyncio.Task | None,
    ) -> None:
        while await task_tracker.has_active_tasks_excluding(restore_task):
            await asyncio.sleep(0.5)

    @staticmethod
    async def _wait_for_no_active_tasks(task_tracker) -> None:
        while True:
            active = await task_tracker.list_active_tasks()
            if not active:
                return
            await asyncio.sleep(0.5)

    @staticmethod
    def resume(callbacks: list[Callable[[], None]]) -> None:
        """Resume paused writers in reverse acquisition order."""
        for callback in reversed(callbacks):
            try:
                callback()
            except Exception:
                logger.debug("Failed to resume cron", exc_info=True)


class RestoreService:
    """Plan, apply, and roll back checkpoint restores."""

    def __init__(self, service: "CheckpointService") -> None:
        self.service = service
        self.repository = service.repository

    async def restore(
        self,
        *,
        target: str | None,
        session_id: str,
        user_id: str,
        channel: str,
        dry_run: bool = False,
    ) -> RestoreResult:
        """Restore only the current conversation session file."""
        if not target:
            raise CheckpointError(
                "Usage: /checkpoint restore <N | snap_name | sha> "
                "[--dry-run | --confirm]",
            )
        return await self._run_restore(
            target=target,
            session_id=session_id,
            user_id=user_id,
            channel=channel,
            dry_run=dry_run,
        )

    async def restore_with_memory(
        self,
        *,
        target: str | None,
        session_id: str,
        user_id: str,
        channel: str,
        dry_run: bool = False,
    ) -> RestoreResult:
        """Restore conversation + MEMORY.md + memory/ to a checkpoint."""
        if not target:
            raise CheckpointError(
                "Usage: /checkpoint restore <target> --include-memory "
                "--confirm",
            )
        return await self._run_restore(
            target=target,
            session_id=session_id,
            user_id=user_id,
            channel=channel,
            include_memory=True,
            dry_run=dry_run,
        )

    async def restore_with_files(
        self,
        *,
        target: str | None,
        session_id: str,
        user_id: str,
        channel: str,
        include_memory: bool = False,
        selected_files: tuple[str, ...] | None = None,
        dry_run: bool = False,
    ) -> RestoreResult:
        """Restore conversation and workspace files to a checkpoint tree."""
        if not target:
            raise CheckpointError(
                "Usage: /checkpoint restore <target> --include-files "
                "--confirm",
            )
        if not dry_run and selected_files is None:
            raise CheckpointError(
                "Applying workspace-file restore requires an explicit "
                "`--files` selection.",
            )
        return await self._run_restore(
            target=target,
            session_id=session_id,
            user_id=user_id,
            channel=channel,
            include_memory=include_memory,
            include_files=True,
            selected_files=selected_files,
            dry_run=dry_run,
        )

    async def _run_restore(
        self,
        *,
        target: str,
        session_id: str,
        user_id: str,
        channel: str,
        include_memory: bool = False,
        include_files: bool = False,
        selected_files: tuple[str, ...] | None = None,
        dry_run: bool = False,
    ) -> RestoreResult:
        """Run one validated restore transaction."""
        service = self.service
        conv_rel = self._conversation_rel(
            session_id=session_id,
            user_id=user_id,
            channel=channel,
        )
        skey = session_key(
            channel=channel,
            user_id=user_id,
            session_id=session_id,
        )
        if dry_run:
            conversation_id = service.resolve_chat_conversation_id(
                session_id=session_id,
                user_id=user_id,
                channel=channel,
            )
        else:
            conversation_id = await service.prepare_chat_artifact(
                session_id=session_id,
                user_id=user_id,
                channel=channel,
            )
        memory = (
            await run_sync_io(self._memory_restorer)
            if include_memory
            else None
        )
        mutation_guard = (
            self._workspace_mutation_guard() if not dry_run else None
        )
        prepared: _PreparedRestore | None = None
        pre_ref: str | None = None
        resume_callbacks: list[Callable[[], None]] = []
        async with service.maintenance_lock:
            if not dry_run:
                service.query_gate.clear()
            try:
                async with service.lock:
                    if mutation_guard is not None:
                        resume_callbacks = await mutation_guard.quiesce()
                    prepared = await run_sync_io(
                        self._prepare_restore,
                        target=target,
                        session_id=session_id,
                        user_id=user_id,
                        channel=channel,
                        session_key_str=skey,
                        conversation_path=conv_rel,
                        include_memory=include_memory,
                        include_files=include_files,
                        selected_files=selected_files,
                        memory=memory,
                        conversation_id=conversation_id,
                    )
                    if dry_run:
                        return self._result_from_plan(
                            prepared.plan,
                            dry_run=True,
                        )

                    if include_files:
                        description = f"Before file restore to {target}"
                    elif include_memory:
                        description = f"Before memory restore to {target}"
                    else:
                        description = f"Before restore to {target}"

                    pre_ref = await run_sync_io(
                        self._apply_restore_transaction_sync,
                        prepared,
                        session_id=session_id,
                        user_id=user_id,
                        channel=channel,
                        session_key_str=skey,
                        conversation_path=conv_rel,
                        include_files=include_files,
                        description=description,
                        memory=memory,
                    )
                    if prepared.chat_manifest is not None:
                        await self._invalidate_chat_live(
                            prepared.chat_manifest.conversation_id,
                        )
            finally:
                if mutation_guard is not None:
                    mutation_guard.resume(resume_callbacks)
                if not dry_run:
                    service.query_gate.set()

        assert prepared is not None
        return self._result_from_plan(
            prepared.plan,
            dry_run=False,
            pre_restore_ref=pre_ref,
        )

    def _apply_restore_transaction_sync(
        self,
        prepared: _PreparedRestore,
        *,
        session_id: str,
        user_id: str,
        channel: str,
        session_key_str: str,
        conversation_path: str,
        include_files: bool,
        description: str,
        memory: "MemoryRestorer | None",
    ) -> str:
        """Apply and, on failure, roll back all repository mutations."""
        pre_snapshot = None
        try:
            pre_snapshot = self.service.create_snapshot_unlocked(
                "pre-restore",
                session_id,
                user_id,
                channel,
                None,
                description,
                None,
                tree_override=(
                    prepared.current_tree if include_files else None
                ),
                conversation_id=(
                    prepared.chat_manifest.conversation_id
                    if prepared.chat_manifest is not None
                    else None
                ),
            )
            if prepared.conversation_blob is not None:
                self.repository.restore_internal_paths(
                    {conversation_path: prepared.conversation_blob},
                )
            if (
                prepared.chat_manifest is not None
                and prepared.chat_database_blob is not None
            ):
                self._restore_chat_database(
                    prepared.chat_manifest,
                    prepared.chat_database_blob,
                )
            if include_files:
                self.repository.restore_tree_paths(
                    prepared.entry.commit,
                    set(prepared.touched),
                )
            if memory is not None:
                memory.mutation_started = True
                memory.restore_sync(prepared.entry.commit)
            self.repository.set_session_head(
                session_key_str,
                prepared.entry.commit,
            )
            return pre_snapshot.ref
        except Exception as exc:
            if pre_snapshot is not None:
                self._rollback_restore_transaction_sync(
                    original=exc,
                    pre_commit=pre_snapshot.commit,
                    conversation_path=conversation_path,
                    file_paths=set(prepared.touched),
                    include_memory=(
                        memory is not None and memory.mutation_started
                    ),
                    session_key_str=session_key_str,
                    previous_head=prepared.previous_head,
                    memory=memory,
                    conversation_id=(
                        prepared.chat_manifest.conversation_id
                        if prepared.chat_manifest is not None
                        else None
                    ),
                )
            raise

    def _rollback_restore_transaction_sync(
        self,
        *,
        original: BaseException,
        pre_commit: str,
        conversation_path: str,
        file_paths: set[str],
        include_memory: bool,
        session_key_str: str,
        previous_head: str | None,
        memory: "MemoryRestorer | None",
        conversation_id: str | None,
    ) -> None:
        try:
            if self.repository.tree_has_blob(
                pre_commit,
                conversation_path,
            ):
                conversation = self.repository.read_blob(
                    pre_commit,
                    conversation_path,
                )
                self.repository.restore_internal_paths(
                    {conversation_path: conversation},
                )
            if conversation_id is not None:
                self._restore_chat_artifact_from_commit(
                    pre_commit,
                    conversation_id,
                )
            self.repository.restore_tree_paths(pre_commit, file_paths)
            if include_memory and memory is not None:
                memory.restore_sync(pre_commit)
            if previous_head is None:
                self.repository.remove_session_heads({session_key_str})
            else:
                self.repository.set_session_head(
                    session_key_str,
                    previous_head,
                )
        except Exception as rollback_exc:
            logger.exception("Checkpoint restore rollback failed")
            raise CheckpointError(
                "Restore failed and rollback to the pre-restore checkpoint "
                "also failed; inspect the pre-restore ref manually.",
            ) from rollback_exc
        logger.info(
            "Rolled back failed checkpoint restore after error: %s",
            original,
        )

    def _prepare_restore(
        self,
        *,
        target: str,
        session_id: str,
        user_id: str,
        channel: str,
        session_key_str: str,
        conversation_path: str,
        include_memory: bool,
        include_files: bool,
        selected_files: tuple[str, ...] | None,
        memory: "MemoryRestorer | None",
        conversation_id: str | None,
    ) -> _PreparedRestore:
        entry = self.service.resolve_target(
            target,
            session_id,
            user_id,
            channel,
        )
        previous_head = self.service.session_head(session_key_str)
        touched: set[str] = set()
        current_tree: str | None = None
        if include_files:
            current_tree = self.service.write_snapshot_tree(
                conversation_id,
            )
            assert current_tree is not None
            touched = self._file_restore_candidates(
                target_commit=entry.commit,
                current_tree=current_tree,
                conv_rel=conversation_path,
                selected_files=selected_files,
            )
        conversation = (
            self.repository.read_blob(entry.commit, conversation_path)
            if self.repository.tree_has_blob(
                entry.commit,
                conversation_path,
            )
            else None
        )
        chat_manifest, chat_database = self._read_chat_artifact(
            entry.commit,
            conversation_id,
        )
        if (
            chat_manifest is None
            and conversation_id is not None
            and conversation is not None
        ):
            chat_manifest, chat_database = self._legacy_blob_to_chat_artifact(
                conversation,
                conversation_id=conversation_id,
                session_id=session_id,
                conversation_path=conversation_path,
            )
        plan = self._build_plan(
            target=target,
            commit=entry.commit,
            conversation_path=conversation_path,
            touched=touched,
            include_memory=include_memory,
            include_files=include_files,
            memory=memory,
            chat_manifest=chat_manifest,
        )
        if conversation is None and chat_manifest is None:
            raise CheckpointError(
                "Checkpoint contains no restorable conversation data",
            )
        return _PreparedRestore(
            entry=entry,
            previous_head=previous_head,
            plan=plan,
            conversation_blob=conversation,
            touched=frozenset(touched),
            current_tree=current_tree,
            chat_manifest=chat_manifest,
            chat_database_blob=chat_database,
        )

    def _build_plan(
        self,
        *,
        target: str,
        commit: str,
        conversation_path: str,
        touched: set[str],
        include_memory: bool,
        include_files: bool,
        memory: "MemoryRestorer | None",
        chat_manifest: ChatCheckpointManifest | None,
    ) -> RestorePlan:
        restored: list[str] = []
        deleted: list[str] = []
        if include_files:
            restored, deleted = self.repository.plan_tree_restore(
                commit,
                touched,
            )
        if memory is not None:
            memory_restore, memory_delete = memory.plan(commit)
            restored.extend(memory_restore)
            deleted.extend(memory_delete)
        return RestorePlan(
            target=target,
            commit=commit,
            conversation_path=conversation_path,
            restore_paths=tuple(restored),
            delete_paths=tuple(deleted),
            file_paths=tuple(sorted(touched)) if include_files else (),
            include_memory=include_memory,
            include_files=include_files,
            conversation_format=(
                "normalized_chat_v2"
                if chat_manifest is not None
                else "legacy_session_json"
            ),
            conversation_id=(
                chat_manifest.conversation_id
                if chat_manifest is not None
                else None
            ),
            head_turn_id=(
                chat_manifest.head_turn_id
                if chat_manifest is not None
                else None
            ),
        )

    @staticmethod
    def _result_from_plan(
        plan: RestorePlan,
        *,
        dry_run: bool,
        pre_restore_ref: str | None = None,
    ) -> RestoreResult:
        return RestoreResult(
            target=plan.target,
            commit=plan.commit,
            restored_paths=(
                plan.conversation_path,
                *plan.restore_paths,
            ),
            pre_restore_ref=pre_restore_ref,
            dry_run=dry_run,
            include_memory=plan.include_memory,
            include_files=plan.include_files,
            deleted_paths=plan.delete_paths,
            file_paths=plan.file_paths,
            conversation_format=plan.conversation_format,
            conversation_id=plan.conversation_id,
            head_turn_id=plan.head_turn_id,
            view_invalidated=(
                not dry_run and plan.conversation_id is not None
            ),
        )

    def _read_chat_artifact(
        self,
        commit: str,
        conversation_id: str | None,
    ) -> tuple[ChatCheckpointManifest | None, bytes | None]:
        has_database = self.repository.tree_has_blob(
            commit,
            CHAT_ARTIFACT_DATABASE,
        )
        has_manifest = self.repository.tree_has_blob(
            commit,
            CHAT_ARTIFACT_MANIFEST,
        )
        if has_database != has_manifest:
            raise CheckpointError("Chat checkpoint artifact is incomplete")
        if not has_database:
            return None, None
        if conversation_id is None:
            raise CheckpointError(
                "Normalized Chat checkpoint has no current Chat mapping",
            )
        manifest = parse_chat_checkpoint_manifest(
            self.repository.read_blob(commit, CHAT_ARTIFACT_MANIFEST),
        )
        database = self.repository.read_blob(
            commit,
            CHAT_ARTIFACT_DATABASE,
        )
        validate_chat_checkpoint_blob(
            database,
            manifest,
            expected_conversation_id=conversation_id,
        )
        return manifest, database

    def _restore_chat_artifact_from_commit(
        self,
        commit: str,
        conversation_id: str,
    ) -> None:
        manifest, database = self._read_chat_artifact(
            commit,
            conversation_id,
        )
        if manifest is None or database is None:
            raise CheckpointError(
                "Safety checkpoint has no normalized Chat artifact",
            )
        self._restore_chat_database(manifest, database)

    def _restore_chat_database(
        self,
        manifest: ChatCheckpointManifest,
        database: bytes,
    ) -> None:
        workspace = self.service.workspace
        application = getattr(workspace, "chat_application", None)
        if application is None:
            raise CheckpointError("Normalized Chat service is unavailable")
        paths = ChatStoragePaths(self.service.workspace_dir)
        now_us = time.time_ns() // 1_000
        replace_chat_database(
            paths.conversation(manifest.conversation_id),
            database,
        )
        self._validate_alias_conflicts(
            paths,
            manifest.conversation_id,
        )
        self._assign_recall_sequences(
            paths,
            manifest.conversation_id,
            now_us=now_us,
        )
        self._rebase_restored_chat(
            paths,
            manifest.conversation_id,
            now_us=now_us,
        )
        interrupt_nonterminal_work(
            paths.conversation(manifest.conversation_id),
            conversation_id=manifest.conversation_id,
            now_us=now_us,
        )
        self._clear_derived_chat_rows(paths, manifest.conversation_id)
        project_conversation(
            paths,
            manifest.conversation_id,
            now_us=now_us,
        )

    def _legacy_blob_to_chat_artifact(
        self,
        blob: bytes,
        *,
        conversation_id: str,
        session_id: str,
        conversation_path: str,
    ) -> tuple[ChatCheckpointManifest, bytes]:
        paths = ChatStoragePaths(self.service.workspace_dir)
        with connect_database(
            paths.conversation(conversation_id),
            readonly=True,
        ) as source:
            identity = source.execute(
                "SELECT agent_id, owner_scope, backend, created_at_us "
                "FROM chat_meta WHERE singleton_id = 1",
            ).fetchone()
        if identity is None:
            raise CheckpointError("Current Chat identity is missing")
        with tempfile.TemporaryDirectory(
            prefix="qwenpaw-checkpoint-legacy-",
        ) as temporary:
            workspace = Path(temporary) / "workspace"
            relative = PurePosixPath(conversation_path)
            if not relative.parts or relative.parts[0] != "sessions":
                raise CheckpointError(
                    "Legacy checkpoint session path is invalid",
                )
            session_relative = PurePosixPath(*relative.parts[1:])
            session_file = workspace / "sessions" / session_relative
            session_file.parent.mkdir(parents=True, exist_ok=True)
            session_file.write_bytes(blob)
            snapshot = create_legacy_snapshot(
                workspace,
                conversation_id=conversation_id,
                session_id=session_id,
                source_paths=[session_relative.as_posix()],
            )
            normalized = normalize_legacy_snapshot(workspace, snapshot)
            database = workspace / "materialized.db"
            materialize_normalized_transcript(
                database,
                conversation_id=conversation_id,
                agent_id=str(identity["agent_id"]),
                owner_scope=str(identity["owner_scope"]),
                backend=str(identity["backend"]),
                normalized=normalized,
                snapshot=snapshot,
                now_us=int(identity["created_at_us"]),
            )
            artifact_dir = workspace / "artifact"
            (
                artifact,
                _manifest_path,
                manifest,
            ) = create_chat_checkpoint_artifact(
                database,
                artifact_dir,
                conversation_id=conversation_id,
            )
            return manifest, artifact.read_bytes()

    @staticmethod
    def _assign_recall_sequences(
        paths: ChatStoragePaths,
        conversation_id: str,
        *,
        now_us: int,
    ) -> None:
        path = paths.conversation(conversation_id)
        with connect_database(path, readonly=True) as source:
            block_ids = [
                str(row["block_id"])
                for row in source.execute(
                    "SELECT block_id FROM chat_blocks "
                    "WHERE recall_seq IS NULL "
                    "ORDER BY created_at_us, block_id",
                )
            ]
        lease = reserve_recall_sequences(
            paths.catalog,
            workspace_id=str(paths.workspace.resolve()),
            count=len(block_ids),
            lease_owner="checkpoint-restore",
            now_us=now_us,
        )
        if lease is None:
            return
        with connect_database(path) as source:
            with immediate_transaction(source):
                source.executemany(
                    "UPDATE chat_blocks SET recall_seq = ? "
                    "WHERE block_id = ? AND recall_seq IS NULL",
                    zip(lease.values(), block_ids),
                )

    @staticmethod
    def _validate_alias_conflicts(
        paths: ChatStoragePaths,
        conversation_id: str,
    ) -> None:
        with connect_database(
            paths.conversation(conversation_id),
            readonly=True,
        ) as source:
            aliases = source.execute(
                "SELECT alias_namespace, external_key, owner_scope "
                "FROM chat_aliases",
            ).fetchall()
        with connect_database(paths.catalog, readonly=True) as catalog:
            agent = catalog.execute(
                "SELECT agent_id FROM chat_catalog_entries "
                "WHERE conversation_id = ? AND lifecycle = 'ready'",
                (conversation_id,),
            ).fetchone()
            if agent is None:
                raise CheckpointError("Ready Chat catalog entry is missing")
            for alias in aliases:
                conflict = catalog.execute(
                    "SELECT conversation_id FROM chat_alias_index "
                    "WHERE agent_id = ? AND alias_namespace = ? "
                    "AND external_key = ? AND owner_scope = ?",
                    (
                        agent["agent_id"],
                        alias["alias_namespace"],
                        alias["external_key"],
                        alias["owner_scope"],
                    ),
                ).fetchone()
                if (
                    conflict is not None
                    and str(conflict["conversation_id"]) != conversation_id
                ):
                    raise CheckpointError(
                        "Chat checkpoint alias belongs to another Chat",
                    )

    @staticmethod
    def _rebase_restored_chat(
        paths: ChatStoragePaths,
        conversation_id: str,
        *,
        now_us: int,
    ) -> None:
        with connect_database(paths.catalog, readonly=True) as catalog:
            row = catalog.execute(
                "SELECT source_generation FROM chat_catalog_entries "
                "WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        catalog_generation = int(row["source_generation"]) if row else 0
        with connect_database(
            paths.conversation(conversation_id),
        ) as source:
            with immediate_transaction(source):
                meta = source.execute(
                    "SELECT source_generation, write_seq "
                    "FROM chat_meta WHERE singleton_id = 1",
                ).fetchone()
                generation = (
                    max(
                        catalog_generation,
                        int(meta["source_generation"]),
                    )
                    + 1
                )
                write_seq = int(meta["write_seq"]) + 1
                source.execute(
                    "UPDATE chat_meta SET source_generation = ?, "
                    "write_seq = ?, updated_at_us = ? "
                    "WHERE singleton_id = 1",
                    (generation, write_seq, now_us),
                )
                source.execute(
                    "INSERT INTO chat_projection_outbox("
                    "projection, operation, source_generation, "
                    "payload_json, created_at_us, updated_at_us) VALUES "
                    "('catalog', 'checkpoint_restored', ?, ?, ?, ?), "
                    "('recall', 'checkpoint_restored', ?, ?, ?, ?)",
                    (
                        generation,
                        json.dumps(
                            {"conversation_id": conversation_id},
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        now_us,
                        now_us,
                        generation,
                        "{}",
                        now_us,
                        now_us,
                    ),
                )

    @staticmethod
    def _clear_derived_chat_rows(
        paths: ChatStoragePaths,
        conversation_id: str,
    ) -> None:
        with connect_database(paths.catalog) as catalog:
            with immediate_transaction(catalog):
                catalog.execute(
                    "DELETE FROM chat_alias_index "
                    "WHERE conversation_id = ?",
                    (conversation_id,),
                )
                catalog.execute(
                    "DELETE FROM chat_runtime_intents "
                    "WHERE conversation_id = ?",
                    (conversation_id,),
                )

    async def _invalidate_chat_live(self, conversation_id: str) -> None:
        workspace = self.service.workspace
        tracker = getattr(workspace, "task_tracker", None)
        hub = getattr(tracker, "chat_live_hub", None)
        if hub is not None:
            await hub.invalidate_conversation(conversation_id)

    def _file_restore_candidates(
        self,
        *,
        target_commit: str,
        current_tree: str,
        conv_rel: str,
        selected_files: tuple[str, ...] | None,
    ) -> set[str]:
        touched = _changed_paths(
            self.repository,
            target_commit=target_commit,
            current_commit=current_tree,
        )
        candidates = {
            rel
            for rel in touched
            if self._is_file_restore_candidate(rel, conv_rel=conv_rel)
        }
        if selected_files is None:
            return candidates

        selected = {
            self._normalize_selected_file(rel, conv_rel=conv_rel)
            for rel in selected_files
        }
        unavailable = selected - candidates
        if unavailable:
            rendered = ", ".join(f"`{rel}`" for rel in sorted(unavailable))
            raise CheckpointError(
                "Selected file(s) are not changed between the target "
                f"checkpoint and the current workspace: {rendered}.",
            )
        return selected

    @classmethod
    def _normalize_selected_file(cls, rel: str, *, conv_rel: str) -> str:
        normalized = (rel or "").strip().replace("\\", "/")
        path = PurePosixPath(normalized)
        if (
            not normalized
            or path.is_absolute()
            or ".." in path.parts
            or (path.parts and path.parts[0].endswith(":"))
        ):
            raise CheckpointError(
                f"`--files` path must be workspace-relative: `{rel}`.",
            )
        normalized = path.as_posix().removeprefix("./")
        if not cls._is_file_restore_candidate(normalized, conv_rel=conv_rel):
            raise CheckpointError(
                f"`--files` cannot restore QwenPaw state path `{rel}`.",
            )
        return normalized

    def _conversation_rel(
        self,
        *,
        session_id: str,
        user_id: str,
        channel: str,
    ) -> str:
        conv_path = session_file_path(
            self.service.workspace_dir,
            session_id=session_id,
            user_id=user_id,
            channel=channel,
        )
        return conv_path.relative_to(self.service.workspace_dir).as_posix()

    @staticmethod
    def _is_file_restore_candidate(rel: str, *, conv_rel: str) -> bool:
        if not rel or rel == conv_rel:
            return False
        if rel.startswith("sessions/"):
            return False
        if rel == "MEMORY.md" or rel.startswith("memory/"):
            return False
        if is_qwenpaw_state_path(rel):
            return False
        return True

    def _memory_restorer(self) -> MemoryRestorer:
        return MemoryRestorer(repository=self.repository)

    def _workspace_mutation_guard(self) -> WorkspaceMutationGuard:
        return WorkspaceMutationGuard(
            workspace=self.service.workspace,
            timeout=self.service.memory_quiesce_timeout,
        )


class MemoryRestorer:
    """Restore ``MEMORY.md`` and ``memory/`` from a checkpoint tree."""

    def __init__(
        self,
        *,
        repository,
    ) -> None:
        self.repository = repository
        self.workspace_dir = repository.workspace_dir
        self.mutation_started = False

    def plan(self, commit: str) -> tuple[list[str], list[str]]:
        """Return only memory files whose contents would actually change."""
        target_paths = self._checkpoint_paths(commit)
        current_paths = self._current_memory_paths()
        return self.repository.plan_tree_restore(
            commit,
            current_paths | set(target_paths),
        )

    def restore_sync(self, commit: str) -> tuple[list[str], list[str]]:
        target_paths = self._checkpoint_paths(commit)
        current_paths = self._current_memory_paths()
        restored, deleted = self.repository.restore_tree_paths(
            commit,
            current_paths | set(target_paths),
        )
        self._remove_empty_memory_dirs()
        return restored, deleted

    def _checkpoint_paths(self, commit: str) -> list[str]:
        """List checkpoint memory files without loading their contents."""
        return self.repository.list_tree_paths(
            commit,
            "MEMORY.md",
            "memory/",
        )

    def _current_memory_paths(self) -> set[str]:
        paths: set[str] = set()
        memory_md = self.workspace_dir / "MEMORY.md"
        memory_dir = self.workspace_dir / "memory"
        if memory_md.is_file() or memory_md.is_symlink():
            paths.add("MEMORY.md")
        if memory_dir.exists():
            for root, dirs, files in os.walk(memory_dir):
                for dirname in list(dirs):
                    path = Path(root, dirname)
                    if path.is_symlink():
                        paths.add(
                            path.relative_to(self.workspace_dir).as_posix(),
                        )
                        dirs.remove(dirname)
                for fname in files:
                    paths.add(
                        Path(root, fname)
                        .relative_to(
                            self.workspace_dir,
                        )
                        .as_posix(),
                    )
        return paths

    def _remove_empty_memory_dirs(self) -> None:
        memory_dir = self.workspace_dir / "memory"
        if not memory_dir.is_dir():
            return
        for root, _dirs, _files in os.walk(memory_dir, topdown=False):
            path = Path(root)
            try:
                path.rmdir()
            except OSError:
                pass
