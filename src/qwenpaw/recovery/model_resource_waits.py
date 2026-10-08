# -*- coding: utf-8 -*-
"""SQLite source of truth for model-resource waits and continuations."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid5

from ..kernel import (
    ContinuationAvailability,
    ContinuationMode,
    ContinuationRef,
    ModelCallAttempt,
    ModelCallResult,
    ModelRecoveryDisposition,
    ModelResourceWait,
    ResourceWaitStatus,
    ResourceWaitTrigger,
    WaitCondition,
    WaitConditionKind,
    WaitConditionStatus,
)
from ..kernel.models import utc_now


class ModelResourceWaitError(RuntimeError):
    """Base class for model-resource wait persistence failures."""


class ModelResourceWaitConflictError(ModelResourceWaitError):
    """Raised when one wait is completed with another Submission."""


class ModelResourceWaitNotFoundError(ModelResourceWaitError):
    """Raised when a wait identity is unknown."""


ResourceWaitDispatcher = Callable[
    [ModelResourceWait],
    Awaitable[UUID | None],
]


class ModelResourceWaitService:
    """Persist resource blockers separately from the user turn queue."""

    def __init__(
        self,
        database_path: Path,
        *,
        agent_id: str,
        rate_limit_delay_seconds: int = 60,
    ) -> None:
        if not agent_id.strip():
            raise ValueError("resource wait owner cannot be empty")
        if rate_limit_delay_seconds < 1:
            raise ValueError("rate-limit delay must be positive")
        self.database_path = Path(database_path)
        self.agent_id = agent_id
        self.rate_limit_delay_seconds = rate_limit_delay_seconds
        self._initialize_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_sync(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS model_resource_waits (
                    wait_id TEXT PRIMARY KEY,
                    attempt_id TEXT NOT NULL UNIQUE,
                    invocation_id TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    failure_class TEXT NOT NULL,
                    trigger_kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    not_before TEXT,
                    submission_id TEXT,
                    revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_model_resource_ready
                    ON model_resource_waits(
                        agent_id,
                        status,
                        not_before,
                        created_at
                    );
                CREATE INDEX IF NOT EXISTS idx_model_resource_chat
                    ON model_resource_waits(
                        agent_id,
                        conversation_id,
                        updated_at
                    );
                """,
            )

    async def start(self) -> None:
        """Initialize the durable store once."""
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            await asyncio.to_thread(self._initialize_sync)
            self._initialized = True

    async def close(self) -> None:
        """Close the service; SQLite connections are operation-scoped."""

    async def defer(
        self,
        attempt: ModelCallAttempt,
        result: ModelCallResult,
    ) -> ModelResourceWait | None:
        """Create one idempotent wait for a resource-gated result."""
        if (
            result.recovery_disposition
            is not ModelRecoveryDisposition.WAIT_RESOURCE
        ):
            return None
        if result.failure_class is None:
            raise ValueError("resource wait requires a failure class")
        if attempt.attempt_id != result.attempt_id:
            raise ValueError("resource wait attempt identity mismatch")
        if attempt.invocation_id != result.invocation_id:
            raise ValueError("resource wait invocation identity mismatch")
        if not attempt.conversation_id:
            return None
        wait = ModelResourceWait.for_model_failure(
            wait_id=uuid5(attempt.attempt_id, "model-resource-wait"),
            attempt_id=attempt.attempt_id,
            invocation_id=attempt.invocation_id,
            correlation_id=attempt.correlation_id,
            agent_id=self.agent_id,
            conversation_id=attempt.conversation_id,
            failure_class=result.failure_class,
            retry_delay_seconds=self.rate_limit_delay_seconds,
            created_at=result.completed_at,
        )
        await self.start()
        async with self._write_lock:
            return await asyncio.to_thread(self._defer_sync, wait)

    def _defer_sync(self, wait: ModelResourceWait) -> ModelResourceWait:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM model_resource_waits WHERE wait_id = ?",
                (str(wait.wait_id),),
            ).fetchone()
            if row is not None:
                existing = self._from_row(row)
                immutable = (
                    "wait_id",
                    "attempt_id",
                    "invocation_id",
                    "correlation_id",
                    "agent_id",
                    "conversation_id",
                    "failure_class",
                    "trigger",
                    "not_before",
                    "created_at",
                )
                if any(
                    getattr(existing, field) != getattr(wait, field)
                    for field in immutable
                ):
                    raise ModelResourceWaitConflictError(
                        "model resource wait has conflicting content",
                    )
                return existing
            values = self._to_values(wait)
            connection.execute(
                "INSERT INTO model_resource_waits VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
        return wait

    async def release(self, wait_id: UUID) -> ModelResourceWait:
        """Release an external-event wait after resource replenishment."""
        await self.start()
        async with self._write_lock:
            return await asyncio.to_thread(self._release_sync, wait_id)

    def _release_sync(self, wait_id: UUID) -> ModelResourceWait:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = self._load(connection, wait_id)
            if current.status is not ResourceWaitStatus.WAITING:
                connection.rollback()
                return current
            if current.trigger is not ResourceWaitTrigger.EXTERNAL_EVENT:
                raise ModelResourceWaitConflictError(
                    "timer resource wait cannot be released manually",
                )
            updated = current.model_copy(
                update={
                    "status": ResourceWaitStatus.READY,
                    "revision": current.revision + 1,
                    "updated_at": utc_now(),
                },
            )
            self._update(connection, updated)
            connection.commit()
            return updated
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def list_ready(self) -> Sequence[ModelResourceWait]:
        """Mature due timers and return dispatchable waits in order."""
        await self.start()
        async with self._write_lock:
            return await asyncio.to_thread(self._list_ready_sync)

    def _list_ready_sync(self) -> tuple[ModelResourceWait, ...]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                "UPDATE model_resource_waits SET status = ?, "
                "revision = revision + 1, updated_at = ? "
                "WHERE agent_id = ? AND status = ? "
                "AND trigger_kind = ? AND not_before <= ?",
                (
                    ResourceWaitStatus.READY.value,
                    now.isoformat(),
                    self.agent_id,
                    ResourceWaitStatus.WAITING.value,
                    ResourceWaitTrigger.TIMER.value,
                    now.isoformat(),
                ),
            )
            rows = connection.execute(
                "SELECT * FROM model_resource_waits "
                "WHERE agent_id = ? AND status = ? "
                "ORDER BY created_at, wait_id",
                (self.agent_id, ResourceWaitStatus.READY.value),
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    async def seconds_until_next_timer(self) -> float | None:
        """Return the bounded delay until the next automatic release."""
        await self.start()
        row = await asyncio.to_thread(self._next_timer_sync)
        if row is None:
            return None
        return max((row - utc_now()).total_seconds(), 0.0)

    def _next_timer_sync(self):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT MIN(not_before) AS next_at "
                "FROM model_resource_waits WHERE agent_id = ? "
                "AND status = ? AND trigger_kind = ?",
                (
                    self.agent_id,
                    ResourceWaitStatus.WAITING.value,
                    ResourceWaitTrigger.TIMER.value,
                ),
            ).fetchone()
        if row is None or row["next_at"] is None:
            return None
        return datetime.fromisoformat(row["next_at"])

    async def mark_dispatched(
        self,
        wait_id: UUID,
        submission_id: UUID,
    ) -> ModelResourceWait:
        """Bind a ready wait to its durable Submission exactly once."""
        await self.start()
        async with self._write_lock:
            return await asyncio.to_thread(
                self._mark_dispatched_sync,
                wait_id,
                submission_id,
            )

    async def dispatch_ready(
        self,
        wait_id: UUID,
        dispatch: ResourceWaitDispatcher,
    ) -> ModelResourceWait:
        """Serialize cancellation with one external enqueue operation.

        A ``None`` result means that the caller found a durable control
        fence and the wait must become cancelled instead of dispatched.
        """
        await self.start()
        async with self._write_lock:
            current = await asyncio.to_thread(self._get_sync, wait_id)
            if current is None:
                raise ModelResourceWaitNotFoundError(str(wait_id))
            if current.status is not ResourceWaitStatus.READY:
                return current
            submission_id = await dispatch(current)
            if submission_id is None:
                return await asyncio.to_thread(
                    self._cancel_wait_sync,
                    wait_id,
                )
            return await asyncio.to_thread(
                self._mark_dispatched_sync,
                wait_id,
                submission_id,
            )

    async def cancel_for_conversation(
        self,
        *,
        agent_id: str,
        conversation_id: str,
    ) -> tuple[ModelResourceWait, ...]:
        """Cancel every continuation that has not entered the turn queue."""
        if agent_id != self.agent_id or not conversation_id.strip():
            return ()
        await self.start()
        async with self._write_lock:
            return await asyncio.to_thread(
                self._cancel_for_conversation_sync,
                conversation_id,
            )

    def _cancel_for_conversation_sync(
        self,
        conversation_id: str,
    ) -> tuple[ModelResourceWait, ...]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM model_resource_waits "
                "WHERE agent_id = ? AND conversation_id = ? "
                "AND status IN (?, ?) ORDER BY created_at, wait_id",
                (
                    self.agent_id,
                    conversation_id,
                    ResourceWaitStatus.WAITING.value,
                    ResourceWaitStatus.READY.value,
                ),
            ).fetchall()
            cancelled = tuple(
                self._cancel_wait(connection, self._from_row(row))
                for row in rows
            )
            connection.commit()
            return cancelled
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _cancel_wait_sync(self, wait_id: UUID) -> ModelResourceWait:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = self._load(connection, wait_id)
            if current.status not in {
                ResourceWaitStatus.WAITING,
                ResourceWaitStatus.READY,
            }:
                connection.rollback()
                return current
            cancelled = self._cancel_wait(connection, current)
            connection.commit()
            return cancelled
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @classmethod
    def _cancel_wait(
        cls,
        connection: sqlite3.Connection,
        current: ModelResourceWait,
    ) -> ModelResourceWait:
        cancelled = current.model_copy(
            update={
                "status": ResourceWaitStatus.CANCELLED,
                "revision": current.revision + 1,
                "updated_at": utc_now(),
            },
        )
        cls._update(connection, cancelled)
        return cancelled

    def _mark_dispatched_sync(
        self,
        wait_id: UUID,
        submission_id: UUID,
    ) -> ModelResourceWait:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = self._load(connection, wait_id)
            if current.status is ResourceWaitStatus.DISPATCHED:
                if current.submission_id != submission_id:
                    raise ModelResourceWaitConflictError(
                        "resource wait is bound to another submission",
                    )
                connection.rollback()
                return current
            if current.status is not ResourceWaitStatus.READY:
                raise ModelResourceWaitConflictError(
                    "only a ready resource wait can be dispatched",
                )
            updated = current.model_copy(
                update={
                    "status": ResourceWaitStatus.DISPATCHED,
                    "submission_id": submission_id,
                    "revision": current.revision + 1,
                    "updated_at": utc_now(),
                },
            )
            self._update(connection, updated)
            connection.commit()
            return updated
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def get(self, wait_id: UUID) -> ModelResourceWait | None:
        """Load one wait for continuation materialization."""
        await self.start()
        return await asyncio.to_thread(self._get_sync, wait_id)

    def _get_sync(self, wait_id: UUID) -> ModelResourceWait | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM model_resource_waits WHERE wait_id = ?",
                (str(wait_id),),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    async def list_wait_conditions(
        self,
        *,
        agent_id: str,
        conversation_id: str,
        include_terminal: bool = False,
        limit: int = 100,
    ) -> Sequence[WaitCondition]:
        """Project model-resource facts into the shared wait contract."""
        if agent_id != self.agent_id or not conversation_id.strip():
            return ()
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        await self.start()
        rows = await asyncio.to_thread(
            self._list_for_conversation_sync,
            conversation_id,
            include_terminal,
            limit,
        )
        return tuple(self._project_wait(item) for item in rows)

    def _list_for_conversation_sync(
        self,
        conversation_id: str,
        include_terminal: bool,
        limit: int,
    ) -> tuple[ModelResourceWait, ...]:
        query = (
            "SELECT * FROM model_resource_waits WHERE agent_id = ? "
            "AND conversation_id = ? "
        )
        arguments: tuple[object, ...] = (self.agent_id, conversation_id)
        if not include_terminal:
            query += "AND status = ? "
            arguments += (ResourceWaitStatus.WAITING.value,)
        query += "ORDER BY updated_at DESC, wait_id DESC LIMIT ?"
        arguments += (limit,)
        with self._connect() as connection:
            rows = connection.execute(query, arguments).fetchall()
        return tuple(self._from_row(row) for row in rows)

    @staticmethod
    def _project_wait(wait: ModelResourceWait) -> WaitCondition:
        terminal = wait.status in {
            ResourceWaitStatus.READY,
            ResourceWaitStatus.DISPATCHED,
            ResourceWaitStatus.CANCELLED,
        }
        return WaitCondition(
            condition_id=wait.wait_id,
            kind=WaitConditionKind.RESOURCE,
            status=(
                WaitConditionStatus.SATISFIED
                if wait.status
                in {
                    ResourceWaitStatus.READY,
                    ResourceWaitStatus.DISPATCHED,
                }
                else WaitConditionStatus.CANCELLED
                if wait.status is ResourceWaitStatus.CANCELLED
                else WaitConditionStatus.WAITING
            ),
            agent_id=wait.agent_id,
            conversation_id=wait.conversation_id,
            source_type="qwenpaw.model-resource",
            source_id=wait.attempt_id,
            continuation=ContinuationRef(
                mode=ContinuationMode.CONVERSATION_TURN,
                availability=ContinuationAvailability.DETACHED,
                invocation_id=wait.invocation_id,
            ),
            revision=wait.revision,
            created_at=wait.created_at,
            resolved_at=wait.updated_at if terminal else None,
        )

    def _load(
        self,
        connection: sqlite3.Connection,
        wait_id: UUID,
    ) -> ModelResourceWait:
        row = connection.execute(
            "SELECT * FROM model_resource_waits WHERE wait_id = ?",
            (str(wait_id),),
        ).fetchone()
        if row is None:
            raise ModelResourceWaitNotFoundError(str(wait_id))
        return self._from_row(row)

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ModelResourceWait:
        values = dict(row)
        values["trigger"] = values.pop("trigger_kind")
        return ModelResourceWait.model_validate(values)

    @staticmethod
    def _to_values(wait: ModelResourceWait) -> tuple[object, ...]:
        return (
            str(wait.wait_id),
            str(wait.attempt_id),
            str(wait.invocation_id),
            str(wait.correlation_id),
            wait.agent_id,
            wait.conversation_id,
            wait.failure_class.value,
            wait.trigger.value,
            wait.status.value,
            wait.not_before.isoformat() if wait.not_before else None,
            str(wait.submission_id) if wait.submission_id else None,
            wait.revision,
            wait.created_at.isoformat(),
            wait.updated_at.isoformat(),
        )

    @classmethod
    def _update(
        cls,
        connection: sqlite3.Connection,
        wait: ModelResourceWait,
    ) -> None:
        connection.execute(
            "UPDATE model_resource_waits SET status = ?, submission_id = ?, "
            "revision = ?, updated_at = ? WHERE wait_id = ?",
            (
                wait.status.value,
                str(wait.submission_id) if wait.submission_id else None,
                wait.revision,
                wait.updated_at.isoformat(),
                str(wait.wait_id),
            ),
        )


__all__ = [
    "ModelResourceWaitConflictError",
    "ModelResourceWaitError",
    "ModelResourceWaitNotFoundError",
    "ModelResourceWaitService",
]
