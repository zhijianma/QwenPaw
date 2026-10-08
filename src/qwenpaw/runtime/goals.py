# -*- coding: utf-8 -*-
"""Lite SQLite adapter for Chat-owned long-running Goal state."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
from pathlib import Path
from uuid import UUID

from ..kernel.goals import (
    GoalExecution,
    GoalExecutionConflictError,
    GoalExecutionStatus,
)
from ..kernel.models import utc_now
from ..kernel.outcomes import ConversationOutcomeStatus

_PREPARE_LOCKS_GUARD = threading.Lock()
_PREPARE_LOCKS: dict[Path, threading.Lock] = {}


def goal_outcome_summary(status: ConversationOutcomeStatus) -> str:
    """Return the stable business summary used by every Goal replay."""
    if status is ConversationOutcomeStatus.ACHIEVED:
        return "The active long-running goal was explicitly completed."
    return (
        "The active long-running goal stopped at a confirmed "
        "blocking boundary."
    )


def _database_prepare_lock(database_path: Path) -> threading.Lock:
    resolved_path = database_path.expanduser().resolve()
    with _PREPARE_LOCKS_GUARD:
        lock = _PREPARE_LOCKS.get(resolved_path)
        if lock is None:
            lock = threading.Lock()
            _PREPARE_LOCKS[resolved_path] = lock
        return lock


class SQLiteGoalExecutionStore:
    """Persist one revisioned current Goal per Agent and ChatSpec."""

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
                    CREATE TABLE IF NOT EXISTS conversation_goals (
                        agent_id TEXT NOT NULL,
                        conversation_id TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        model_json TEXT NOT NULL,
                        PRIMARY KEY (agent_id, conversation_id)
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
    def _parse(row: sqlite3.Row) -> GoalExecution:
        return GoalExecution.model_validate_json(row["model_json"])

    def _read_sync(
        self,
        agent_id: str,
        conversation_id: str,
    ) -> GoalExecution | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT model_json FROM conversation_goals "
                "WHERE agent_id = ? AND conversation_id = ?",
                (agent_id, conversation_id),
            ).fetchone()
        self._protect_files()
        return self._parse(row) if row is not None else None

    async def read(
        self,
        *,
        agent_id: str,
        conversation_id: str,
    ) -> GoalExecution | None:
        """Return the current durable Goal snapshot for one Chat."""
        await self._prepare()
        return await asyncio.to_thread(
            self._read_sync,
            agent_id,
            conversation_id,
        )

    async def active_correlation(
        self,
        *,
        agent_id: str,
        conversation_id: str,
    ) -> UUID | None:
        """Return the Goal correlation while its execution remains live."""
        execution = await self.read(
            agent_id=agent_id,
            conversation_id=conversation_id,
        )
        if execution is None or execution.status not in {
            GoalExecutionStatus.ACTIVE,
            GoalExecutionStatus.OUTCOME_PENDING,
        }:
            return None
        return execution.correlation_id

    def _list_pending_sync(self, agent_id: str) -> tuple[GoalExecution, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT model_json FROM conversation_goals "
                "WHERE agent_id = ? ORDER BY conversation_id",
                (agent_id,),
            ).fetchall()
        self._protect_files()
        pending = []
        for row in rows:
            execution = self._parse(row)
            if execution.status is GoalExecutionStatus.OUTCOME_PENDING:
                pending.append(execution)
        return tuple(pending)

    async def list_pending(
        self,
        *,
        agent_id: str,
    ) -> tuple[GoalExecution, ...]:
        """Return pending Goal outcomes in stable Chat order."""
        await self._prepare()
        return await asyncio.to_thread(self._list_pending_sync, agent_id)

    def _write_sync(
        self,
        execution: GoalExecution,
        expected_revision: int,
    ) -> GoalExecution:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revision, model_json FROM conversation_goals "
                "WHERE agent_id = ? AND conversation_id = ?",
                (execution.agent_id, execution.conversation_id),
            ).fetchone()
            current_revision = int(row["revision"]) if row else 0
            if current_revision != expected_revision:
                connection.rollback()
                raise GoalExecutionConflictError(
                    f"goal revision is {current_revision}, expected "
                    f"{expected_revision}",
                )
            if row is not None:
                self._validate_transition(self._parse(row), execution)
            persisted = execution.model_copy(
                update={
                    "revision": expected_revision + 1,
                    "updated_at": utc_now(),
                },
            )
            connection.execute(
                """
                INSERT INTO conversation_goals (
                    agent_id, conversation_id, revision, model_json
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(agent_id, conversation_id) DO UPDATE SET
                    revision = excluded.revision,
                    model_json = excluded.model_json
                """,
                (
                    persisted.agent_id,
                    persisted.conversation_id,
                    persisted.revision,
                    persisted.model_dump_json(),
                ),
            )
            connection.commit()
        self._protect_files()
        return persisted

    @staticmethod
    def _validate_transition(
        current: GoalExecution,
        target: GoalExecution,
    ) -> None:
        if current.goal_id != target.goal_id:
            if current.status in {
                GoalExecutionStatus.ACTIVE,
                GoalExecutionStatus.OUTCOME_PENDING,
            }:
                raise GoalExecutionConflictError(
                    "active goal cannot be replaced",
                )
            if target.status is not GoalExecutionStatus.ACTIVE:
                raise GoalExecutionConflictError(
                    "replacement goal must start active",
                )
            return
        immutable = (
            "agent_id",
            "conversation_id",
            "correlation_id",
            "objective",
            "max_iterations",
            "token_budget",
            "started_at",
        )
        if any(
            getattr(current, field) != getattr(target, field)
            for field in immutable
        ):
            raise GoalExecutionConflictError(
                "goal immutable identity or contract changed",
            )
        transitions = {
            GoalExecutionStatus.ACTIVE: {
                GoalExecutionStatus.ACTIVE,
                GoalExecutionStatus.OUTCOME_PENDING,
                GoalExecutionStatus.ABANDONED,
                GoalExecutionStatus.EXHAUSTED,
            },
            GoalExecutionStatus.OUTCOME_PENDING: {
                GoalExecutionStatus.OUTCOME_PENDING,
                GoalExecutionStatus.COMPLETED,
                GoalExecutionStatus.BLOCKED,
                GoalExecutionStatus.ABANDONED,
            },
            GoalExecutionStatus.COMPLETED: {
                GoalExecutionStatus.COMPLETED,
            },
            GoalExecutionStatus.BLOCKED: {
                GoalExecutionStatus.BLOCKED,
            },
            GoalExecutionStatus.ABANDONED: {
                GoalExecutionStatus.ABANDONED,
            },
            GoalExecutionStatus.EXHAUSTED: {
                GoalExecutionStatus.EXHAUSTED,
            },
        }
        if target.status not in transitions[current.status]:
            raise GoalExecutionConflictError(
                f"invalid goal transition: {current.status.value} -> "
                f"{target.status.value}",
            )

    async def write(
        self,
        execution: GoalExecution,
        *,
        expected_revision: int,
    ) -> GoalExecution:
        """Create or replace one exact observed revision."""
        if execution.revision != expected_revision:
            raise GoalExecutionConflictError(
                "goal payload revision does not match expected revision",
            )
        await self._prepare()
        async with self._write_lock:
            return await asyncio.to_thread(
                self._write_sync,
                execution,
                expected_revision,
            )


def lite_goal_execution_store(
    workspace_dir: str | Path,
) -> SQLiteGoalExecutionStore:
    """Build the Lite Goal store at its stable Workspace path."""
    return SQLiteGoalExecutionStore(
        Path(workspace_dir) / ".qwenpaw" / "lite" / "goals.db",
    )


__all__ = [
    "SQLiteGoalExecutionStore",
    "goal_outcome_summary",
    "lite_goal_execution_store",
]
