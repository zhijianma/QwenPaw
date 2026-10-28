# -*- coding: utf-8 -*-
"""Consistent, portable snapshots for normalized Chat SQLite storage."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from .catalog import CATALOG_SCHEMA_VERSION, validate_catalog_database
from .common import ChatStorageSchemaError, connect_database
from .conversation import (
    CONVERSATION_SCHEMA_VERSION,
    validate_conversation_database,
)
from .paths import ChatStoragePaths


CHAT_STORAGE_MANIFEST = "chat-storage-manifest.json"
CHAT_STORAGE_BACKUP_FORMAT = "qwenpaw-chat-storage.v1"
_BACKUP_TIMEOUT_SECONDS = 60.0
_MAX_INVENTORY_ATTEMPTS = 3
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


@dataclass(frozen=True)
class ChatStorageSnapshot:
    """Validated workspace snapshot ready for archive or restore."""

    root: Path
    manifest_path: Path
    conversation_ids: tuple[str, ...]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_online_backup(
    source: Path,
    destination: Path,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> Path:
    """Create one verified SQLite snapshot without copying live sidecars."""
    source = source.expanduser().resolve()
    destination = destination.expanduser()
    if not source.is_file():
        raise FileNotFoundError(source)
    if destination.resolve() == source:
        raise ValueError("Chat backup destination must differ from source")
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _BACKUP_TIMEOUT_SECONDS

    def _progress(_status: int, _remaining: int, _total: int) -> None:
        if check_cancelled is not None:
            check_cancelled()
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Chat snapshot timed out: {source}")

    _progress(0, 0, 0)
    origin = sqlite3.connect(
        f"{source.as_uri()}?mode=ro",
        uri=True,
        timeout=0.1,
    )
    snapshot: sqlite3.Connection | None = None
    completed = False
    try:
        origin.execute("PRAGMA busy_timeout = 100")
        snapshot = sqlite3.connect(destination)
        origin.backup(
            snapshot,
            pages=256,
            progress=_progress,
            sleep=0.05,
        )
        snapshot.commit()
        mode = str(
            snapshot.execute("PRAGMA journal_mode = DELETE").fetchone()[0],
        ).lower()
        if mode != "delete":
            raise ChatStorageSchemaError(
                f"Chat snapshot retained journal mode: {mode}",
            )
        row = snapshot.execute("PRAGMA quick_check").fetchone()
        if row is None or str(row[0]) != "ok":
            raise ChatStorageSchemaError(
                "Chat snapshot failed its integrity check",
            )
        if snapshot.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ChatStorageSchemaError(
                "Chat snapshot contains dangling foreign keys",
            )
        completed = True
    finally:
        if snapshot is not None:
            snapshot.close()
        origin.close()
        if not completed:
            destination.unlink(missing_ok=True)
        for suffix in _SQLITE_SIDECAR_SUFFIXES:
            sidecar = Path(f"{destination}{suffix}")
            if sidecar.exists():
                sidecar.unlink()
    return destination


def _catalog_inventory(path: Path) -> tuple[tuple[str, str], ...]:
    validate_catalog_database(path)
    with connect_database(path, readonly=True) as connection:
        rows = connection.execute(
            "SELECT conversation_id, physical_relpath FROM "
            "chat_catalog_entries WHERE lifecycle = 'ready' "
            "ORDER BY conversation_id",
        ).fetchall()
    inventory: list[tuple[str, str]] = []
    for row in rows:
        conversation_id = str(row["conversation_id"])
        try:
            canonical = str(UUID(conversation_id))
        except ValueError as exc:
            raise ChatStorageSchemaError(
                f"Chat catalog contains an invalid conversation ID: "
                f"{conversation_id}",
            ) from exc
        if canonical != conversation_id:
            raise ChatStorageSchemaError(
                f"Chat catalog contains a non-canonical conversation ID: "
                f"{conversation_id}",
            )
        expected = f"conversations/{conversation_id}.db"
        physical_relpath = str(row["physical_relpath"])
        if physical_relpath != expected:
            raise ChatStorageSchemaError(
                "Chat catalog contains a non-canonical conversation path: "
                f"{physical_relpath}",
            )
        inventory.append((conversation_id, physical_relpath))
    return tuple(inventory)


def _conversation_manifest(
    path: Path,
    *,
    relative_path: str,
    conversation_id: str,
) -> dict[str, Any]:
    validate_conversation_database(
        path,
        expected_conversation_id=conversation_id,
    )
    with connect_database(path, readonly=True) as connection:
        row = connection.execute(
            "SELECT agent_id, owner_scope, backend, source_generation "
            "FROM chat_meta WHERE singleton_id = 1",
        ).fetchone()
    if row is None:
        raise ChatStorageSchemaError("Chat snapshot has no identity row")
    return {
        "path": relative_path,
        "kind": "conversation",
        "schema_version": CONVERSATION_SCHEMA_VERSION,
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
        "derived": False,
        "conversation_id": conversation_id,
        "agent_id": str(row["agent_id"]),
        "owner_scope": str(row["owner_scope"]),
        "backend": str(row["backend"]),
        "source_generation": int(row["source_generation"]),
    }


def _write_manifest(
    paths: ChatStoragePaths,
    inventory: tuple[tuple[str, str], ...],
) -> Path:
    files: list[dict[str, Any]] = [
        {
            "path": "chat_catalog.db",
            "kind": "catalog",
            "schema_version": CATALOG_SCHEMA_VERSION,
            "bytes": paths.catalog.stat().st_size,
            "sha256": _sha256(paths.catalog),
            "derived": False,
        },
    ]
    for conversation_id, physical_relpath in inventory:
        files.append(
            _conversation_manifest(
                paths.root / physical_relpath,
                relative_path=physical_relpath,
                conversation_id=conversation_id,
            ),
        )
    manifest = {
        "format": CHAT_STORAGE_BACKUP_FORMAT,
        "files": files,
        "excluded_derived": ["chat_recall.db"],
    }
    manifest_path = paths.root / CHAT_STORAGE_MANIFEST
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
        newline="\n",
    )
    return manifest_path


def snapshot_workspace_chat_storage(
    workspace: Path,
    destination: Path,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> ChatStorageSnapshot | None:
    """Snapshot catalog and all ready conversations into a workspace tree.

    Inventory drift causes a bounded retry before any archive bytes are
    emitted. Runtime projections may advance between database snapshots, but
    they are rebuilt from authoritative conversation databases on restore.
    """
    source_paths = ChatStoragePaths(workspace)
    if not source_paths.catalog.is_file():
        return None
    target_paths = ChatStoragePaths(destination)
    for attempt in range(_MAX_INVENTORY_ATTEMPTS):
        if check_cancelled is not None:
            check_cancelled()
        shutil.rmtree(target_paths.root, ignore_errors=True)
        target_paths.ensure_layout()
        initial = _catalog_inventory(source_paths.catalog)
        try:
            for _conversation_id, physical_relpath in initial:
                sqlite_online_backup(
                    source_paths.root / physical_relpath,
                    target_paths.root / physical_relpath,
                    check_cancelled=check_cancelled,
                )
            sqlite_online_backup(
                source_paths.catalog,
                target_paths.catalog,
                check_cancelled=check_cancelled,
            )
            captured = _catalog_inventory(target_paths.catalog)
            if captured != initial:
                continue
            manifest_path = _write_manifest(target_paths, captured)
            validate_workspace_chat_storage(destination)
            return ChatStorageSnapshot(
                root=target_paths.root,
                manifest_path=manifest_path,
                conversation_ids=tuple(item[0] for item in captured),
            )
        except (FileNotFoundError, sqlite3.Error):
            if attempt + 1 >= _MAX_INVENTORY_ATTEMPTS:
                raise
    raise ChatStorageSchemaError(
        "Chat inventory changed during every backup attempt",
    )


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ChatStorageSchemaError(
            f"Chat backup manifest is invalid: {path}",
        ) from exc
    if not isinstance(value, dict):
        raise ChatStorageSchemaError("Chat backup manifest must be an object")
    if value.get("format") != CHAT_STORAGE_BACKUP_FORMAT:
        raise ChatStorageSchemaError("Unsupported Chat backup manifest format")
    if not isinstance(value.get("files"), list):
        raise ChatStorageSchemaError("Chat backup manifest has no file list")
    return value


def _validate_manifest_files(
    paths: ChatStoragePaths,
    records: list[Any],
    expected_paths: set[str],
) -> dict[str, dict[str, Any]]:
    """Validate the bounded artifact list and return records by path."""
    by_path: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            raise ChatStorageSchemaError(
                "Chat backup manifest contains an invalid file record",
            )
        relative_path = record.get("path")
        if not isinstance(relative_path, str):
            raise ChatStorageSchemaError(
                "Chat backup manifest file path is invalid",
            )
        artifact = (paths.root / relative_path).resolve()
        if (
            not artifact.is_relative_to(paths.root.resolve())
            or not artifact.is_file()
        ):
            raise ChatStorageSchemaError(
                f"Chat backup artifact is missing: {relative_path}",
            )
        if relative_path in by_path:
            raise ChatStorageSchemaError(
                f"Duplicate Chat backup artifact: {relative_path}",
            )
        by_path[relative_path] = record
        if artifact.stat().st_size != record.get("bytes"):
            raise ChatStorageSchemaError(
                f"Chat backup artifact size mismatch: {relative_path}",
            )
        if _sha256(artifact) != record.get("sha256"):
            raise ChatStorageSchemaError(
                f"Chat backup artifact hash mismatch: {relative_path}",
            )
    if set(by_path) != expected_paths:
        raise ChatStorageSchemaError(
            "Chat backup manifest does not match catalog inventory",
        )
    return by_path


def _validate_conversation_record(
    paths: ChatStoragePaths,
    conversation_id: str,
    relative_path: str,
    record: dict[str, Any],
) -> None:
    """Validate one conversation's manifest and physical identity."""
    if (
        record.get("kind") != "conversation"
        or record.get("conversation_id") != conversation_id
        or record.get("schema_version") != CONVERSATION_SCHEMA_VERSION
    ):
        raise ChatStorageSchemaError(
            f"Chat conversation manifest contract failed: {conversation_id}",
        )
    database = paths.root / relative_path
    validate_conversation_database(
        database,
        expected_conversation_id=conversation_id,
        expected_agent_id=str(record.get("agent_id")),
        expected_owner_scope=str(record.get("owner_scope")),
        expected_backend=str(record.get("backend")),
    )
    with connect_database(database, readonly=True) as connection:
        row = connection.execute(
            "SELECT source_generation FROM chat_meta "
            "WHERE singleton_id = 1",
        ).fetchone()
    generation = int(row[0]) if row is not None else -1
    if generation != record.get("source_generation"):
        raise ChatStorageSchemaError(
            f"Chat conversation generation mismatch: {conversation_id}",
        )


def validate_workspace_chat_storage(
    workspace: Path,
    *,
    require_manifest: bool = True,
) -> ChatStorageSnapshot | None:
    """Validate a staged normalized Chat workspace before publication."""
    paths = ChatStoragePaths(workspace)
    if not paths.catalog.is_file():
        if (paths.root / CHAT_STORAGE_MANIFEST).exists():
            raise ChatStorageSchemaError(
                "Chat backup manifest exists without its catalog",
            )
        return None
    inventory = _catalog_inventory(paths.catalog)
    manifest_path = paths.root / CHAT_STORAGE_MANIFEST
    if require_manifest and not manifest_path.is_file():
        raise ChatStorageSchemaError("Chat backup manifest is missing")
    if not manifest_path.is_file():
        for conversation_id, relative_path in inventory:
            validate_conversation_database(
                paths.root / relative_path,
                expected_conversation_id=conversation_id,
            )
        return ChatStorageSnapshot(
            root=paths.root,
            manifest_path=manifest_path,
            conversation_ids=tuple(item[0] for item in inventory),
        )

    manifest = _load_manifest(manifest_path)
    records = manifest["files"]
    expected_paths = {
        "chat_catalog.db",
        *(relative_path for _, relative_path in inventory),
    }
    by_path = _validate_manifest_files(paths, records, expected_paths)
    catalog_record = by_path["chat_catalog.db"]
    if (
        catalog_record.get("kind") != "catalog"
        or catalog_record.get(
            "schema_version",
        )
        != CATALOG_SCHEMA_VERSION
    ):
        raise ChatStorageSchemaError("Chat catalog manifest contract failed")
    for conversation_id, relative_path in inventory:
        _validate_conversation_record(
            paths,
            conversation_id,
            relative_path,
            by_path[relative_path],
        )
    return ChatStorageSnapshot(
        root=paths.root,
        manifest_path=manifest_path,
        conversation_ids=tuple(item[0] for item in inventory),
    )


def is_live_chat_sqlite_file(workspace: Path, path: Path) -> bool:
    """Return whether regular archive traversal must skip one live file."""
    paths = ChatStoragePaths(workspace)
    candidate = path.resolve()
    database_paths = {paths.catalog.resolve(), paths.recall.resolve()}
    try:
        relative = candidate.relative_to(paths.conversations.resolve())
    except ValueError:
        relative = None
    if relative is not None and len(relative.parts) == 1:
        name = relative.name
        if name.endswith((".db", ".db-wal", ".db-shm", ".db-journal")):
            return True
    for database in database_paths:
        if candidate == database:
            return True
        if any(
            candidate == Path(f"{database}{suffix}")
            for suffix in _SQLITE_SIDECAR_SUFFIXES
        ):
            return True
    return False
