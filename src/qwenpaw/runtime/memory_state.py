# -*- coding: utf-8 -*-
"""Lite SQLite adapter for provider-scoped Memory state."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import JsonValue

from ..kernel.memory import (
    MemoryStateConflictError,
    MemoryStateScope,
    MemoryStateSnapshot,
)

_MAX_MEMORY_STATE_BYTES = 256 * 1024
_PREPARE_LOCKS_GUARD = threading.Lock()
_PREPARE_LOCKS: dict[Path, threading.Lock] = {}


def _database_prepare_lock(database_path: Path) -> threading.Lock:
    """Return one process-wide initialization lock per database file."""
    resolved_path = database_path.expanduser().resolve()
    with _PREPARE_LOCKS_GUARD:
        lock = _PREPARE_LOCKS.get(resolved_path)
        if lock is None:
            lock = threading.Lock()
            _PREPARE_LOCKS[resolved_path] = lock
        return lock


class SQLiteMemoryStateStore:
    """One provider/owner view over a shared Workspace state database."""

    def __init__(
        self,
        database_path: Path,
        *,
        provider_id: str,
        scope: MemoryStateScope,
        owner_id: str,
    ) -> None:
        self._database_path = Path(database_path)
        self._provider_id = provider_id
        self._scope = scope
        self._owner_id = owner_id
        self._prepare_lock = asyncio.Lock()
        self._prepared = False

    async def read(self, key: str) -> MemoryStateSnapshot | None:
        """Return one current value without exposing other namespaces."""
        self._validate_key(key)
        await self._prepare()
        row = await asyncio.to_thread(self._read_sync, key)
        return self._snapshot(row, key) if row is not None else None

    async def write(
        self,
        key: str,
        value: JsonValue,
        *,
        expected_revision: int,
    ) -> MemoryStateSnapshot:
        """Create or update one value using compare-and-swap semantics."""
        self._validate_key(key)
        if expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")
        payload = self._encode_value(value)
        await self._prepare()
        row = await asyncio.to_thread(
            self._write_sync,
            key,
            payload,
            expected_revision,
        )
        return self._snapshot(row, key)

    async def delete(self, key: str, *, expected_revision: int) -> None:
        """Delete one exact observed revision."""
        self._validate_key(key)
        if expected_revision < 1:
            raise ValueError("delete expected_revision must be positive")
        await self._prepare()
        await asyncio.to_thread(
            self._delete_sync,
            key,
            expected_revision,
        )

    async def _prepare(self) -> None:
        if self._prepared:
            return
        async with self._prepare_lock:
            if self._prepared:
                return
            await asyncio.to_thread(self._prepare_sync)
            self._prepared = True

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database_path,
            timeout=10.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _prepare_sync(self) -> None:
        with _database_prepare_lock(self._database_path):
            self._database_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memory_provider_state (
                        provider_id TEXT NOT NULL,
                        scope TEXT NOT NULL,
                        owner_id TEXT NOT NULL,
                        state_key TEXT NOT NULL,
                        value_json TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (
                            provider_id,
                            scope,
                            owner_id,
                            state_key
                        )
                    )
                    """,
                )

    def _read_sync(self, key: str) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT value_json, revision, updated_at
                FROM memory_provider_state
                WHERE provider_id = ? AND scope = ?
                  AND owner_id = ? AND state_key = ?
                """,
                self._identity(key),
            ).fetchone()

    def _write_sync(
        self,
        key: str,
        value_json: str,
        expected_revision: int,
    ) -> sqlite3.Row:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """
                SELECT revision FROM memory_provider_state
                WHERE provider_id = ? AND scope = ?
                  AND owner_id = ? AND state_key = ?
                """,
                self._identity(key),
            ).fetchone()
            current_revision = int(current["revision"]) if current else 0
            if current_revision != expected_revision:
                connection.rollback()
                raise MemoryStateConflictError(
                    f"memory state revision is {current_revision}, expected "
                    f"{expected_revision}",
                )
            revision = current_revision + 1
            updated_at = datetime.now(timezone.utc).isoformat()
            connection.execute(
                """
                INSERT INTO memory_provider_state (
                    provider_id, scope, owner_id, state_key,
                    value_json, revision, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider_id, scope, owner_id, state_key)
                DO UPDATE SET
                    value_json = excluded.value_json,
                    revision = excluded.revision,
                    updated_at = excluded.updated_at
                """,
                (*self._identity(key), value_json, revision, updated_at),
            )
            connection.commit()
            row = connection.execute(
                """
                SELECT value_json, revision, updated_at
                FROM memory_provider_state
                WHERE provider_id = ? AND scope = ?
                  AND owner_id = ? AND state_key = ?
                """,
                self._identity(key),
            ).fetchone()
        assert row is not None
        return row

    def _delete_sync(self, key: str, expected_revision: int) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """
                SELECT revision FROM memory_provider_state
                WHERE provider_id = ? AND scope = ?
                  AND owner_id = ? AND state_key = ?
                """,
                self._identity(key),
            ).fetchone()
            current_revision = int(current["revision"]) if current else 0
            if current_revision != expected_revision:
                connection.rollback()
                raise MemoryStateConflictError(
                    f"memory state revision is {current_revision}, expected "
                    f"{expected_revision}",
                )
            connection.execute(
                """
                DELETE FROM memory_provider_state
                WHERE provider_id = ? AND scope = ?
                  AND owner_id = ? AND state_key = ?
                """,
                self._identity(key),
            )
            connection.commit()

    def _identity(self, key: str) -> tuple[str, str, str, str]:
        return (
            self._provider_id,
            self._scope.value,
            self._owner_id,
            key,
        )

    def _snapshot(
        self,
        row: sqlite3.Row,
        key: str,
    ) -> MemoryStateSnapshot:
        return MemoryStateSnapshot(
            provider_id=self._provider_id,
            scope=self._scope,
            owner_id=self._owner_id,
            key=key,
            value=json.loads(str(row["value_json"])),
            revision=int(row["revision"]),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    @staticmethod
    def _validate_key(key: str) -> None:
        if not isinstance(key, str) or not key.strip() or len(key) > 200:
            raise ValueError("memory state key must contain 1 to 200 chars")

    @staticmethod
    def _encode_value(value: Any) -> str:
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as error:
            raise ValueError("memory state value must be JSON") from error
        if len(encoded.encode("utf-8")) > _MAX_MEMORY_STATE_BYTES:
            raise ValueError("memory state value exceeds 256 KiB")
        return encoded


__all__ = ["SQLiteMemoryStateStore"]
