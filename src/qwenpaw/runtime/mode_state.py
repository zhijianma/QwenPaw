# -*- coding: utf-8 -*-
"""Lite SQLite adapter for namespaced Agent Mode state."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
from pathlib import Path

from ..kernel.mode_state import (
    AgentModeState,
    AgentModeStateConflictError,
)
from ..kernel.models import utc_now

_PREPARE_LOCKS_GUARD = threading.Lock()
_PREPARE_LOCKS: dict[Path, threading.Lock] = {}


def _database_prepare_lock(database_path: Path) -> threading.Lock:
    resolved_path = database_path.expanduser().resolve()
    with _PREPARE_LOCKS_GUARD:
        lock = _PREPARE_LOCKS.get(resolved_path)
        if lock is None:
            lock = threading.Lock()
            _PREPARE_LOCKS[resolved_path] = lock
        return lock


class SQLiteAgentModeStateStore:
    """Persist CAS-protected state isolated by provider and ChatSpec."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self._prepare_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._prepared = False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _protect_files(self) -> None:
        for path in (
            self.database_path,
            Path(f"{self.database_path}-wal"),
            Path(f"{self.database_path}-shm"),
        ):
            try:
                path.chmod(0o600)
            except FileNotFoundError:
                continue
            except OSError:
                if os.name != "nt":
                    raise

    def _prepare_sync(self) -> None:
        with _database_prepare_lock(self.database_path):
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = NORMAL")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS agent_mode_state (
                        provider_id TEXT NOT NULL,
                        agent_id TEXT NOT NULL,
                        conversation_id TEXT NOT NULL,
                        state_key TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        model_json TEXT NOT NULL,
                        PRIMARY KEY (
                            provider_id,
                            agent_id,
                            conversation_id,
                            state_key
                        )
                    )
                    """,
                )
        self._protect_files()

    async def _prepare(self) -> None:
        if self._prepared:
            return
        async with self._prepare_lock:
            if self._prepared:
                return
            await asyncio.to_thread(self._prepare_sync)
            self._prepared = True

    @staticmethod
    def _parse(row: sqlite3.Row) -> AgentModeState:
        return AgentModeState.model_validate_json(row["model_json"])

    def _read_sync(
        self,
        provider_id: str,
        agent_id: str,
        conversation_id: str,
        state_key: str,
    ) -> AgentModeState | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT model_json FROM agent_mode_state "
                "WHERE provider_id = ? AND agent_id = ? "
                "AND conversation_id = ? AND state_key = ?",
                (provider_id, agent_id, conversation_id, state_key),
            ).fetchone()
        self._protect_files()
        return self._parse(row) if row is not None else None

    async def read(
        self,
        *,
        provider_id: str,
        agent_id: str,
        conversation_id: str,
        state_key: str,
    ) -> AgentModeState | None:
        """Read one exact provider-owned state value."""
        await self._prepare()
        return await asyncio.to_thread(
            self._read_sync,
            provider_id,
            agent_id,
            conversation_id,
            state_key,
        )

    def _write_sync(
        self,
        state: AgentModeState,
        expected_revision: int,
    ) -> AgentModeState:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revision, model_json FROM agent_mode_state "
                "WHERE provider_id = ? AND agent_id = ? "
                "AND conversation_id = ? AND state_key = ?",
                (
                    state.provider_id,
                    state.agent_id,
                    state.conversation_id,
                    state.state_key,
                ),
            ).fetchone()
            current_revision = int(row["revision"]) if row else 0
            if current_revision != expected_revision:
                connection.rollback()
                raise AgentModeStateConflictError(
                    f"agent mode state revision is {current_revision}, "
                    f"expected {expected_revision}",
                )
            if row is not None:
                current = self._parse(row)
                if (
                    current.writer_registry_epoch_id
                    == state.writer_registry_epoch_id
                    and state.writer_generation < current.writer_generation
                ):
                    connection.rollback()
                    raise AgentModeStateConflictError(
                        "agent mode state writer generation regressed",
                    )
            persisted = state.model_copy(
                update={
                    "revision": expected_revision + 1,
                    "updated_at": utc_now(),
                },
            )
            connection.execute(
                """
                INSERT INTO agent_mode_state (
                    provider_id, agent_id, conversation_id,
                    state_key, revision, model_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    provider_id, agent_id, conversation_id, state_key
                ) DO UPDATE SET
                    revision = excluded.revision,
                    model_json = excluded.model_json
                """,
                (
                    persisted.provider_id,
                    persisted.agent_id,
                    persisted.conversation_id,
                    persisted.state_key,
                    persisted.revision,
                    persisted.model_dump_json(),
                ),
            )
            connection.commit()
        self._protect_files()
        return persisted

    async def write(
        self,
        state: AgentModeState,
        *,
        expected_revision: int,
    ) -> AgentModeState:
        """Create or replace one exact observed state revision."""
        if state.revision != expected_revision:
            raise AgentModeStateConflictError(
                "agent mode state payload revision does not match expected",
            )
        await self._prepare()
        async with self._write_lock:
            return await asyncio.to_thread(
                self._write_sync,
                state,
                expected_revision,
            )


def lite_agent_mode_state_store(
    workspace_dir: str | Path,
) -> SQLiteAgentModeStateStore:
    """Build the Lite state store at its stable Workspace path."""
    return SQLiteAgentModeStateStore(
        Path(workspace_dir) / ".qwenpaw" / "lite" / "mode-state.db",
    )


__all__ = ["SQLiteAgentModeStateStore", "lite_agent_mode_state_store"]
