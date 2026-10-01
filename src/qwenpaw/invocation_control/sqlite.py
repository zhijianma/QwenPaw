# -*- coding: utf-8 -*-
"""SQLite Lite adapter for the server-owned invocation control plane."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID

from ..kernel import (
    ACTIVE_SUBMISSION_STATUSES,
    TERMINAL_SUBMISSION_STATUSES,
    ControlCommand,
    ControlCommandKind,
    ControlCommandStatus,
    ControlRecord,
    ControlReceipt,
    QueueProjection,
    SteerSafePoint,
    SubmissionStatus,
    TurnSubmission,
    TurnSubmissionRequest,
    validate_submission_transition,
)
from ..kernel.models import utc_now

CONTROL_SCHEMA_VERSION = 4


class InvocationControlError(RuntimeError):
    """Base class for durable invocation-control failures."""


class UnsupportedControlSchemaError(InvocationControlError):
    """Raised when the Lite database uses an unknown schema generation."""


class QueueRevisionConflictError(InvocationControlError):
    """Raised when optimistic queue concurrency detects a stale mutation."""

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"queue revision conflict: expected {expected}, actual {actual}",
        )


class ControlIdempotencyConflictError(InvocationControlError):
    """Raised when an idempotency key is reused for different content."""


class QueueTargetNotFoundError(InvocationControlError):
    """Raised when a queue command references an unknown submission."""


class QueueCommandConflictError(InvocationControlError):
    """Raised when a command is invalid for current authoritative state."""


def _canonical_json(model: Any) -> str:
    """Serialize one strict model for replay-safe conflict comparison."""
    return json.dumps(
        model.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _submission_request_json(request: TurnSubmissionRequest) -> str:
    """Return client-significant fields for submission idempotency."""
    payload = request.model_dump(
        mode="json",
        by_alias=True,
        exclude={"correlation_id"},
    )
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _control_request_json(command: ControlCommand) -> str:
    """Return client-significant fields for command idempotency."""
    payload = command.model_dump(
        mode="json",
        by_alias=True,
        exclude={"command_id", "requested_at"},
    )
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


class SQLiteInvocationControl:
    """Durable Lite queue with revision and idempotency enforcement."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
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
            version = int(
                connection.execute("PRAGMA user_version").fetchone()[0],
            )
            if version not in {0, 1, 2, 3, CONTROL_SCHEMA_VERSION}:
                raise UnsupportedControlSchemaError(
                    f"unsupported invocation-control schema: {version}",
                )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS control_queues (
                    agent_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(agent_id, conversation_id)
                );
                CREATE TABLE IF NOT EXISTS turn_submissions (
                    submission_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    queue_position INTEGER NOT NULL,
                    priority INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    model_json TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(agent_id, conversation_id, sequence),
                    UNIQUE(agent_id, conversation_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_turn_queue
                    ON turn_submissions(
                        agent_id,
                        conversation_id,
                        status,
                        sequence
                    );
                CREATE TABLE IF NOT EXISTS control_commands (
                    command_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    model_json TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(agent_id, conversation_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_control_commands_pending
                    ON control_commands(
                        agent_id,
                        conversation_id,
                        status,
                        updated_at
                    );
                """,
            )
            if version == 1:
                connection.execute(
                    "ALTER TABLE turn_submissions "
                    "ADD COLUMN priority INTEGER NOT NULL DEFAULT 20",
                )
            if version in {1, 2, 3}:
                columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(turn_submissions)",
                    ).fetchall()
                }
                if "queue_position" not in columns:
                    connection.execute(
                        "ALTER TABLE turn_submissions ADD COLUMN "
                        "queue_position INTEGER NOT NULL DEFAULT 0",
                    )
                connection.execute(
                    "UPDATE turn_submissions SET queue_position = sequence",
                )
                self._add_legacy_queue_positions(connection)
            if version in {1, 2}:
                self._remove_legacy_submission_session_ids(connection)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_turn_dispatch "
                "ON turn_submissions(agent_id, conversation_id, status, "
                "priority, queue_position)",
            )
            connection.execute(
                f"PRAGMA user_version = {CONTROL_SCHEMA_VERSION}",
            )

    @staticmethod
    def _add_legacy_queue_positions(
        connection: sqlite3.Connection,
    ) -> None:
        """Add the scheduling position to pre-v4 submission JSON."""
        rows = connection.execute(
            "SELECT submission_id, sequence, model_json "
            "FROM turn_submissions",
        ).fetchall()
        for row in rows:
            model_payload = json.loads(row["model_json"])
            model_payload["queue_position"] = int(row["sequence"])
            connection.execute(
                "UPDATE turn_submissions SET model_json = ? "
                "WHERE submission_id = ?",
                (
                    json.dumps(
                        model_payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    row["submission_id"],
                ),
            )

    @staticmethod
    def _remove_legacy_submission_session_ids(
        connection: sqlite3.Connection,
    ) -> None:
        """Remove the obsolete channel identity from control-plane JSON."""
        rows = connection.execute(
            "SELECT submission_id, request_json, model_json "
            "FROM turn_submissions",
        ).fetchall()
        for row in rows:
            request_payload = json.loads(row["request_json"])
            model_payload = json.loads(row["model_json"])
            request_payload.pop("session_id", None)
            model_payload.pop("session_id", None)
            connection.execute(
                "UPDATE turn_submissions SET request_json = ?, "
                "model_json = ? WHERE submission_id = ?",
                (
                    json.dumps(
                        request_payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    json.dumps(
                        model_payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    row["submission_id"],
                ),
            )

    async def initialize(self) -> None:
        """Initialize or validate the Lite schema once per instance."""
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            await asyncio.to_thread(self._initialize_sync)
            self._initialized = True

    @staticmethod
    def _queue_revision(
        connection: sqlite3.Connection,
        agent_id: str,
        conversation_id: str,
    ) -> int:
        row = connection.execute(
            "SELECT revision FROM control_queues "
            "WHERE agent_id = ? AND conversation_id = ?",
            (agent_id, conversation_id),
        ).fetchone()
        return int(row["revision"]) if row is not None else 0

    @staticmethod
    def _write_queue_revision(
        connection: sqlite3.Connection,
        agent_id: str,
        conversation_id: str,
        revision: int,
    ) -> None:
        now = utc_now().isoformat()
        connection.execute(
            "INSERT INTO control_queues "
            "(agent_id, conversation_id, revision, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(agent_id, conversation_id) DO UPDATE SET "
            "revision = excluded.revision, updated_at = excluded.updated_at",
            (agent_id, conversation_id, revision, now),
        )

    @staticmethod
    def _require_revision(actual: int, expected: int) -> None:
        if actual != expected:
            raise QueueRevisionConflictError(expected, actual)

    @staticmethod
    def _next_sequence(
        connection: sqlite3.Connection,
        agent_id: str,
        conversation_id: str,
    ) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS latest "
            "FROM turn_submissions "
            "WHERE agent_id = ? AND conversation_id = ?",
            (agent_id, conversation_id),
        ).fetchone()
        return int(row["latest"]) + 1

    @staticmethod
    def _submission_from_request(
        request: TurnSubmissionRequest,
        sequence: int,
    ) -> TurnSubmission:
        payload = request.model_dump(mode="python", by_alias=False)
        return TurnSubmission.model_validate(
            {
                **payload,
                "sequence": sequence,
                "queue_position": sequence,
                "status": SubmissionStatus.QUEUED,
            },
        )

    def _submit_sync(
        self,
        request: TurnSubmissionRequest,
        expected_revision: int | None,
    ) -> ControlReceipt:
        request_json = _submission_request_json(request)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT request_json, receipt_json FROM turn_submissions "
                "WHERE agent_id = ? AND conversation_id = ? "
                "AND idempotency_key = ?",
                (
                    request.agent_id,
                    request.conversation_id,
                    request.idempotency_key,
                ),
            ).fetchone()
            if existing is not None:
                if existing["request_json"] != request_json:
                    raise ControlIdempotencyConflictError(
                        "submission idempotency key has conflicting content",
                    )
                connection.rollback()
                return ControlReceipt.model_validate_json(
                    existing["receipt_json"],
                )
            actual = self._queue_revision(
                connection,
                request.agent_id,
                request.conversation_id,
            )
            if expected_revision is not None:
                self._require_revision(actual, expected_revision)
            submission = self._submission_from_request(
                request,
                self._next_sequence(
                    connection,
                    request.agent_id,
                    request.conversation_id,
                ),
            )
            revision = actual + 1
            receipt = ControlReceipt(
                submission_id=submission.submission_id,
                kind="enqueue",
                status=ControlCommandStatus.APPLIED,
                agent_id=request.agent_id,
                conversation_id=request.conversation_id,
                revision=revision,
            )
            connection.execute(
                "INSERT INTO turn_submissions "
                "(submission_id, agent_id, conversation_id, sequence, "
                "queue_position, priority, status, idempotency_key, "
                "request_json, model_json, updated_at, receipt_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(submission.submission_id),
                    submission.agent_id,
                    submission.conversation_id,
                    submission.sequence,
                    submission.queue_position,
                    submission.priority,
                    submission.status.value,
                    submission.idempotency_key,
                    request_json,
                    _canonical_json(submission),
                    submission.updated_at.isoformat(),
                    _canonical_json(receipt),
                ),
            )
            self._write_queue_revision(
                connection,
                request.agent_id,
                request.conversation_id,
                revision,
            )
            connection.commit()
            return receipt
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def submit(
        self,
        submission: TurnSubmissionRequest,
        *,
        expected_revision: int | None = None,
    ) -> ControlReceipt:
        """Persist and server-sequence one idempotent turn submission."""
        await self.initialize()
        async with self._write_lock:
            return await asyncio.to_thread(
                self._submit_sync,
                submission,
                expected_revision,
            )

    def _read_queue_sync(
        self,
        agent_id: str,
        conversation_id: str,
    ) -> QueueProjection:
        with self._connect() as connection:
            revision = self._queue_revision(
                connection,
                agent_id,
                conversation_id,
            )
            terminal = tuple(
                status.value for status in TERMINAL_SUBMISSION_STATUSES
            )
            placeholders = ",".join("?" for _ in terminal)
            rows = connection.execute(
                "SELECT model_json FROM turn_submissions "
                "WHERE agent_id = ? AND conversation_id = ? "
                f"AND status NOT IN ({placeholders}) "
                "ORDER BY CASE WHEN status = ? THEN 1 ELSE 0 END, "
                "queue_position, sequence",
                (
                    agent_id,
                    conversation_id,
                    *terminal,
                    SubmissionStatus.QUEUED.value,
                ),
            ).fetchall()
        submissions = tuple(
            TurnSubmission.model_validate_json(row["model_json"])
            for row in rows
        )
        active = [
            item.submission_id
            for item in submissions
            if item.status in ACTIVE_SUBMISSION_STATUSES
        ]
        return QueueProjection(
            agent_id=agent_id,
            conversation_id=conversation_id,
            revision=revision,
            active_submission_id=active[0] if active else None,
            submissions=submissions,
        )

    async def read_queue(
        self,
        *,
        agent_id: str,
        conversation_id: str,
    ) -> QueueProjection:
        """Read queued and active submissions from durable state."""
        await self.initialize()
        return await asyncio.to_thread(
            self._read_queue_sync,
            agent_id,
            conversation_id,
        )

    def _get_submission_sync(
        self,
        submission_id: UUID,
    ) -> TurnSubmission | None:
        with self._connect() as connection:
            return self._load_submission(connection, submission_id)

    async def get_submission(
        self,
        submission_id: UUID,
    ) -> TurnSubmission | None:
        """Return one submission including terminal records."""
        await self.initialize()
        return await asyncio.to_thread(
            self._get_submission_sync,
            submission_id,
        )

    def _list_dispatchable_sync(
        self,
        agent_id: str,
    ) -> tuple[TurnSubmission, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT model_json FROM turn_submissions "
                "WHERE agent_id = ? AND status IN (?, ?, ?, ?) "
                "ORDER BY priority, queue_position, sequence",
                (
                    agent_id,
                    SubmissionStatus.QUEUED.value,
                    SubmissionStatus.ADMITTED.value,
                    SubmissionStatus.RUNNING.value,
                    SubmissionStatus.INTERRUPTING.value,
                ),
            ).fetchall()
        submissions = tuple(
            TurnSubmission.model_validate_json(row["model_json"])
            for row in rows
        )
        active_conversations = {
            item.conversation_id
            for item in submissions
            if item.status in ACTIVE_SUBMISSION_STATUSES
        }
        selected: list[TurnSubmission] = []
        seen: set[str] = set()
        for item in submissions:
            if item.status is not SubmissionStatus.QUEUED:
                continue
            if item.conversation_id in active_conversations:
                continue
            if item.conversation_id in seen:
                continue
            seen.add(item.conversation_id)
            selected.append(item)
        return tuple(selected)

    async def list_dispatchable(
        self,
        *,
        agent_id: str,
    ) -> tuple[TurnSubmission, ...]:
        """Return one oldest queued submission per idle conversation."""
        await self.initialize()
        return await asyncio.to_thread(
            self._list_dispatchable_sync,
            agent_id,
        )

    def _recover_orphaned_submissions_sync(
        self,
        agent_id: str,
    ) -> tuple[TurnSubmission, ...]:
        """Interrupt active submissions whose Runtime owner disappeared."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            active_values = tuple(
                status.value for status in ACTIVE_SUBMISSION_STATUSES
            )
            placeholders = ",".join("?" for _ in active_values)
            rows = connection.execute(
                "SELECT model_json FROM turn_submissions "
                "WHERE agent_id = ? "
                f"AND status IN ({placeholders}) "
                "ORDER BY conversation_id, sequence",
                (agent_id, *active_values),
            ).fetchall()
            recovered: list[TurnSubmission] = []
            invocation_ids: set[UUID] = set()
            for row in rows:
                current = TurnSubmission.model_validate_json(
                    row["model_json"],
                )
                if current.invocation_id is None:
                    raise QueueCommandConflictError(
                        "active submission has no invocation identity",
                    )
                invocation_ids.add(current.invocation_id)
                if current.status is SubmissionStatus.RUNNING:
                    current = self._with_submission_state(
                        current,
                        SubmissionStatus.INTERRUPTING,
                        invocation_id=current.invocation_id,
                    )
                terminal = self._with_submission_state(
                    current,
                    SubmissionStatus.INTERRUPTED,
                    invocation_id=current.invocation_id,
                )
                self._persist_submission(connection, terminal)
                revision = self._queue_revision(
                    connection,
                    terminal.agent_id,
                    terminal.conversation_id,
                )
                self._write_queue_revision(
                    connection,
                    terminal.agent_id,
                    terminal.conversation_id,
                    revision + 1,
                )
                recovered.append(terminal)

            if invocation_ids:
                command_rows = connection.execute(
                    "SELECT model_json, receipt_json FROM control_commands "
                    "WHERE agent_id = ? AND status = ? "
                    "ORDER BY updated_at, command_id",
                    (
                        agent_id,
                        ControlCommandStatus.ACCEPTED.value,
                    ),
                ).fetchall()
                for row in command_rows:
                    command = ControlCommand.model_validate_json(
                        row["model_json"],
                    )
                    if command.target_invocation_id not in invocation_ids:
                        continue
                    receipt = ControlReceipt.model_validate_json(
                        row["receipt_json"],
                    )
                    is_interrupt = command.kind in {
                        ControlCommandKind.INTERRUPT_CURRENT,
                        ControlCommandKind.STOP_AND_CLEAR,
                    }
                    status = (
                        ControlCommandStatus.APPLIED
                        if is_interrupt
                        else ControlCommandStatus.REJECTED
                    )
                    detail = (
                        "runtime interrupted during dispatcher recovery"
                        if is_interrupt
                        else "runtime unavailable after dispatcher restart"
                    )
                    revision = (
                        self._queue_revision(
                            connection,
                            receipt.agent_id,
                            receipt.conversation_id,
                        )
                        + 1
                    )
                    payload = receipt.model_dump(
                        mode="python",
                        by_alias=False,
                    )
                    payload.update(
                        {
                            "status": status,
                            "detail": detail,
                            "revision": revision,
                            "recorded_at": utc_now(),
                        },
                    )
                    resolved = ControlReceipt.model_validate(payload)
                    connection.execute(
                        "UPDATE control_commands SET status = ?, "
                        "receipt_json = ?, updated_at = ? "
                        "WHERE command_id = ?",
                        (
                            status.value,
                            _canonical_json(resolved),
                            resolved.recorded_at.isoformat(),
                            str(command.command_id),
                        ),
                    )
                    self._write_queue_revision(
                        connection,
                        receipt.agent_id,
                        receipt.conversation_id,
                        revision,
                    )
            connection.commit()
            return tuple(recovered)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def recover_orphaned_submissions(
        self,
        *,
        agent_id: str,
    ) -> tuple[TurnSubmission, ...]:
        """Close prior-generation active turns before dispatch resumes."""
        await self.initialize()
        async with self._write_lock:
            return await asyncio.to_thread(
                self._recover_orphaned_submissions_sync,
                agent_id,
            )

    @staticmethod
    def _persist_submission(
        connection: sqlite3.Connection,
        submission: TurnSubmission,
    ) -> None:
        connection.execute(
            "UPDATE turn_submissions SET status = ?, queue_position = ?, "
            "model_json = ?, updated_at = ? WHERE submission_id = ?",
            (
                submission.status.value,
                submission.queue_position,
                _canonical_json(submission),
                submission.updated_at.isoformat(),
                str(submission.submission_id),
            ),
        )

    @staticmethod
    def _with_submission_state(
        submission: TurnSubmission,
        target: SubmissionStatus,
        *,
        invocation_id: UUID | None,
    ) -> TurnSubmission:
        validate_submission_transition(submission.status, target)
        payload = submission.model_dump(mode="python", by_alias=False)
        payload.update(
            {
                "status": target,
                "invocation_id": invocation_id,
                "revision": submission.revision + 1,
                "updated_at": utc_now(),
            },
        )
        return TurnSubmission.model_validate(payload)

    def _claim_next_sync(
        self,
        agent_id: str,
        conversation_id: str,
        invocation_id: UUID,
    ) -> TurnSubmission | None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            active_values = tuple(
                status.value for status in ACTIVE_SUBMISSION_STATUSES
            )
            placeholders = ",".join("?" for _ in active_values)
            active = connection.execute(
                "SELECT 1 FROM turn_submissions "
                "WHERE agent_id = ? AND conversation_id = ? "
                f"AND status IN ({placeholders}) LIMIT 1",
                (agent_id, conversation_id, *active_values),
            ).fetchone()
            if active is not None:
                connection.rollback()
                return None
            row = connection.execute(
                "SELECT model_json FROM turn_submissions "
                "WHERE agent_id = ? AND conversation_id = ? "
                "AND status = ? "
                "ORDER BY priority, queue_position, sequence LIMIT 1",
                (
                    agent_id,
                    conversation_id,
                    SubmissionStatus.QUEUED.value,
                ),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            current = TurnSubmission.model_validate_json(row["model_json"])
            claimed = self._with_submission_state(
                current,
                SubmissionStatus.ADMITTED,
                invocation_id=invocation_id,
            )
            self._persist_submission(connection, claimed)
            revision = self._queue_revision(
                connection,
                agent_id,
                conversation_id,
            )
            self._write_queue_revision(
                connection,
                agent_id,
                conversation_id,
                revision + 1,
            )
            connection.commit()
            return claimed
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def claim_next(
        self,
        *,
        agent_id: str,
        conversation_id: str,
        invocation_id: UUID,
    ) -> TurnSubmission | None:
        """Atomically admit the highest-priority oldest queued turn."""
        await self.initialize()
        async with self._write_lock:
            return await asyncio.to_thread(
                self._claim_next_sync,
                agent_id,
                conversation_id,
                invocation_id,
            )

    def _transition_submission_sync(
        self,
        submission_id: UUID,
        invocation_id: UUID,
        target: SubmissionStatus,
        expected_revision: int,
    ) -> TurnSubmission:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = self._load_submission(connection, submission_id)
            if current is None:
                raise QueueTargetNotFoundError(
                    "submission was not found",
                )
            if current.invocation_id != invocation_id:
                raise QueueCommandConflictError(
                    "submission is owned by another invocation",
                )
            if current.revision != expected_revision:
                raise QueueRevisionConflictError(
                    expected_revision,
                    current.revision,
                )
            next_invocation_id = (
                None if target is SubmissionStatus.QUEUED else invocation_id
            )
            updated = self._with_submission_state(
                current,
                target,
                invocation_id=next_invocation_id,
            )
            self._persist_submission(connection, updated)
            queue_revision = self._queue_revision(
                connection,
                current.agent_id,
                current.conversation_id,
            )
            self._write_queue_revision(
                connection,
                current.agent_id,
                current.conversation_id,
                queue_revision + 1,
            )
            connection.commit()
            return updated
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def transition_submission(
        self,
        submission_id: UUID,
        *,
        invocation_id: UUID,
        target: SubmissionStatus,
        expected_revision: int,
    ) -> TurnSubmission:
        """Commit one runtime-owned submission lifecycle transition."""
        await self.initialize()
        async with self._write_lock:
            return await asyncio.to_thread(
                self._transition_submission_sync,
                submission_id,
                invocation_id,
                target,
                expected_revision,
            )

    def watch(
        self,
        *,
        agent_id: str,
        conversation_id: str,
        after_revision: int,
    ) -> AsyncIterator[QueueProjection]:
        """Poll durable revisions independently of one process lifetime."""

        async def follow() -> AsyncIterator[QueueProjection]:
            revision = after_revision
            while True:
                projection = await self.read_queue(
                    agent_id=agent_id,
                    conversation_id=conversation_id,
                )
                if projection.revision > revision:
                    revision = projection.revision
                    yield projection
                await asyncio.sleep(0.1)

        return follow()

    async def control(self, command: ControlCommand) -> ControlReceipt:
        """Durably accept a validated command for the runtime dispatcher."""
        await self.initialize()
        async with self._write_lock:
            return await asyncio.to_thread(self._control_sync, command)

    @staticmethod
    def _load_submission(
        connection: sqlite3.Connection,
        submission_id: object,
    ) -> TurnSubmission | None:
        row = connection.execute(
            "SELECT model_json FROM turn_submissions "
            "WHERE submission_id = ?",
            (str(submission_id),),
        ).fetchone()
        if row is None:
            return None
        return TurnSubmission.model_validate_json(row["model_json"])

    @staticmethod
    def _load_active_submission(
        connection: sqlite3.Connection,
        command: ControlCommand,
    ) -> TurnSubmission | None:
        active_statuses = tuple(
            status.value for status in ACTIVE_SUBMISSION_STATUSES
        )
        placeholders = ",".join("?" for _ in active_statuses)
        row = connection.execute(
            "SELECT model_json FROM turn_submissions "
            "WHERE agent_id = ? AND conversation_id = ? "
            f"AND status IN ({placeholders}) ORDER BY sequence LIMIT 1",
            (
                command.agent_id,
                command.conversation_id,
                *active_statuses,
            ),
        ).fetchone()
        if row is None:
            return None
        return TurnSubmission.model_validate_json(row["model_json"])

    @classmethod
    def _validate_live_target(
        cls,
        connection: sqlite3.Connection,
        command: ControlCommand,
    ) -> None:
        active = cls._load_active_submission(connection, command)
        if active is None:
            raise QueueCommandConflictError("conversation has no active turn")
        if active.invocation_id != command.target_invocation_id:
            raise QueueCommandConflictError(
                "command target is not the active invocation",
            )

    @classmethod
    def _validate_queued_target(
        cls,
        connection: sqlite3.Connection,
        command: ControlCommand,
    ) -> None:
        target = cls._load_submission(
            connection,
            command.target_submission_id,
        )
        if target is None:
            raise QueueTargetNotFoundError("queued submission was not found")
        if (
            target.agent_id != command.agent_id
            or target.conversation_id != command.conversation_id
        ):
            raise QueueTargetNotFoundError("queued submission was not found")
        if target.status is not SubmissionStatus.QUEUED:
            raise QueueCommandConflictError(
                "cancel target is no longer queued",
            )

    @staticmethod
    def _validate_reorder_target(
        connection: sqlite3.Connection,
        command: ControlCommand,
    ) -> None:
        rows = connection.execute(
            "SELECT submission_id FROM turn_submissions "
            "WHERE agent_id = ? AND conversation_id = ? AND status = ?",
            (
                command.agent_id,
                command.conversation_id,
                SubmissionStatus.QUEUED.value,
            ),
        ).fetchall()
        actual = {row["submission_id"] for row in rows}
        requested = {
            str(submission_id)
            for submission_id in command.ordered_submission_ids
        }
        if requested != actual:
            raise QueueCommandConflictError(
                "reorder must contain every queued submission exactly once",
            )

    @classmethod
    def _validate_command_target(
        cls,
        connection: sqlite3.Connection,
        command: ControlCommand,
    ) -> None:
        if command.kind in {
            ControlCommandKind.STEER,
            ControlCommandKind.INTERRUPT_CURRENT,
        }:
            cls._validate_live_target(connection, command)
        elif command.kind is ControlCommandKind.CANCEL_QUEUED:
            cls._validate_queued_target(connection, command)
        elif command.kind is ControlCommandKind.REORDER:
            cls._validate_reorder_target(connection, command)
        elif command.kind is ControlCommandKind.STOP_AND_CLEAR:
            active = cls._load_active_submission(connection, command)
            if active is None:
                if command.target_invocation_id is not None:
                    raise QueueCommandConflictError(
                        "conversation has no active turn",
                    )
            elif active.invocation_id != command.target_invocation_id:
                raise QueueCommandConflictError(
                    "stop_and_clear must capture the active invocation",
                )

    @classmethod
    def _apply_immediate_command(
        cls,
        connection: sqlite3.Connection,
        command: ControlCommand,
    ) -> tuple[ControlCommandStatus | None, str]:
        """Apply queue-only commands inside their acceptance transaction."""
        if command.kind is ControlCommandKind.CANCEL_QUEUED:
            target = cls._load_submission(
                connection,
                command.target_submission_id,
            )
            if target is None:
                raise QueueTargetNotFoundError(
                    "queued submission was not found",
                )
            cancelled = cls._with_submission_state(
                target,
                SubmissionStatus.CANCELLED,
                invocation_id=None,
            )
            cls._persist_submission(connection, cancelled)
            return (
                ControlCommandStatus.APPLIED,
                "queued submission cancelled before admission",
            )
        if command.kind is ControlCommandKind.REORDER:
            for position, submission_id in enumerate(
                command.ordered_submission_ids,
                start=1,
            ):
                target = cls._load_submission(connection, submission_id)
                if target is None:
                    raise QueueTargetNotFoundError(
                        "queued submission was not found",
                    )
                payload = target.model_dump(mode="python", by_alias=False)
                payload.update(
                    {
                        "queue_position": position,
                        "revision": target.revision + 1,
                        "updated_at": utc_now(),
                    },
                )
                cls._persist_submission(
                    connection,
                    TurnSubmission.model_validate(payload),
                )
            return (
                ControlCommandStatus.APPLIED,
                "queued submissions reordered",
            )
        if command.kind is ControlCommandKind.STOP_AND_CLEAR:
            rows = connection.execute(
                "SELECT model_json FROM turn_submissions "
                "WHERE agent_id = ? AND conversation_id = ? "
                "AND status = ?",
                (
                    command.agent_id,
                    command.conversation_id,
                    SubmissionStatus.QUEUED.value,
                ),
            ).fetchall()
            for row in rows:
                queued = TurnSubmission.model_validate_json(
                    row["model_json"],
                )
                cancelled = cls._with_submission_state(
                    queued,
                    SubmissionStatus.CANCELLED,
                    invocation_id=None,
                )
                cls._persist_submission(connection, cancelled)
            count = len(rows)
            if command.target_invocation_id is None:
                return (
                    ControlCommandStatus.APPLIED,
                    f"queued submissions cancelled={count}; "
                    "no active invocation",
                )
            return (
                ControlCommandStatus.ACCEPTED,
                f"queued submissions cancelled={count}; "
                "waiting for active invocation cancellation",
            )
        return None, ""

    def _control_sync(self, command: ControlCommand) -> ControlReceipt:
        request_json = _control_request_json(command)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT request_json, receipt_json FROM control_commands "
                "WHERE agent_id = ? AND conversation_id = ? "
                "AND idempotency_key = ?",
                (
                    command.agent_id,
                    command.conversation_id,
                    command.idempotency_key,
                ),
            ).fetchone()
            if existing is not None:
                if existing["request_json"] != request_json:
                    raise ControlIdempotencyConflictError(
                        "control idempotency key has conflicting content",
                    )
                connection.rollback()
                return ControlReceipt.model_validate_json(
                    existing["receipt_json"],
                )
            actual = self._queue_revision(
                connection,
                command.agent_id,
                command.conversation_id,
            )
            self._require_revision(actual, command.expected_revision)
            self._validate_command_target(connection, command)
            immediate_status, detail = self._apply_immediate_command(
                connection,
                command,
            )
            revision = actual + 1
            receipt = ControlReceipt(
                command_id=command.command_id,
                kind=command.kind,
                status=immediate_status or ControlCommandStatus.ACCEPTED,
                agent_id=command.agent_id,
                conversation_id=command.conversation_id,
                revision=revision,
                detail=detail,
            )
            connection.execute(
                "INSERT INTO control_commands "
                "(command_id, agent_id, conversation_id, idempotency_key, "
                "status, request_json, model_json, receipt_json, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(command.command_id),
                    command.agent_id,
                    command.conversation_id,
                    command.idempotency_key,
                    receipt.status.value,
                    request_json,
                    _canonical_json(command),
                    _canonical_json(receipt),
                    receipt.recorded_at.isoformat(),
                ),
            )
            self._write_queue_revision(
                connection,
                command.agent_id,
                command.conversation_id,
                revision,
            )
            connection.commit()
            return receipt
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _list_accepted_sync(
        self,
        agent_id: str,
        conversation_id: str,
    ) -> tuple[ControlCommand, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT model_json FROM control_commands "
                "WHERE agent_id = ? AND conversation_id = ? "
                "AND status = ? ORDER BY updated_at, command_id",
                (
                    agent_id,
                    conversation_id,
                    ControlCommandStatus.ACCEPTED.value,
                ),
            ).fetchall()
        return tuple(
            ControlCommand.model_validate_json(row["model_json"])
            for row in rows
        )

    async def list_accepted_commands(
        self,
        *,
        agent_id: str,
        conversation_id: str,
    ) -> tuple[ControlCommand, ...]:
        """Return commands waiting for one runtime safe-point dispatcher."""
        await self.initialize()
        return await asyncio.to_thread(
            self._list_accepted_sync,
            agent_id,
            conversation_id,
        )

    async def list_for_conversation(
        self,
        *,
        agent_id: str,
        conversation_id: str,
        limit: int = 100,
    ) -> tuple[ControlRecord, ...]:
        """List newest control commands with their latest receipts."""
        if not agent_id.strip() or not conversation_id.strip():
            raise ValueError("control owner cannot be empty")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        await self.initialize()
        return await asyncio.to_thread(
            self._list_for_conversation_sync,
            agent_id,
            conversation_id,
            limit,
        )

    def _list_for_conversation_sync(
        self,
        agent_id: str,
        conversation_id: str,
        limit: int | None,
    ) -> tuple[ControlRecord, ...]:
        with self._connect() as connection:
            suffix = " LIMIT ?" if limit is not None else ""
            arguments: tuple[object, ...] = (
                (agent_id, conversation_id, limit)
                if limit is not None
                else (agent_id, conversation_id)
            )
            rows = connection.execute(
                "SELECT model_json, receipt_json FROM control_commands "
                "WHERE agent_id = ? AND conversation_id = ? "
                "ORDER BY updated_at DESC, command_id DESC" + suffix,
                arguments,
            ).fetchall()
        return tuple(
            ControlRecord(
                command=ControlCommand.model_validate_json(
                    row["model_json"],
                ),
                receipt=ControlReceipt.model_validate_json(
                    row["receipt_json"],
                ),
            )
            for row in rows
        )

    async def scan_for_conversation(
        self,
        *,
        agent_id: str,
        conversation_id: str,
    ) -> tuple[ControlRecord, ...]:
        """Scan all Lite records for a derived-index rebuild."""
        if not agent_id.strip() or not conversation_id.strip():
            raise ValueError("control owner cannot be empty")
        await self.initialize()
        return await asyncio.to_thread(
            self._list_for_conversation_sync,
            agent_id,
            conversation_id,
            None,
        )

    def _lookup_control_sync(
        self,
        agent_id: str,
        conversation_id: str,
        idempotency_key: str,
    ) -> tuple[ControlCommand, ControlReceipt] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT model_json, receipt_json FROM control_commands "
                "WHERE agent_id = ? AND conversation_id = ? "
                "AND idempotency_key = ?",
                (agent_id, conversation_id, idempotency_key),
            ).fetchone()
        if row is None:
            return None
        return (
            ControlCommand.model_validate_json(row["model_json"]),
            ControlReceipt.model_validate_json(row["receipt_json"]),
        )

    async def lookup_control(
        self,
        *,
        agent_id: str,
        conversation_id: str,
        idempotency_key: str,
    ) -> tuple[ControlCommand, ControlReceipt] | None:
        """Read one original command and its latest replayable receipt."""
        await self.initialize()
        return await asyncio.to_thread(
            self._lookup_control_sync,
            agent_id,
            conversation_id,
            idempotency_key,
        )

    def _resolve_command_sync(
        self,
        command_id: UUID,
        status: ControlCommandStatus,
        detail: str,
        safe_point: SteerSafePoint | None,
    ) -> ControlReceipt:
        if status is ControlCommandStatus.ACCEPTED:
            raise ValueError("resolved command status must be terminal")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT receipt_json FROM control_commands "
                "WHERE command_id = ?",
                (str(command_id),),
            ).fetchone()
            if row is None:
                raise QueueTargetNotFoundError("control command was not found")
            current = ControlReceipt.model_validate_json(
                row["receipt_json"],
            )
            if current.status is not ControlCommandStatus.ACCEPTED:
                if (
                    current.status is status
                    and current.detail == detail
                    and current.applied_at_safe_point is safe_point
                ):
                    connection.rollback()
                    return current
                raise QueueCommandConflictError(
                    "control command is already resolved",
                )
            revision = (
                self._queue_revision(
                    connection,
                    current.agent_id,
                    current.conversation_id,
                )
                + 1
            )
            payload = current.model_dump(mode="python", by_alias=False)
            payload.update(
                {
                    "status": status,
                    "detail": detail,
                    "revision": revision,
                    "applied_at_safe_point": safe_point,
                    "recorded_at": utc_now(),
                },
            )
            resolved = ControlReceipt.model_validate(payload)
            connection.execute(
                "UPDATE control_commands SET status = ?, "
                "receipt_json = ?, updated_at = ? WHERE command_id = ?",
                (
                    status.value,
                    _canonical_json(resolved),
                    resolved.recorded_at.isoformat(),
                    str(command_id),
                ),
            )
            self._write_queue_revision(
                connection,
                current.agent_id,
                current.conversation_id,
                revision,
            )
            connection.commit()
            return resolved
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def resolve_command(
        self,
        command_id: UUID,
        *,
        status: ControlCommandStatus,
        detail: str = "",
        safe_point: SteerSafePoint | None = None,
    ) -> ControlReceipt:
        """Persist the runtime's terminal acknowledgement for a command."""
        await self.initialize()
        async with self._write_lock:
            return await asyncio.to_thread(
                self._resolve_command_sync,
                command_id,
                status,
                detail,
                safe_point,
            )


__all__ = [
    "CONTROL_SCHEMA_VERSION",
    "ControlIdempotencyConflictError",
    "InvocationControlError",
    "QueueCommandConflictError",
    "QueueRevisionConflictError",
    "QueueTargetNotFoundError",
    "SQLiteInvocationControl",
    "UnsupportedControlSchemaError",
]
