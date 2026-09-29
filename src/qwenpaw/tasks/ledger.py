# -*- coding: utf-8 -*-
"""SQLite WAL implementation of the append-only execution ledger."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any
from uuid import UUID

from ..kernel.events import (
    ExecutionCommit,
    ExecutionEvent,
    TaskProjectionSnapshot,
)
from ..kernel.models import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    ExecutionCheckpoint,
    IdempotencyRecord,
    Plan,
    Run,
    SideEffectRecord,
    SideEffectStatus,
    Task,
    utc_now,
)

LEDGER_SCHEMA_VERSION = 2


class LedgerError(RuntimeError):
    """Base class for durable execution ledger failures."""


class UnsupportedLedgerSchemaError(LedgerError):
    """Raised when a database uses an unsupported schema version."""


class EventConflictError(LedgerError):
    """Raised when an event ID is reused with different content."""


class EventSequenceError(LedgerError):
    """Raised when a task event sequence is not contiguous."""


class EventCausalityError(LedgerError):
    """Raised when an event references an invalid direct cause."""


class CheckpointConflictError(LedgerError):
    """Raised when a checkpoint ID is reused with different content."""


class TaskVersionConflictError(LedgerError):
    """Raised when a task projection fails optimistic concurrency."""


class ProjectionConflictError(LedgerError):
    """Raised when an immutable projection already exists."""


class IdempotencyConflictError(LedgerError):
    """Raised when an idempotency key is already committed."""


class SideEffectConflictError(LedgerError):
    """Raised when a side-effect identity conflicts with durable state."""


def _canonical_json(model: Any) -> str:
    """Serialize a Pydantic model into stable JSON for conflict checks."""
    return json.dumps(
        model.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


class SQLiteExecutionLedger:  # pylint: disable=too-many-public-methods
    """Append-only event and checkpoint store backed by SQLite WAL."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self._initialize_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=30.0,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize_sync(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            current_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0],
            )
            if current_version not in (0, 1, LEDGER_SCHEMA_VERSION):
                raise UnsupportedLedgerSchemaError(
                    f"unsupported ledger schema version: {current_version}",
                )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    version INTEGER NOT NULL,
                    model_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS plans (
                    plan_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    model_json TEXT NOT NULL,
                    UNIQUE(task_id, revision)
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    model_json TEXT NOT NULL,
                    UNIQUE(task_id, attempt)
                );
                CREATE TABLE IF NOT EXISTS execution_events (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    run_id TEXT,
                    sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    registry_generation INTEGER NOT NULL,
                    event_json TEXT NOT NULL,
                    UNIQUE(task_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS idx_execution_events_task
                    ON execution_events(task_id, sequence);
                CREATE TABLE IF NOT EXISTS execution_checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    safe_to_resume INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    checkpoint_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_checkpoints_task
                    ON execution_checkpoints(
                        task_id,
                        safe_to_resume,
                        sequence
                    );
                CREATE TABLE IF NOT EXISTS approval_records (
                    approval_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    run_id TEXT,
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    decision_json TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_approvals_task_status
                    ON approval_records(task_id, run_id, status, updated_at);
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    operation TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(operation, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS side_effect_records (
                    record_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    record_json TEXT NOT NULL,
                    UNIQUE(task_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_side_effects_task_run
                    ON side_effect_records(task_id, run_id, started_at);
                """,
            )
            connection.execute(
                f"PRAGMA user_version = {LEDGER_SCHEMA_VERSION}",
            )

    async def initialize(self) -> None:
        """Initialize or validate the database schema once per instance."""
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            await asyncio.to_thread(self._initialize_sync)
            self._initialized = True

    async def schema_version(self) -> int:
        """Return the SQLite user-version for diagnostics."""
        await self.initialize()

        def read() -> int:
            with self._connect() as connection:
                return int(
                    connection.execute("PRAGMA user_version").fetchone()[0],
                )

        return await asyncio.to_thread(read)

    async def journal_mode(self) -> str:
        """Return the active journal mode for diagnostics."""
        await self.initialize()

        def read() -> str:
            with self._connect() as connection:
                row = connection.execute("PRAGMA journal_mode").fetchone()
                return str(row[0]).lower()

        return await asyncio.to_thread(read)

    def _append_sync(self, event: ExecutionEvent) -> bool:
        serialized = _canonical_json(event)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT event_json FROM execution_events "
                "WHERE event_id = ?",
                (str(event.event_id),),
            ).fetchone()
            if existing is not None:
                if existing["event_json"] == serialized:
                    connection.rollback()
                    return False
                raise EventConflictError(
                    f"event id {event.event_id} has conflicting content",
                )

            self._validate_event_causes(connection, (event,))

            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS latest "
                "FROM execution_events WHERE task_id = ?",
                (str(event.task_id),),
            ).fetchone()
            expected = int(row["latest"]) + 1
            if event.sequence != expected:
                raise EventSequenceError(
                    f"invalid event sequence for task {event.task_id}: "
                    f"expected {expected}, received {event.sequence}",
                )

            connection.execute(
                """
                INSERT INTO execution_events (
                    event_id,
                    task_id,
                    run_id,
                    sequence,
                    event_type,
                    occurred_at,
                    registry_generation,
                    event_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(event.event_id),
                    str(event.task_id),
                    str(event.run_id) if event.run_id else None,
                    event.sequence,
                    event.event_type,
                    event.occurred_at.isoformat(),
                    event.registry_generation,
                    serialized,
                ),
            )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _validate_event_causes(
        connection: sqlite3.Connection,
        events: tuple[ExecutionEvent, ...],
    ) -> None:
        """Require every direct cause to be an earlier event of one task."""
        incoming = {event.event_id: event for event in events}
        for event in events:
            cause_id = event.cause_event_id
            if cause_id is None:
                continue
            cause = incoming.get(cause_id)
            if cause is not None:
                if (
                    cause.task_id != event.task_id
                    or cause.sequence >= event.sequence
                ):
                    raise EventCausalityError(
                        "event cause must precede its effect in one task",
                    )
                continue
            row = connection.execute(
                "SELECT task_id, sequence FROM execution_events "
                "WHERE event_id = ?",
                (str(cause_id),),
            ).fetchone()
            if (
                row is None
                or row["task_id"] != str(event.task_id)
                or int(row["sequence"]) >= event.sequence
            ):
                raise EventCausalityError(
                    "event cause must reference an earlier event in one task",
                )

    @staticmethod
    def _existing_commit_state(
        connection: sqlite3.Connection,
        commit: ExecutionCommit,
    ) -> bool:
        """Return true when every commit event is an identical retry."""
        existing_count = 0
        for event in commit.events:
            row = connection.execute(
                "SELECT event_json FROM execution_events "
                "WHERE event_id = ?",
                (str(event.event_id),),
            ).fetchone()
            if row is None:
                continue
            existing_count += 1
            if row["event_json"] != _canonical_json(event):
                raise EventConflictError(
                    f"event id {event.event_id} has conflicting content",
                )
        if existing_count == 0:
            return False
        if existing_count != len(commit.events):
            raise EventConflictError(
                "execution commit was only partially persisted",
            )
        return True

    @staticmethod
    def _insert_commit_events(
        connection: sqlite3.Connection,
        commit: ExecutionCommit,
    ) -> None:
        SQLiteExecutionLedger._validate_event_causes(
            connection,
            commit.events,
        )
        first = commit.events[0]
        row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS latest "
            "FROM execution_events WHERE task_id = ?",
            (str(first.task_id),),
        ).fetchone()
        expected = int(row["latest"]) + 1
        if first.sequence != expected:
            raise EventSequenceError(
                f"invalid event sequence for task {first.task_id}: "
                f"expected {expected}, received {first.sequence}",
            )

        for event in commit.events:
            connection.execute(
                """
                INSERT INTO execution_events (
                    event_id,
                    task_id,
                    run_id,
                    sequence,
                    event_type,
                    occurred_at,
                    registry_generation,
                    event_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(event.event_id),
                    str(event.task_id),
                    str(event.run_id) if event.run_id else None,
                    event.sequence,
                    event.event_type,
                    event.occurred_at.isoformat(),
                    event.registry_generation,
                    _canonical_json(event),
                ),
            )

    @staticmethod
    def _write_task_projection(
        connection: sqlite3.Connection,
        commit: ExecutionCommit,
    ) -> None:
        task = commit.task
        if task is None:
            return
        serialized = _canonical_json(task)
        if commit.create_task:
            if task.version != 1:
                raise TaskVersionConflictError(
                    "a newly created task must have version 1",
                )
            try:
                connection.execute(
                    "INSERT INTO tasks ("
                    "task_id, version, model_json, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (
                        str(task.task_id),
                        task.version,
                        serialized,
                        task.created_at.isoformat(),
                        task.updated_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ProjectionConflictError(
                    f"task already exists: {task.task_id}",
                ) from exc
            return

        expected = commit.expected_task_version
        if expected is None or task.version != expected + 1:
            raise TaskVersionConflictError(
                "task update must increment the expected version once",
            )
        cursor = connection.execute(
            "UPDATE tasks SET version = ?, model_json = ?, updated_at = ? "
            "WHERE task_id = ? AND version = ?",
            (
                task.version,
                serialized,
                task.updated_at.isoformat(),
                str(task.task_id),
                expected,
            ),
        )
        if cursor.rowcount != 1:
            raise TaskVersionConflictError(
                f"task version conflict: {task.task_id}",
            )

    @staticmethod
    def _write_plan_projection(
        connection: sqlite3.Connection,
        plan: Plan | None,
    ) -> None:
        if plan is None:
            return
        try:
            connection.execute(
                "INSERT INTO plans (plan_id, task_id, revision, model_json) "
                "VALUES (?, ?, ?, ?)",
                (
                    str(plan.plan_id),
                    str(plan.task_id),
                    plan.revision,
                    _canonical_json(plan),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ProjectionConflictError(
                f"plan revision already exists: {plan.task_id} "
                f"revision {plan.revision}",
            ) from exc

    @staticmethod
    def _write_run_projection(
        connection: sqlite3.Connection,
        commit: ExecutionCommit,
    ) -> None:
        run = commit.run
        if run is None:
            return
        serialized = _canonical_json(run)
        if commit.create_run:
            try:
                connection.execute(
                    "INSERT INTO runs ("
                    "run_id, task_id, attempt, model_json"
                    ") VALUES (?, ?, ?, ?)",
                    (
                        str(run.run_id),
                        str(run.task_id),
                        run.attempt,
                        serialized,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ProjectionConflictError(
                    f"run attempt already exists: {run.task_id} "
                    f"attempt {run.attempt}",
                ) from exc
            return

        cursor = connection.execute(
            "UPDATE runs SET model_json = ? WHERE run_id = ?",
            (serialized, str(run.run_id)),
        )
        if cursor.rowcount != 1:
            raise ProjectionConflictError(f"run not found: {run.run_id}")

    @staticmethod
    def _write_approval_projection(
        connection: sqlite3.Connection,
        commit: ExecutionCommit,
    ) -> None:
        request = commit.approval_request
        if request is not None:
            try:
                connection.execute(
                    "INSERT INTO approval_records ("
                    "approval_id, task_id, run_id, status, request_json, "
                    "decision_json, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, NULL, ?)",
                    (
                        str(request.approval_id),
                        str(request.task_id),
                        str(request.run_id) if request.run_id else None,
                        request.status.value,
                        _canonical_json(request),
                        request.created_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ProjectionConflictError(
                    f"approval already exists: {request.approval_id}",
                ) from exc

        decisions = (
            *(
                (commit.approval_decision,)
                if commit.approval_decision is not None
                else ()
            ),
            *commit.additional_approval_decisions,
        )
        for decision in decisions:
            cursor = connection.execute(
                "UPDATE approval_records SET status = ?, "
                "decision_json = ?, updated_at = ? "
                "WHERE approval_id = ? AND decision_json IS NULL",
                (
                    decision.decision.value,
                    _canonical_json(decision),
                    decision.decided_at.isoformat(),
                    str(decision.approval_id),
                ),
            )
            if cursor.rowcount != 1:
                raise ProjectionConflictError(
                    f"approval is missing or resolved: "
                    f"{decision.approval_id}",
                )

    @staticmethod
    def _write_checkpoint_projection(
        connection: sqlite3.Connection,
        checkpoint: ExecutionCheckpoint | None,
    ) -> None:
        if checkpoint is None:
            return
        latest_row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS latest "
            "FROM execution_events WHERE task_id = ?",
            (str(checkpoint.task_id),),
        ).fetchone()
        latest = int(latest_row["latest"])
        if checkpoint.sequence > latest:
            raise EventSequenceError(
                f"checkpoint references uncommitted sequence "
                f"{checkpoint.sequence}; latest is {latest}",
            )
        try:
            connection.execute(
                "INSERT INTO execution_checkpoints ("
                "checkpoint_id, task_id, run_id, sequence, "
                "safe_to_resume, created_at, checkpoint_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(checkpoint.checkpoint_id),
                    str(checkpoint.task_id),
                    str(checkpoint.run_id),
                    checkpoint.sequence,
                    int(checkpoint.safe_to_resume),
                    checkpoint.created_at.isoformat(),
                    _canonical_json(checkpoint),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise CheckpointConflictError(
                f"checkpoint already exists: {checkpoint.checkpoint_id}",
            ) from exc

    @staticmethod
    def _write_idempotency(
        connection: sqlite3.Connection,
        record: IdempotencyRecord | None,
    ) -> None:
        if record is None:
            return
        try:
            connection.execute(
                "INSERT INTO idempotency_keys ("
                "operation, idempotency_key, request_hash, response_json, "
                "created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    record.operation,
                    record.key,
                    record.request_hash,
                    json.dumps(
                        record.response,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    utc_now().isoformat(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise IdempotencyConflictError(
                f"idempotency key already exists: {record.operation}",
            ) from exc

    @staticmethod
    def _write_side_effect(
        connection: sqlite3.Connection,
        record: SideEffectRecord | None,
    ) -> None:
        if record is None:
            return
        existing = connection.execute(
            "SELECT record_id, request_hash, record_json "
            "FROM side_effect_records WHERE task_id = ? "
            "AND idempotency_key = ?",
            (str(record.task_id), record.idempotency_key),
        ).fetchone()
        encoded = _canonical_json(record)
        if existing is None:
            if record.status is not SideEffectStatus.PREPARED:
                raise SideEffectConflictError(
                    "new side-effect records must start prepared",
                )
            connection.execute(
                "INSERT INTO side_effect_records ("
                "record_id, task_id, run_id, idempotency_key, "
                "request_hash, status, started_at, finished_at, record_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(record.record_id),
                    str(record.task_id),
                    str(record.run_id),
                    record.idempotency_key,
                    record.request_hash,
                    record.status.value,
                    record.started_at.isoformat(),
                    (
                        record.finished_at.isoformat()
                        if record.finished_at is not None
                        else None
                    ),
                    encoded,
                ),
            )
            return
        if (
            existing["record_id"] != str(record.record_id)
            or existing["request_hash"] != record.request_hash
        ):
            raise SideEffectConflictError(
                "side-effect idempotency key conflicts with durable state",
            )
        durable = SideEffectRecord.model_validate_json(
            existing["record_json"],
        )
        reconciles_uncertain = (
            durable.status is SideEffectStatus.UNCERTAIN
            and record.status is SideEffectStatus.FAILED
            and record.recovered_at is not None
        )
        if durable.status is not SideEffectStatus.PREPARED:
            if reconciles_uncertain:
                connection.execute(
                    "UPDATE side_effect_records SET status = ?, "
                    "finished_at = ?, record_json = ? WHERE record_id = ?",
                    (
                        record.status.value,
                        record.finished_at.isoformat(),
                        encoded,
                        str(record.record_id),
                    ),
                )
                return
            if encoded == existing["record_json"]:
                return
            raise SideEffectConflictError(
                "terminal side-effect records are immutable",
            )
        if record.status is SideEffectStatus.PREPARED:
            if encoded == existing["record_json"]:
                return
            raise SideEffectConflictError(
                "prepared side-effect records are immutable",
            )
        connection.execute(
            "UPDATE side_effect_records SET status = ?, finished_at = ?, "
            "record_json = ? WHERE record_id = ?",
            (
                record.status.value,
                (
                    record.finished_at.isoformat()
                    if record.finished_at is not None
                    else None
                ),
                encoded,
                str(record.record_id),
            ),
        )

    def _commit_sync(self, commit: ExecutionCommit) -> bool:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if self._existing_commit_state(connection, commit):
                connection.rollback()
                return False
            self._insert_commit_events(connection, commit)
            self._write_task_projection(connection, commit)
            self._write_plan_projection(connection, commit.plan)
            self._write_run_projection(connection, commit)
            self._write_approval_projection(connection, commit)
            self._write_checkpoint_projection(
                connection,
                commit.checkpoint,
            )
            self._write_idempotency(connection, commit.idempotency)
            self._write_side_effect(
                connection,
                commit.side_effect_record,
            )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def commit(self, commit: ExecutionCommit) -> bool:
        """Atomically persist projections and their immutable events."""
        await self.initialize()
        async with self._write_lock:
            return await asyncio.to_thread(self._commit_sync, commit)

    async def append(self, event: ExecutionEvent) -> bool:
        """Append one event or return false for an identical retry."""
        await self.initialize()
        async with self._write_lock:
            return await asyncio.to_thread(self._append_sync, event)

    async def get_task(self, task_id: UUID) -> Task | None:
        """Return one durable task projection."""
        await self.initialize()

        def read() -> Task | None:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT model_json FROM tasks WHERE task_id = ?",
                    (str(task_id),),
                ).fetchone()
            if row is None:
                return None
            return Task.model_validate_json(row["model_json"])

        return await asyncio.to_thread(read)

    def _read_projection_sync(
        self,
        task_id: UUID,
        event_limit: int,
    ) -> TaskProjectionSnapshot:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            task_row = connection.execute(
                "SELECT model_json FROM tasks WHERE task_id = ?",
                (str(task_id),),
            ).fetchone()
            run_rows = connection.execute(
                "SELECT model_json FROM runs WHERE task_id = ? "
                "ORDER BY attempt ASC",
                (str(task_id),),
            ).fetchall()
            plan_row = connection.execute(
                "SELECT model_json FROM plans WHERE task_id = ? "
                "ORDER BY revision DESC LIMIT 1",
                (str(task_id),),
            ).fetchone()
            approval_rows = connection.execute(
                "SELECT request_json, decision_json "
                "FROM approval_records WHERE task_id = ? "
                "ORDER BY updated_at ASC, approval_id ASC",
                (str(task_id),),
            ).fetchall()
            event_rows = connection.execute(
                "SELECT event_json FROM execution_events "
                "WHERE task_id = ? ORDER BY sequence ASC LIMIT ?",
                (str(task_id), event_limit),
            ).fetchall()
            checkpoint_row = connection.execute(
                "SELECT checkpoint_json FROM execution_checkpoints "
                "WHERE task_id = ? AND safe_to_resume = 1 "
                "ORDER BY sequence DESC, created_at DESC LIMIT 1",
                (str(task_id),),
            ).fetchone()
            sequence_row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS latest "
                "FROM execution_events WHERE task_id = ?",
                (str(task_id),),
            ).fetchone()
            approvals = tuple(
                (
                    ApprovalRequest.model_validate_json(
                        row["request_json"],
                    ),
                    (
                        ApprovalDecision.model_validate_json(
                            row["decision_json"],
                        )
                        if row["decision_json"]
                        else None
                    ),
                )
                for row in approval_rows
            )
            return TaskProjectionSnapshot(
                task=(
                    Task.model_validate_json(task_row["model_json"])
                    if task_row is not None
                    else None
                ),
                runs=tuple(
                    Run.model_validate_json(row["model_json"])
                    for row in run_rows
                ),
                latest_plan=(
                    Plan.model_validate_json(plan_row["model_json"])
                    if plan_row is not None
                    else None
                ),
                approvals=approvals,
                events=tuple(
                    ExecutionEvent.model_validate_json(row["event_json"])
                    for row in event_rows
                ),
                checkpoint=(
                    ExecutionCheckpoint.model_validate_json(
                        checkpoint_row["checkpoint_json"],
                    )
                    if checkpoint_row is not None
                    else None
                ),
                last_sequence=int(sequence_row["latest"]),
            )
        finally:
            connection.rollback()
            connection.close()

    async def read_projection(
        self,
        task_id: UUID,
        *,
        event_limit: int = 1000,
    ) -> TaskProjectionSnapshot:
        """Read all Task Workbench data in one SQLite transaction."""
        if event_limit < 1 or event_limit > 1000:
            raise ValueError("event_limit must be between 1 and 1000")
        await self.initialize()
        return await asyncio.to_thread(
            self._read_projection_sync,
            task_id,
            event_limit,
        )

    async def list_approvals(
        self,
        task_id: UUID,
        *,
        run_id: UUID | None = None,
        status: ApprovalStatus | None = None,
    ) -> list[tuple[ApprovalRequest, ApprovalDecision | None]]:
        """List approval projections without scanning execution events."""
        await self.initialize()

        def read() -> list[tuple[ApprovalRequest, ApprovalDecision | None]]:
            clauses = ["task_id = ?"]
            parameters: list[object] = [str(task_id)]
            if run_id is not None:
                clauses.append("run_id = ?")
                parameters.append(str(run_id))
            if status is not None:
                clauses.append("status = ?")
                parameters.append(status.value)
            query = (
                "SELECT request_json, decision_json "
                "FROM approval_records WHERE "
                + " AND ".join(clauses)
                + " ORDER BY updated_at ASC, approval_id ASC"
            )
            with self._connect() as connection:
                rows = connection.execute(query, parameters).fetchall()
            records = []
            for row in rows:
                request = ApprovalRequest.model_validate_json(
                    row["request_json"],
                )
                decision_json = row["decision_json"]
                decision = (
                    ApprovalDecision.model_validate_json(decision_json)
                    if decision_json
                    else None
                )
                records.append((request, decision))
            return records

        return await asyncio.to_thread(read)

    async def list_tasks(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> list[Task]:
        """Return a deterministic task page ordered by task ID."""
        if limit < 1 or limit > 200:
            raise ValueError("limit must be between 1 and 200")
        await self.initialize()

        def read() -> list[Task]:
            query = "SELECT model_json FROM tasks"
            parameters: tuple[object, ...]
            if cursor is None:
                parameters = (limit,)
            else:
                query += " WHERE task_id > ?"
                parameters = (cursor, limit)
            query += " ORDER BY task_id ASC LIMIT ?"
            with self._connect() as connection:
                rows = connection.execute(query, parameters).fetchall()
            return [
                Task.model_validate_json(row["model_json"]) for row in rows
            ]

        return await asyncio.to_thread(read)

    async def get_run(self, run_id: UUID) -> Run | None:
        """Return one durable run projection."""
        await self.initialize()

        def read() -> Run | None:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT model_json FROM runs WHERE run_id = ?",
                    (str(run_id),),
                ).fetchone()
            if row is None:
                return None
            return Run.model_validate_json(row["model_json"])

        return await asyncio.to_thread(read)

    async def list_runs(self, task_id: UUID) -> list[Run]:
        """Return all task runs ordered by attempt."""
        await self.initialize()

        def read() -> list[Run]:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT model_json FROM runs WHERE task_id = ? "
                    "ORDER BY attempt ASC",
                    (str(task_id),),
                ).fetchall()
            return [Run.model_validate_json(row["model_json"]) for row in rows]

        return await asyncio.to_thread(read)

    async def latest_plan(self, task_id: UUID) -> Plan | None:
        """Return the newest plan revision for one task."""
        await self.initialize()

        def read() -> Plan | None:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT model_json FROM plans WHERE task_id = ? "
                    "ORDER BY revision DESC LIMIT 1",
                    (str(task_id),),
                ).fetchone()
            if row is None:
                return None
            return Plan.model_validate_json(row["model_json"])

        return await asyncio.to_thread(read)

    async def get_approval(
        self,
        approval_id: UUID,
    ) -> tuple[ApprovalRequest, ApprovalDecision | None] | None:
        """Return one approval request and its optional decision."""
        await self.initialize()

        def read() -> tuple[ApprovalRequest, ApprovalDecision | None] | None:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT request_json, decision_json "
                    "FROM approval_records WHERE approval_id = ?",
                    (str(approval_id),),
                ).fetchone()
            if row is None:
                return None
            request = ApprovalRequest.model_validate_json(
                row["request_json"],
            )
            decision_json = row["decision_json"]
            decision = (
                ApprovalDecision.model_validate_json(decision_json)
                if decision_json
                else None
            )
            return request, decision

        return await asyncio.to_thread(read)

    async def get_idempotency(
        self,
        operation: str,
        key: str,
    ) -> IdempotencyRecord | None:
        """Return one durable idempotency response."""
        await self.initialize()

        def read() -> IdempotencyRecord | None:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT request_hash, response_json "
                    "FROM idempotency_keys WHERE operation = ? "
                    "AND idempotency_key = ?",
                    (operation, key),
                ).fetchone()
            if row is None:
                return None
            return IdempotencyRecord(
                operation=operation,
                key=key,
                request_hash=row["request_hash"],
                response=json.loads(row["response_json"]),
            )

        return await asyncio.to_thread(read)

    async def get_side_effect(
        self,
        record_id: UUID,
    ) -> SideEffectRecord | None:
        """Return one durable side-effect record by identity."""
        await self.initialize()

        def read() -> SideEffectRecord | None:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT record_json FROM side_effect_records "
                    "WHERE record_id = ?",
                    (str(record_id),),
                ).fetchone()
            if row is None:
                return None
            return SideEffectRecord.model_validate_json(row["record_json"])

        return await asyncio.to_thread(read)

    async def get_side_effect_by_key(
        self,
        task_id: UUID,
        idempotency_key: str,
    ) -> SideEffectRecord | None:
        """Return one durable side effect by task-scoped idempotency key."""
        await self.initialize()

        def read() -> SideEffectRecord | None:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT record_json FROM side_effect_records "
                    "WHERE task_id = ? AND idempotency_key = ?",
                    (str(task_id), idempotency_key),
                ).fetchone()
            if row is None:
                return None
            return SideEffectRecord.model_validate_json(row["record_json"])

        return await asyncio.to_thread(read)

    async def list_side_effects(
        self,
        task_id: UUID,
        *,
        run_id: UUID | None = None,
    ) -> list[SideEffectRecord]:
        """List durable side effects in stable creation order."""
        await self.initialize()

        def read() -> list[SideEffectRecord]:
            query = (
                "SELECT record_json FROM side_effect_records "
                "WHERE task_id = ?"
            )
            values: list[str] = [str(task_id)]
            if run_id is not None:
                query += " AND run_id = ?"
                values.append(str(run_id))
            query += " ORDER BY started_at ASC, record_id ASC"
            with self._connect() as connection:
                rows = connection.execute(query, values).fetchall()
            return [
                SideEffectRecord.model_validate_json(row["record_json"])
                for row in rows
            ]

        return await asyncio.to_thread(read)

    def _list_events_sync(
        self,
        task_id: UUID,
        after_sequence: int,
        limit: int,
    ) -> list[ExecutionEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event_json FROM execution_events "
                "WHERE task_id = ? AND sequence > ? "
                "ORDER BY sequence ASC LIMIT ?",
                (str(task_id), after_sequence, limit),
            ).fetchall()
        return [
            ExecutionEvent.model_validate_json(row["event_json"])
            for row in rows
        ]

    async def list_events(
        self,
        task_id: UUID,
        *,
        after_sequence: int = 0,
        limit: int = 200,
    ) -> list[ExecutionEvent]:
        """Read a bounded task timeline in ascending order."""
        if after_sequence < 0:
            raise ValueError("after_sequence cannot be negative")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        await self.initialize()
        return await asyncio.to_thread(
            self._list_events_sync,
            task_id,
            after_sequence,
            limit,
        )

    async def latest_sequence(self, task_id: UUID) -> int:
        """Return the latest sequence committed for a task."""
        await self.initialize()

        def read() -> int:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) AS latest "
                    "FROM execution_events WHERE task_id = ?",
                    (str(task_id),),
                ).fetchone()
                return int(row["latest"])

        return await asyncio.to_thread(read)

    def _save_checkpoint_sync(
        self,
        checkpoint: ExecutionCheckpoint,
    ) -> None:
        serialized = _canonical_json(checkpoint)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            latest_row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS latest "
                "FROM execution_events WHERE task_id = ?",
                (str(checkpoint.task_id),),
            ).fetchone()
            latest = int(latest_row["latest"])
            if checkpoint.sequence > latest:
                raise EventSequenceError(
                    f"checkpoint references uncommitted sequence "
                    f"{checkpoint.sequence}; latest is {latest}",
                )

            existing = connection.execute(
                "SELECT checkpoint_json FROM execution_checkpoints "
                "WHERE checkpoint_id = ?",
                (str(checkpoint.checkpoint_id),),
            ).fetchone()
            if existing is not None:
                if existing["checkpoint_json"] == serialized:
                    connection.rollback()
                    return
                raise CheckpointConflictError(
                    f"checkpoint id {checkpoint.checkpoint_id} conflicts",
                )

            connection.execute(
                """
                INSERT INTO execution_checkpoints (
                    checkpoint_id,
                    task_id,
                    run_id,
                    sequence,
                    safe_to_resume,
                    created_at,
                    checkpoint_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(checkpoint.checkpoint_id),
                    str(checkpoint.task_id),
                    str(checkpoint.run_id),
                    checkpoint.sequence,
                    int(checkpoint.safe_to_resume),
                    checkpoint.created_at.isoformat(),
                    serialized,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def save_checkpoint(
        self,
        checkpoint: ExecutionCheckpoint,
    ) -> None:
        """Persist a checkpoint referencing an already committed event."""
        await self.initialize()
        async with self._write_lock:
            await asyncio.to_thread(
                self._save_checkpoint_sync,
                checkpoint,
            )

    async def latest_resumable(
        self,
        task_id: UUID,
    ) -> ExecutionCheckpoint | None:
        """Return the newest safe checkpoint for a task."""
        await self.initialize()

        def read() -> ExecutionCheckpoint | None:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT checkpoint_json FROM execution_checkpoints "
                    "WHERE task_id = ? AND safe_to_resume = 1 "
                    "ORDER BY sequence DESC, created_at DESC LIMIT 1",
                    (str(task_id),),
                ).fetchone()
            if row is None:
                return None
            return ExecutionCheckpoint.model_validate_json(
                row["checkpoint_json"],
            )

        return await asyncio.to_thread(read)
