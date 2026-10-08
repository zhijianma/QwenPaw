# -*- coding: utf-8 -*-
"""SQLite source of truth for model-resource waits and continuations."""

from __future__ import annotations

import asyncio
import json
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
    ModelFailureClass,
    ModelOutputBoundary,
    ModelRecoveryDisposition,
    ModelResourceWait,
    ModelStepContextCheckpoint,
    ModelStepContinuation,
    ModelStepContinuationStatus,
    ModelStepReconciliation,
    ModelStepReconciliationReason,
    ModelStepRetryAuthorization,
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
ModelStepDispatcher = Callable[
    [ModelStepContinuation],
    Awaitable[UUID | None],
]


def _same_reconciliation(
    left: ModelStepReconciliation | None,
    right: ModelStepReconciliation | None,
) -> bool:
    """Compare reconciliation facts without their audit timestamp."""
    if left is None or right is None:
        return left is right
    return left.model_dump(exclude={"assessed_at"}) == right.model_dump(
        exclude={"assessed_at"},
    )


def _same_context_checkpoint(
    left: ModelStepContextCheckpoint,
    right: ModelStepContextCheckpoint,
) -> bool:
    """Compare checkpoint identity without its first-write timestamp."""
    return left.model_dump(exclude={"created_at"}) == right.model_dump(
        exclude={"created_at"},
    )


def _model_step_from_row(
    row: sqlite3.Row,
) -> ModelStepContinuation:
    """Decode one continuation row for service and history adapters."""
    values = dict(row)
    raw_reconciliation = values.pop("reconciliation_json", None)
    values["reconciliation"] = (
        json.loads(raw_reconciliation)
        if raw_reconciliation is not None
        else None
    )
    raw_checkpoint = values.pop("context_checkpoint_json", None)
    values["context_checkpoint"] = (
        json.loads(raw_checkpoint)
        if raw_checkpoint is not None
        else None
    )
    raw_authorization = values.pop("retry_authorization_json", None)
    values["retry_authorization"] = (
        json.loads(raw_authorization)
        if raw_authorization is not None
        else None
    )
    return ModelStepContinuation.model_validate(values)


class ModelResourceWaitService:  # pylint: disable=too-many-public-methods
    """Persist resource blockers separately from the user turn queue."""

    def __init__(
        self,
        database_path: Path,
        *,
        agent_id: str,
        rate_limit_delay_seconds: int = 60,
        transport_delay_seconds: int = 60,
        max_automatic_recovery_cycles: int = 3,
        max_model_step_recovery_cycles: int = 2,
    ) -> None:
        if not agent_id.strip():
            raise ValueError("resource wait owner cannot be empty")
        if rate_limit_delay_seconds < 1:
            raise ValueError("rate-limit delay must be positive")
        if transport_delay_seconds < 1:
            raise ValueError("transport delay must be positive")
        if max_automatic_recovery_cycles < 1:
            raise ValueError("automatic recovery cycles must be positive")
        if max_model_step_recovery_cycles < 1:
            raise ValueError("model-step recovery cycles must be positive")
        self.database_path = Path(database_path)
        self.agent_id = agent_id
        self.rate_limit_delay_seconds = rate_limit_delay_seconds
        self.transport_delay_seconds = transport_delay_seconds
        self.max_automatic_recovery_cycles = (
            max_automatic_recovery_cycles
        )
        self.max_model_step_recovery_cycles = (
            max_model_step_recovery_cycles
        )
        self._change_event = asyncio.Event()
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
                    provider_id TEXT,
                    model_id TEXT,
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
                CREATE INDEX IF NOT EXISTS idx_model_resource_correlation
                    ON model_resource_waits(
                        agent_id,
                        correlation_id,
                        failure_class
                    );
                CREATE TABLE IF NOT EXISTS model_step_continuations (
                    continuation_id TEXT PRIMARY KEY,
                    attempt_id TEXT NOT NULL UNIQUE,
                    invocation_id TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    output_boundary TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reconciliation_json TEXT,
                    context_checkpoint_json TEXT,
                    retry_authorization_json TEXT,
                    submission_id TEXT,
                    revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_model_step_ready
                    ON model_step_continuations(
                        agent_id,
                        status,
                        created_at
                    );
                CREATE INDEX IF NOT EXISTS idx_model_step_correlation
                    ON model_step_continuations(
                        agent_id,
                        correlation_id,
                        created_at
                );
                """,
            )
            wait_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(model_resource_waits)",
                ).fetchall()
            }
            if "provider_id" not in wait_columns:
                connection.execute(
                    "ALTER TABLE model_resource_waits "
                    "ADD COLUMN provider_id TEXT",
                )
            if "model_id" not in wait_columns:
                connection.execute(
                    "ALTER TABLE model_resource_waits "
                    "ADD COLUMN model_id TEXT",
                )
            step_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(model_step_continuations)",
                ).fetchall()
            }
            if "reconciliation_json" not in step_columns:
                connection.execute(
                    "ALTER TABLE model_step_continuations "
                    "ADD COLUMN reconciliation_json TEXT",
                )
            if "context_checkpoint_json" not in step_columns:
                connection.execute(
                    "ALTER TABLE model_step_continuations "
                    "ADD COLUMN context_checkpoint_json TEXT",
                )
            if "retry_authorization_json" not in step_columns:
                connection.execute(
                    "ALTER TABLE model_step_continuations "
                    "ADD COLUMN retry_authorization_json TEXT",
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

    def notify_change(self) -> None:
        """Wake the workspace recovery worker without exposing its task."""
        self._change_event.set()

    def clear_change(self) -> None:
        """Clear a consumed wake signal before inspecting durable state."""
        self._change_event.clear()

    async def wait_for_change(self, timeout: float) -> None:
        """Wait until recovery state changes or the next timer is due."""
        try:
            await asyncio.wait_for(self._change_event.wait(), timeout=timeout)
        except TimeoutError:
            pass

    async def defer(
        self,
        attempt: ModelCallAttempt,
        result: ModelCallResult,
    ) -> ModelResourceWait | None:
        """Create one idempotent wait for a resource-gated result."""
        eligible_dispositions = {
            ModelRecoveryDisposition.RETRY_TRANSPORT,
            ModelRecoveryDisposition.WAIT_RESOURCE,
        }
        if result.recovery_disposition not in eligible_dispositions:
            return None
        if result.failure_class is None:
            raise ValueError("resource wait requires a failure class")
        supported_failures = {
            ModelFailureClass.TRANSPORT_UNAVAILABLE,
            ModelFailureClass.PROVIDER_OVERLOADED,
            ModelFailureClass.RATE_LIMITED,
            ModelFailureClass.QUOTA_EXHAUSTED,
        }
        if result.failure_class not in supported_failures:
            return None
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
            provider_id=attempt.provider_id,
            model_id=attempt.model_id,
            failure_class=result.failure_class,
            retry_delay_seconds=(
                max(result.retry_after_seconds, 1.0)
                if result.retry_after_seconds is not None
                else self.transport_delay_seconds
                if result.failure_class
                in {
                    ModelFailureClass.TRANSPORT_UNAVAILABLE,
                    ModelFailureClass.PROVIDER_OVERLOADED,
                }
                else self.rate_limit_delay_seconds
            ),
            created_at=result.completed_at,
        )
        await self.start()
        async with self._write_lock:
            persisted = await asyncio.to_thread(self._defer_sync, wait)
        self.notify_change()
        return persisted

    async def defer_model_step(
        self,
        attempt: ModelCallAttempt,
        result: ModelCallResult,
    ) -> ModelStepContinuation | None:
        """Create an immediate continuation for one partial stream."""
        if (
            result.recovery_disposition
            is not ModelRecoveryDisposition.CONTINUE_MODEL_STEP
            or result.failure_class
            is not ModelFailureClass.STREAM_INTERRUPTED
        ):
            return None
        if not result.emitted_content or result.output_boundary not in {
            ModelOutputBoundary.PARTIAL_STREAM,
            ModelOutputBoundary.INCOMPLETE_STREAM_END,
        }:
            raise ValueError(
                "model-step continuation requires emitted partial output",
            )
        if attempt.attempt_id != result.attempt_id:
            raise ValueError("model-step attempt identity mismatch")
        if attempt.invocation_id != result.invocation_id:
            raise ValueError("model-step invocation identity mismatch")
        if not attempt.conversation_id:
            return None
        continuation = ModelStepContinuation(
            continuation_id=uuid5(
                attempt.attempt_id,
                "model-step-continuation",
            ),
            attempt_id=attempt.attempt_id,
            invocation_id=attempt.invocation_id,
            correlation_id=attempt.correlation_id,
            agent_id=self.agent_id,
            conversation_id=attempt.conversation_id,
            output_boundary=result.output_boundary,
            created_at=result.completed_at,
            updated_at=result.completed_at,
        )
        await self.start()
        async with self._write_lock:
            persisted = await asyncio.to_thread(
                self._defer_model_step_sync,
                continuation,
            )
        self.notify_change()
        return persisted

    def _defer_model_step_sync(
        self,
        continuation: ModelStepContinuation,
    ) -> ModelStepContinuation:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM model_step_continuations "
                "WHERE continuation_id = ?",
                (str(continuation.continuation_id),),
            ).fetchone()
            if row is not None:
                existing = _model_step_from_row(row)
                immutable = (
                    "continuation_id",
                    "attempt_id",
                    "invocation_id",
                    "correlation_id",
                    "agent_id",
                    "conversation_id",
                    "output_boundary",
                    "created_at",
                )
                if any(
                    getattr(existing, field)
                    != getattr(continuation, field)
                    for field in immutable
                ):
                    raise ModelResourceWaitConflictError(
                        "model-step continuation has conflicting content",
                    )
                return existing
            count = connection.execute(
                "SELECT COUNT(*) AS total "
                "FROM model_step_continuations WHERE agent_id = ? "
                "AND correlation_id = ?",
                (self.agent_id, str(continuation.correlation_id)),
            ).fetchone()["total"]
            if count >= self.max_model_step_recovery_cycles:
                continuation = continuation.model_copy(
                    update={
                        "status": (
                            ModelStepContinuationStatus.RECOVERY_EXHAUSTED
                        ),
                    },
                )
            connection.execute(
                "INSERT INTO model_step_continuations "
                "(continuation_id, attempt_id, invocation_id, "
                "correlation_id, agent_id, conversation_id, "
                "output_boundary, status, reconciliation_json, "
                "context_checkpoint_json, retry_authorization_json, "
                "submission_id, revision, created_at, updated_at) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                self._step_to_values(continuation),
            )
        return continuation

    def _defer_sync(
        self,
        wait: ModelResourceWait,
    ) -> ModelResourceWait | None:
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
            if wait.failure_class in {
                ModelFailureClass.TRANSPORT_UNAVAILABLE,
                ModelFailureClass.PROVIDER_OVERLOADED,
                ModelFailureClass.RATE_LIMITED,
            }:
                count = connection.execute(
                    "SELECT COUNT(*) AS total "
                    "FROM model_resource_waits WHERE agent_id = ? "
                    "AND correlation_id = ? "
                    "AND failure_class IN (?, ?, ?)",
                    (
                        self.agent_id,
                        str(wait.correlation_id),
                        ModelFailureClass.TRANSPORT_UNAVAILABLE.value,
                        ModelFailureClass.PROVIDER_OVERLOADED.value,
                        ModelFailureClass.RATE_LIMITED.value,
                    ),
                ).fetchone()["total"]
                if count >= self.max_automatic_recovery_cycles:
                    wait = wait.model_copy(
                        update={
                            "status": (
                                ResourceWaitStatus.RECOVERY_EXHAUSTED
                            ),
                        },
                    )
            values = self._to_values(wait)
            connection.execute(
                "INSERT INTO model_resource_waits "
                "(wait_id, attempt_id, invocation_id, correlation_id, "
                "agent_id, conversation_id, provider_id, model_id, "
                "failure_class, trigger_kind, status, not_before, "
                "submission_id, revision, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
        return wait

    async def release(self, wait_id: UUID) -> ModelResourceWait:
        """Release an external-event wait after resource replenishment."""
        await self.start()
        async with self._write_lock:
            released = await asyncio.to_thread(self._release_sync, wait_id)
        self.notify_change()
        return released

    async def release_provider_resource(
        self,
        *,
        provider_id: str,
        model_id: str,
    ) -> tuple[ModelResourceWait, ...]:
        """Release exact quota waits after verified model availability."""
        provider_id = provider_id.strip()
        model_id = model_id.strip()
        if not provider_id or not model_id:
            raise ValueError("provider resource identity cannot be empty")
        await self.start()
        async with self._write_lock:
            released = await asyncio.to_thread(
                self._release_provider_resource_sync,
                provider_id,
                model_id,
            )
        if released:
            self.notify_change()
        return released

    def _release_provider_resource_sync(
        self,
        provider_id: str,
        model_id: str,
    ) -> tuple[ModelResourceWait, ...]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM model_resource_waits WHERE agent_id = ? "
                "AND provider_id = ? AND model_id = ? AND status = ? "
                "AND trigger_kind = ? AND failure_class = ? "
                "ORDER BY created_at, wait_id",
                (
                    self.agent_id,
                    provider_id,
                    model_id,
                    ResourceWaitStatus.WAITING.value,
                    ResourceWaitTrigger.EXTERNAL_EVENT.value,
                    ModelFailureClass.QUOTA_EXHAUSTED.value,
                ),
            ).fetchall()
            released = []
            for row in rows:
                current = self._from_row(row)
                updated = current.model_copy(
                    update={
                        "status": ResourceWaitStatus.READY,
                        "revision": current.revision + 1,
                        "updated_at": utc_now(),
                    },
                )
                self._update(connection, updated)
                released.append(updated)
            connection.commit()
            return tuple(released)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

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

    async def list_ready_model_steps(
        self,
    ) -> tuple[ModelStepContinuation, ...]:
        """Return immediate model-step continuations in creation order."""
        await self.start()
        return await asyncio.to_thread(self._list_ready_model_steps_sync)

    async def list_recoverable_model_steps(
        self,
    ) -> tuple[ModelStepContinuation, ...]:
        """Return ready and Action-blocked steps for reconciliation."""
        await self.start()
        return await asyncio.to_thread(
            self._list_recoverable_model_steps_sync,
        )

    def _list_recoverable_model_steps_sync(
        self,
    ) -> tuple[ModelStepContinuation, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM model_step_continuations "
                "WHERE agent_id = ? AND status IN (?, ?) "
                "ORDER BY created_at, continuation_id",
                (
                    self.agent_id,
                    ModelStepContinuationStatus.READY.value,
                    ModelStepContinuationStatus
                    .ACTION_RECONCILIATION_REQUIRED.value,
                ),
            ).fetchall()
        return tuple(_model_step_from_row(row) for row in rows)

    def _list_ready_model_steps_sync(
        self,
    ) -> tuple[ModelStepContinuation, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM model_step_continuations "
                "WHERE agent_id = ? AND status = ? "
                "ORDER BY created_at, continuation_id",
                (
                    self.agent_id,
                    ModelStepContinuationStatus.READY.value,
                ),
            ).fetchall()
        return tuple(_model_step_from_row(row) for row in rows)

    async def dispatch_model_step(
        self,
        continuation_id: UUID,
        dispatch: ModelStepDispatcher,
    ) -> ModelStepContinuation:
        """Serialize one model-step enqueue with cancellation."""
        await self.start()
        async with self._write_lock:
            current = await asyncio.to_thread(
                self._get_model_step_sync,
                continuation_id,
            )
            if current is None:
                raise ModelResourceWaitNotFoundError(str(continuation_id))
            if current.status is not ModelStepContinuationStatus.READY:
                return current
            submission_id = await dispatch(current)
            status = (
                ModelStepContinuationStatus.DISPATCHED
                if submission_id is not None
                else ModelStepContinuationStatus.CANCELLED
            )
            return await asyncio.to_thread(
                self._transition_model_step_sync,
                continuation_id,
                status,
                submission_id,
            )

    async def require_action_reconciliation(
        self,
        continuation_id: UUID,
        reconciliation: ModelStepReconciliation,
    ) -> ModelStepContinuation:
        """Fail closed when an originating Invocation owns an Action."""
        await self.start()
        async with self._write_lock:
            return await asyncio.to_thread(
                self._transition_model_step_sync,
                continuation_id,
                (
                    ModelStepContinuationStatus
                    .ACTION_RECONCILIATION_REQUIRED
                ),
                None,
                reconciliation,
            )

    async def attach_model_step_context(
        self,
        continuation_id: UUID,
        checkpoint: ModelStepContextCheckpoint,
    ) -> ModelStepContinuation:
        """Attach an immutable context checkpoint and make the step ready."""
        await self.start()
        async with self._write_lock:
            updated = await asyncio.to_thread(
                self._attach_model_step_context_sync,
                continuation_id,
                checkpoint,
            )
        self.notify_change()
        return updated

    async def authorize_uncertain_action_retry(
        self,
        continuation_id: UUID,
        authorization: ModelStepRetryAuthorization,
    ) -> ModelStepContinuation:
        """Make one exact uncertain Action set eligible for a new step."""
        await self.start()
        async with self._write_lock:
            updated = await asyncio.to_thread(
                self._authorize_uncertain_action_retry_sync,
                continuation_id,
                authorization,
            )
        self.notify_change()
        return updated

    async def cancel_model_step(
        self,
        continuation_id: UUID,
    ) -> ModelStepContinuation:
        """Cancel a ready or blocked step superseded by newer input."""
        await self.start()
        async with self._write_lock:
            updated = await asyncio.to_thread(
                self._cancel_model_step_sync,
                continuation_id,
            )
        self.notify_change()
        return updated

    def _cancel_model_step_sync(
        self,
        continuation_id: UUID,
    ) -> ModelStepContinuation:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM model_step_continuations "
                "WHERE continuation_id = ?",
                (str(continuation_id),),
            ).fetchone()
            if row is None:
                raise ModelResourceWaitNotFoundError(str(continuation_id))
            current = _model_step_from_row(row)
            if current.status is ModelStepContinuationStatus.CANCELLED:
                connection.rollback()
                return current
            if current.status not in {
                ModelStepContinuationStatus.READY,
                ModelStepContinuationStatus.ACTION_RECONCILIATION_REQUIRED,
            }:
                raise ModelResourceWaitConflictError(
                    "only a pending model-step continuation can cancel",
                )
            updated = current.model_copy(
                update={
                    "status": ModelStepContinuationStatus.CANCELLED,
                    "reconciliation": None,
                    "revision": current.revision + 1,
                    "updated_at": utc_now(),
                },
            )
            self._update_model_step(connection, updated)
            connection.commit()
            return updated
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _attach_model_step_context_sync(
        self,
        continuation_id: UUID,
        checkpoint: ModelStepContextCheckpoint,
    ) -> ModelStepContinuation:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM model_step_continuations "
                "WHERE continuation_id = ?",
                (str(continuation_id),),
            ).fetchone()
            if row is None:
                raise ModelResourceWaitNotFoundError(str(continuation_id))
            current = _model_step_from_row(row)
            if (
                checkpoint.continuation_id != current.continuation_id
                or checkpoint.invocation_id != current.invocation_id
                or checkpoint.conversation_id != current.conversation_id
            ):
                raise ModelResourceWaitConflictError(
                    "model-step context checkpoint identity mismatch",
                )
            if current.context_checkpoint is not None:
                if not _same_context_checkpoint(
                    current.context_checkpoint,
                    checkpoint,
                ):
                    raise ModelResourceWaitConflictError(
                        "model-step context checkpoint conflict",
                    )
                connection.rollback()
                return current
            if current.status not in {
                ModelStepContinuationStatus.READY,
                ModelStepContinuationStatus.ACTION_RECONCILIATION_REQUIRED,
            }:
                raise ModelResourceWaitConflictError(
                    "model-step context cannot attach after dispatch",
                )
            if (
                current.status
                is ModelStepContinuationStatus.ACTION_RECONCILIATION_REQUIRED
                and (
                    current.reconciliation is None
                    or current.reconciliation.reason
                    is not ModelStepReconciliationReason
                    .DURABLE_CONTEXT_REQUIRED
                    or current.reconciliation.action_count
                    != checkpoint.action_count
                )
            ):
                raise ModelResourceWaitConflictError(
                    "model-step context cannot resolve Action uncertainty",
                )
            updated = current.model_copy(
                update={
                    "status": ModelStepContinuationStatus.READY,
                    "reconciliation": None,
                    "context_checkpoint": checkpoint,
                    "revision": current.revision + 1,
                    "updated_at": utc_now(),
                },
            )
            self._update_model_step(connection, updated)
            connection.commit()
            return updated
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _authorize_uncertain_action_retry_sync(
        self,
        continuation_id: UUID,
        authorization: ModelStepRetryAuthorization,
    ) -> ModelStepContinuation:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM model_step_continuations "
                "WHERE continuation_id = ?",
                (str(continuation_id),),
            ).fetchone()
            if row is None:
                raise ModelResourceWaitNotFoundError(str(continuation_id))
            current = _model_step_from_row(row)
            if current.retry_authorization is not None:
                if current.retry_authorization != authorization:
                    raise ModelResourceWaitConflictError(
                        "model-step retry authorization conflict",
                    )
                connection.rollback()
                return current
            if (
                authorization.continuation_id != current.continuation_id
                or authorization.invocation_id != current.invocation_id
                or authorization.conversation_id != current.conversation_id
            ):
                raise ModelResourceWaitConflictError(
                    "model-step retry authorization identity mismatch",
                )
            reconciliation = current.reconciliation
            if (
                current.status
                is not ModelStepContinuationStatus
                .ACTION_RECONCILIATION_REQUIRED
                or reconciliation is None
                or reconciliation.reason
                is not ModelStepReconciliationReason.UNCERTAIN_SIDE_EFFECT
                or reconciliation.action_count != authorization.action_count
            ):
                raise ModelResourceWaitConflictError(
                    "only uncertain Actions may authorize model-step retry",
                )
            updated = current.model_copy(
                update={
                    "status": ModelStepContinuationStatus.READY,
                    "reconciliation": None,
                    "retry_authorization": authorization,
                    "revision": current.revision + 1,
                    "updated_at": utc_now(),
                },
            )
            self._update_model_step(connection, updated)
            connection.commit()
            return updated
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def get_model_step(
        self,
        continuation_id: UUID,
    ) -> ModelStepContinuation | None:
        """Load one partial-stream continuation by stable identity."""
        await self.start()
        return await asyncio.to_thread(
            self._get_model_step_sync,
            continuation_id,
        )

    def _get_model_step_sync(
        self,
        continuation_id: UUID,
    ) -> ModelStepContinuation | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM model_step_continuations "
                "WHERE continuation_id = ?",
                (str(continuation_id),),
            ).fetchone()
        return _model_step_from_row(row) if row is not None else None

    def _transition_model_step_sync(
        self,
        continuation_id: UUID,
        status: ModelStepContinuationStatus,
        submission_id: UUID | None,
        reconciliation: ModelStepReconciliation | None = None,
    ) -> ModelStepContinuation:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM model_step_continuations "
                "WHERE continuation_id = ?",
                (str(continuation_id),),
            ).fetchone()
            if row is None:
                raise ModelResourceWaitNotFoundError(str(continuation_id))
            current = _model_step_from_row(row)
            if current.status is status:
                if (
                    current.submission_id != submission_id
                    or not _same_reconciliation(
                        current.reconciliation,
                        reconciliation,
                    )
                ):
                    raise ModelResourceWaitConflictError(
                        "model-step continuation submission conflict",
                    )
                connection.rollback()
                return current
            if current.status is not ModelStepContinuationStatus.READY:
                raise ModelResourceWaitConflictError(
                    "only a ready model-step continuation can transition",
                )
            updated = current.model_copy(
                update={
                    "status": status,
                    "submission_id": submission_id,
                    "reconciliation": reconciliation,
                    "revision": current.revision + 1,
                    "updated_at": utc_now(),
                },
            )
            self._update_model_step(connection, updated)
            connection.commit()
            return updated
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

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
            cancelled = await asyncio.to_thread(
                self._cancel_for_conversation_sync,
                conversation_id,
            )
        self.notify_change()
        return cancelled

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
            step_rows = connection.execute(
                "SELECT * FROM model_step_continuations "
                "WHERE agent_id = ? AND conversation_id = ? "
                "AND status = ?",
                (
                    self.agent_id,
                    conversation_id,
                    ModelStepContinuationStatus.READY.value,
                ),
            ).fetchall()
            for row in step_rows:
                current = _model_step_from_row(row)
                updated = current.model_copy(
                    update={
                        "status": ModelStepContinuationStatus.CANCELLED,
                        "revision": current.revision + 1,
                        "updated_at": utc_now(),
                    },
                )
                self._update_model_step(connection, updated)
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
            ResourceWaitStatus.RECOVERY_EXHAUSTED,
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
                else WaitConditionStatus.EXPIRED
                if wait.status
                is ResourceWaitStatus.RECOVERY_EXHAUSTED
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
            not_before=wait.not_before,
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
            wait.provider_id,
            wait.model_id,
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

    @staticmethod
    def _step_to_values(
        continuation: ModelStepContinuation,
    ) -> tuple[object, ...]:
        return (
            str(continuation.continuation_id),
            str(continuation.attempt_id),
            str(continuation.invocation_id),
            str(continuation.correlation_id),
            continuation.agent_id,
            continuation.conversation_id,
            continuation.output_boundary.value,
            continuation.status.value,
            (
                continuation.reconciliation.model_dump_json()
                if continuation.reconciliation is not None
                else None
            ),
            (
                continuation.context_checkpoint.model_dump_json()
                if continuation.context_checkpoint is not None
                else None
            ),
            (
                continuation.retry_authorization.model_dump_json()
                if continuation.retry_authorization is not None
                else None
            ),
            (
                str(continuation.submission_id)
                if continuation.submission_id
                else None
            ),
            continuation.revision,
            continuation.created_at.isoformat(),
            continuation.updated_at.isoformat(),
        )

    @classmethod
    def _update_model_step(
        cls,
        connection: sqlite3.Connection,
        continuation: ModelStepContinuation,
    ) -> None:
        connection.execute(
            "UPDATE model_step_continuations SET status = ?, "
            "reconciliation_json = ?, context_checkpoint_json = ?, "
            "retry_authorization_json = ?, submission_id = ?, "
            "revision = ?, updated_at = ? "
            "WHERE continuation_id = ?",
            (
                continuation.status.value,
                (
                    continuation.reconciliation.model_dump_json()
                    if continuation.reconciliation is not None
                    else None
                ),
                (
                    continuation.context_checkpoint.model_dump_json()
                    if continuation.context_checkpoint is not None
                    else None
                ),
                (
                    continuation.retry_authorization.model_dump_json()
                    if continuation.retry_authorization is not None
                    else None
                ),
                (
                    str(continuation.submission_id)
                    if continuation.submission_id
                    else None
                ),
                continuation.revision,
                continuation.updated_at.isoformat(),
                str(continuation.continuation_id),
            ),
        )


class ModelRecoveryHistory:
    """Read-only adapter over the model recovery source of truth."""

    def __init__(self, service: ModelResourceWaitService) -> None:
        self._service = service
        self._database_path = service.database_path
        self._agent_id = service.agent_id

    async def scan_model_steps_for_conversation(
        self,
        conversation_id: str,
    ) -> tuple[ModelStepContinuation, ...]:
        """Return content-free model-step recovery history for one Chat."""
        self._validate_conversation_id(conversation_id)
        await self._service.start()
        return await asyncio.to_thread(
            self._scan_model_steps_for_conversation_sync,
            conversation_id,
        )

    async def scan_model_resource_waits_for_conversation(
        self,
        conversation_id: str,
    ) -> tuple[ModelResourceWait, ...]:
        """Return content-free resource recovery history for one Chat."""
        self._validate_conversation_id(conversation_id)
        await self._service.start()
        return await asyncio.to_thread(
            self._scan_model_resource_waits_for_conversation_sync,
            conversation_id,
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        return connection

    def _scan_model_steps_for_conversation_sync(
        self,
        conversation_id: str,
    ) -> tuple[ModelStepContinuation, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM model_step_continuations "
                "WHERE agent_id = ? AND conversation_id = ? "
                "ORDER BY created_at, continuation_id",
                (self._agent_id, conversation_id),
            ).fetchall()
        continuations = []
        for row in rows:
            continuations.append(
                _model_step_from_row(row),
            )
        return tuple(continuations)

    def _scan_model_resource_waits_for_conversation_sync(
        self,
        conversation_id: str,
    ) -> tuple[ModelResourceWait, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM model_resource_waits "
                "WHERE agent_id = ? AND conversation_id = ? "
                "ORDER BY created_at, wait_id",
                (self._agent_id, conversation_id),
            ).fetchall()
        waits = []
        for row in rows:
            values = dict(row)
            values["trigger"] = values.pop("trigger_kind")
            waits.append(ModelResourceWait.model_validate(values))
        return tuple(waits)

    @staticmethod
    def _validate_conversation_id(conversation_id: str) -> None:
        if not conversation_id.strip():
            raise ValueError("conversation_id cannot be empty")


__all__ = [
    "ModelRecoveryHistory",
    "ModelResourceWaitConflictError",
    "ModelResourceWaitError",
    "ModelResourceWaitNotFoundError",
    "ModelResourceWaitService",
]
