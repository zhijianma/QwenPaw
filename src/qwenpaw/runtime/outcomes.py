# -*- coding: utf-8 -*-
"""Lite persistence for explicit conversation business outcomes."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from ..kernel import ConversationOutcome

OUTCOME_SCHEMA_VERSION = 1


class ConversationOutcomeConflictError(RuntimeError):
    """Raised when an outcome violates immutable supersession."""


def _canonical(outcome: ConversationOutcome) -> str:
    return json.dumps(
        outcome.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


class SQLiteConversationOutcomeStore:
    """SQLite source of truth for explicit Conversation outcomes."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self._initialize_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        return connection

    def _protect_files(self) -> None:
        """Keep outcome content owner-only on POSIX hosts."""
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

    def _initialize_sync(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            version = int(
                connection.execute("PRAGMA user_version").fetchone()[0],
            )
            if version not in {0, OUTCOME_SCHEMA_VERSION}:
                raise ConversationOutcomeConflictError(
                    f"unsupported outcome schema: {version}",
                )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversation_outcomes (
                    outcome_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    model_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_outcomes_conversation
                    ON conversation_outcomes(
                        agent_id,
                        conversation_id,
                        correlation_id,
                        created_at DESC,
                        outcome_id DESC
                    );
                """,
            )
            connection.execute(
                f"PRAGMA user_version = {OUTCOME_SCHEMA_VERSION}",
            )
        self._protect_files()

    async def initialize(self) -> None:
        """Initialize the dedicated outcome store once."""
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            await asyncio.to_thread(self._initialize_sync)
            self._initialized = True

    @staticmethod
    def _parse(row: sqlite3.Row) -> ConversationOutcome:
        return ConversationOutcome.model_validate_json(row["model_json"])

    def _append_sync(self, outcome: ConversationOutcome) -> None:
        encoded = _canonical(outcome)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT model_json FROM conversation_outcomes "
                "WHERE outcome_id = ?",
                (str(outcome.outcome_id),),
            ).fetchone()
            if existing is not None:
                if existing["model_json"] != encoded:
                    raise ConversationOutcomeConflictError(
                        "outcome identity already has conflicting content",
                    )
                connection.commit()
                return
            latest_row = connection.execute(
                "SELECT model_json FROM conversation_outcomes "
                "WHERE agent_id = ? AND conversation_id = ? "
                "AND correlation_id = ? "
                "ORDER BY created_at DESC, outcome_id DESC LIMIT 1",
                (
                    outcome.agent_id,
                    outcome.conversation_id,
                    str(outcome.correlation_id),
                ),
            ).fetchone()
            latest = (
                self._parse(latest_row)
                if latest_row is not None
                else None
            )
            if latest is None and outcome.supersedes_outcome_id is not None:
                raise ConversationOutcomeConflictError(
                    "first outcome cannot supersede an unavailable outcome",
                )
            if latest is not None and (
                outcome.supersedes_outcome_id != latest.outcome_id
            ):
                raise ConversationOutcomeConflictError(
                    "new outcome must explicitly supersede the latest outcome",
                )
            if latest is not None and outcome.created_at < latest.created_at:
                raise ConversationOutcomeConflictError(
                    "superseding outcome cannot precede the latest outcome",
                )
            connection.execute(
                "INSERT INTO conversation_outcomes "
                "(outcome_id, agent_id, conversation_id, correlation_id, "
                "created_at, model_json) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(outcome.outcome_id),
                    outcome.agent_id,
                    outcome.conversation_id,
                    str(outcome.correlation_id),
                    outcome.created_at.isoformat(),
                    encoded,
                ),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
            self._protect_files()

    async def append(self, outcome: ConversationOutcome) -> None:
        """Append one immutable outcome with explicit supersession."""
        await self.initialize()
        async with self._write_lock:
            await asyncio.to_thread(self._append_sync, outcome)

    def _latest_for_correlations_sync(
        self,
        agent_id: str,
        conversation_id: str,
        correlation_ids: tuple[UUID, ...],
    ) -> tuple[ConversationOutcome, ...]:
        if not correlation_ids:
            return ()
        placeholders = ",".join("?" for _ in correlation_ids)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT model_json FROM conversation_outcomes "
                "WHERE agent_id = ? AND conversation_id = ? "
                f"AND correlation_id IN ({placeholders}) "
                "ORDER BY created_at DESC, outcome_id DESC",
                (
                    agent_id,
                    conversation_id,
                    *(str(item) for item in correlation_ids),
                ),
            ).fetchall()
        self._protect_files()
        latest: dict[UUID, ConversationOutcome] = {}
        for row in rows:
            outcome = self._parse(row)
            latest.setdefault(outcome.correlation_id, outcome)
        return tuple(
            latest[item]
            for item in correlation_ids
            if item in latest
        )

    async def latest_for_correlations(
        self,
        *,
        agent_id: str,
        conversation_id: str,
        correlation_ids: Sequence[UUID],
    ) -> tuple[ConversationOutcome, ...]:
        """Return the latest explicit outcome for requested correlations."""
        if not agent_id.strip() or not conversation_id.strip():
            raise ValueError("outcome owner cannot be empty")
        unique_ids = tuple(dict.fromkeys(correlation_ids))
        if len(unique_ids) > 100:
            raise ValueError("at most 100 outcome correlations may be read")
        await self.initialize()
        return await asyncio.to_thread(
            self._latest_for_correlations_sync,
            agent_id,
            conversation_id,
            unique_ids,
        )


def lite_conversation_outcome_store(
    workspace_dir: Path,
) -> SQLiteConversationOutcomeStore:
    """Return the Lite outcome store for one workspace."""
    return SQLiteConversationOutcomeStore(
        Path(workspace_dir)
        / ".qwenpaw"
        / "lite"
        / "conversation-outcomes.sqlite3",
    )


__all__ = [
    "ConversationOutcomeConflictError",
    "OUTCOME_SCHEMA_VERSION",
    "SQLiteConversationOutcomeStore",
    "lite_conversation_outcome_store",
]
