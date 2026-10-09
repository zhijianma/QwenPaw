# -*- coding: utf-8 -*-
"""Lite SQLite implementation of the durable Scheduler Port."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from ..kernel.scheduling import (
    ScheduleCursorConflictError,
    ScheduleDefinition,
    ScheduleDefinitionNotFoundError,
    ScheduleFire,
    ScheduleFireConflictError,
    ScheduleLease,
    ScheduleLeaseConflictError,
    ScheduleLeaseNotFoundError,
    ScheduleLeaseStatus,
    ScheduleTriggerCursor,
)

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


def _canonical_json(model: object) -> str:
    """Serialize one Pydantic contract for deterministic comparisons."""
    model_dump = getattr(model, "model_dump")
    return json.dumps(
        model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _utc(value: datetime) -> datetime:
    """Normalize one aware datetime for SQLite ordering."""
    if value.tzinfo is None:
        raise ValueError("scheduler datetimes must be timezone-aware")
    return value.astimezone(timezone.utc)


class SQLiteSchedulerStore:
    """Durable local schedule catalog and revisioned fire lease store."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = Path(database_path)
        self._prepare_lock = asyncio.Lock()
        self._prepared = False

    async def upsert(
        self,
        definition: ScheduleDefinition,
    ) -> ScheduleDefinition:
        """Create or replace one schedule without rewriting fire history."""
        await self._prepare()
        return await asyncio.to_thread(self._upsert_sync, definition)

    async def remove(self, *, agent_id: str, schedule_id: str) -> bool:
        """Delete one definition while preserving its immutable fires."""
        await self._prepare()
        return await asyncio.to_thread(
            self._remove_sync,
            agent_id,
            schedule_id,
        )

    async def list_definitions(
        self,
        *,
        agent_id: str,
    ) -> tuple[ScheduleDefinition, ...]:
        """Return one Agent's definitions in stable schedule-ID order."""
        await self._prepare()
        return await asyncio.to_thread(self._list_sync, agent_id)

    async def claim(
        self,
        fire: ScheduleFire,
        *,
        owner_id: str,
        lease_seconds: float,
    ) -> ScheduleLease:
        """Claim one fire or replay its existing idempotent lease."""
        self._validate_owner_and_duration(owner_id, lease_seconds)
        await self._prepare()
        return await asyncio.to_thread(
            self._claim_sync,
            fire,
            owner_id,
            lease_seconds,
        )

    async def renew(
        self,
        lease_id: UUID,
        *,
        owner_id: str,
        expected_revision: int,
        lease_seconds: float,
    ) -> ScheduleLease:
        """Extend one live lease through owner/revision CAS."""
        self._validate_owner_and_duration(owner_id, lease_seconds)
        await self._prepare()
        return await asyncio.to_thread(
            self._renew_sync,
            lease_id,
            owner_id,
            expected_revision,
            lease_seconds,
        )

    async def complete(
        self,
        lease_id: UUID,
        *,
        owner_id: str,
        expected_revision: int,
        task_id: UUID,
        run_id: UUID | None = None,
    ) -> ScheduleLease:
        """Bind a claimed fire to its created Task exactly once."""
        await self._prepare()
        return await asyncio.to_thread(
            self._complete_sync,
            lease_id,
            owner_id,
            expected_revision,
            task_id,
            run_id,
        )

    async def fail(
        self,
        lease_id: UUID,
        *,
        owner_id: str,
        expected_revision: int,
        error_code: str,
        retry_at: datetime | None = None,
    ) -> ScheduleLease:
        """Record one bounded terminal failure."""
        if not error_code.strip():
            raise ValueError("schedule failure error_code must not be empty")
        await self._prepare()
        return await asyncio.to_thread(
            self._fail_sync,
            lease_id,
            owner_id,
            expected_revision,
            error_code,
            _utc(retry_at) if retry_at is not None else None,
        )

    async def recover_expired(
        self,
        *,
        agent_id: str,
        now: datetime,
    ) -> tuple[ScheduleLease, ...]:
        """Fail one Agent's expired claims in one transaction."""
        await self._prepare()
        return await asyncio.to_thread(
            self._recover_expired_sync,
            agent_id,
            _utc(now),
        )

    async def reconcile_cursor(
        self,
        cursor: ScheduleTriggerCursor,
    ) -> ScheduleTriggerCursor:
        """Create trigger progress or reset it for a new definition hash."""
        await self._prepare()
        return await asyncio.to_thread(self._reconcile_cursor_sync, cursor)

    async def get_cursor(
        self,
        *,
        agent_id: str,
        schedule_id: str,
    ) -> ScheduleTriggerCursor | None:
        """Return one exact trigger cursor."""
        await self._prepare()
        return await asyncio.to_thread(
            self._get_cursor_sync,
            agent_id,
            schedule_id,
        )

    async def list_due_cursors(
        self,
        *,
        agent_id: str,
        now: datetime,
        limit: int = 100,
    ) -> tuple[ScheduleTriggerCursor, ...]:
        """Return due cursors in deterministic occurrence order."""
        if limit < 1 or limit > 1000:
            raise ValueError(
                "schedule cursor limit must be between 1 and 1000",
            )
        await self._prepare()
        return await asyncio.to_thread(
            self._list_due_cursors_sync,
            agent_id,
            _utc(now),
            limit,
        )

    async def advance_cursor(
        self,
        *,
        agent_id: str,
        schedule_id: str,
        definition_hash: str,
        expected_revision: int,
        scheduled_for: datetime,
        next_fire_at: datetime | None,
    ) -> ScheduleTriggerCursor:
        """Commit one handled occurrence through definition/revision CAS."""
        await self._prepare()
        return await asyncio.to_thread(
            self._advance_cursor_sync,
            agent_id,
            schedule_id,
            definition_hash,
            expected_revision,
            _utc(scheduled_for),
            _utc(next_fire_at) if next_fire_at is not None else None,
        )

    async def remove_cursor(
        self,
        *,
        agent_id: str,
        schedule_id: str,
        expected_definition_hash: str | None = None,
        expected_revision: int | None = None,
    ) -> bool:
        """Delete active progress, optionally guarded by cursor identity."""
        if (expected_definition_hash is None) != (expected_revision is None):
            raise ValueError(
                "schedule cursor removal guard requires hash and revision",
            )
        await self._prepare()
        return await asyncio.to_thread(
            self._remove_cursor_sync,
            agent_id,
            schedule_id,
            expected_definition_hash,
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
                connection.execute("BEGIN IMMEDIATE")
                self._prepare_definition_table(connection)
                self._prepare_lease_table(connection)
                self._prepare_cursor_table(connection)
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_schedule_leases_expiry
                    ON schedule_fire_leases(
                        agent_id, status, expires_at
                    )
                    """,
                )
                connection.commit()

    @staticmethod
    def _prepare_cursor_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schedule_trigger_cursors (
                agent_id TEXT NOT NULL,
                schedule_id TEXT NOT NULL,
                definition_hash TEXT NOT NULL,
                revision INTEGER NOT NULL,
                next_fire_at TEXT,
                cursor_json TEXT NOT NULL,
                PRIMARY KEY(agent_id, schedule_id)
            )
            """,
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_schedule_cursors_due
            ON schedule_trigger_cursors(agent_id, next_fire_at, schedule_id)
            """,
        )

    @staticmethod
    def _table_columns(
        connection: sqlite3.Connection,
        table: str,
    ) -> frozenset[str]:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        return frozenset(str(row["name"]) for row in rows)

    @staticmethod
    def _create_definition_table(
        connection: sqlite3.Connection,
    ) -> None:
        connection.execute(
            """
            CREATE TABLE schedule_definitions (
                agent_id TEXT NOT NULL,
                schedule_id TEXT NOT NULL,
                definition_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(agent_id, schedule_id)
            )
            """,
        )

    def _prepare_definition_table(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        columns = self._table_columns(connection, "schedule_definitions")
        if not columns:
            self._create_definition_table(connection)
            return
        if "agent_id" in columns:
            return

        legacy_table = "schedule_definitions_legacy_agent_scope"
        connection.execute(
            f"ALTER TABLE schedule_definitions RENAME TO {legacy_table}",
        )
        self._create_definition_table(connection)
        rows = connection.execute(
            f"""
            SELECT definition_json, updated_at
            FROM {legacy_table}
            """,
        ).fetchall()
        for row in rows:
            definition = ScheduleDefinition.model_validate_json(
                row["definition_json"],
            )
            connection.execute(
                """
                INSERT INTO schedule_definitions (
                    agent_id, schedule_id, definition_json, updated_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    definition.agent_id,
                    definition.schedule_id,
                    _canonical_json(definition),
                    row["updated_at"],
                ),
            )
        connection.execute(f"DROP TABLE {legacy_table}")

    @staticmethod
    def _create_lease_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE schedule_fire_leases (
                lease_id TEXT PRIMARY KEY,
                fire_id TEXT NOT NULL UNIQUE,
                agent_id TEXT NOT NULL,
                schedule_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                status TEXT NOT NULL,
                revision INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                lease_json TEXT NOT NULL,
                UNIQUE(agent_id, schedule_id, idempotency_key)
            )
            """,
        )

    def _prepare_lease_table(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        columns = self._table_columns(connection, "schedule_fire_leases")
        if not columns:
            self._create_lease_table(connection)
            return
        if "agent_id" in columns:
            return

        legacy_table = "schedule_fire_leases_legacy_agent_scope"
        connection.execute(
            f"ALTER TABLE schedule_fire_leases RENAME TO {legacy_table}",
        )
        self._create_lease_table(connection)
        rows = connection.execute(
            f"SELECT lease_json FROM {legacy_table}",
        ).fetchall()
        for row in rows:
            raw = json.loads(row["lease_json"])
            fire = raw.get("fire") if isinstance(raw, dict) else None
            schedule_id = (
                fire.get("schedule_id") if isinstance(fire, dict) else None
            )
            definitions = connection.execute(
                """
                SELECT agent_id FROM schedule_definitions
                WHERE schedule_id = ?
                ORDER BY agent_id
                """,
                (schedule_id,),
            ).fetchall()
            if len(definitions) != 1 or not isinstance(fire, dict):
                raise RuntimeError(
                    "cannot infer Agent ownership for legacy schedule fire",
                )
            fire["agent_id"] = definitions[0]["agent_id"]
            lease = ScheduleLease.model_validate(raw)
            connection.execute(
                """
                INSERT INTO schedule_fire_leases (
                    lease_id, fire_id, agent_id, schedule_id,
                    idempotency_key, owner_id, status, revision,
                    expires_at, lease_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._lease_row(lease),
            )
        connection.execute(f"DROP TABLE {legacy_table}")

    def _upsert_sync(
        self,
        definition: ScheduleDefinition,
    ) -> ScheduleDefinition:
        payload = _canonical_json(definition)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO schedule_definitions (
                    agent_id, schedule_id, definition_json, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(agent_id, schedule_id) DO UPDATE SET
                    definition_json = excluded.definition_json,
                    updated_at = excluded.updated_at
                """,
                (
                    definition.agent_id,
                    definition.schedule_id,
                    payload,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            connection.commit()
        return definition

    def _remove_sync(self, agent_id: str, schedule_id: str) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                DELETE FROM schedule_definitions
                WHERE agent_id = ? AND schedule_id = ?
                """,
                (agent_id, schedule_id),
            )
            connection.commit()
        return cursor.rowcount > 0

    def _list_sync(self, agent_id: str) -> tuple[ScheduleDefinition, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT definition_json FROM schedule_definitions
                WHERE agent_id = ?
                ORDER BY schedule_id
                """,
                (agent_id,),
            ).fetchall()
        return tuple(
            ScheduleDefinition.model_validate_json(row["definition_json"])
            for row in rows
        )

    def _claim_sync(
        self,
        fire: ScheduleFire,
        owner_id: str,
        lease_seconds: float,
    ) -> ScheduleLease:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            definition = connection.execute(
                """
                SELECT definition_json FROM schedule_definitions
                WHERE agent_id = ? AND schedule_id = ?
                """,
                (fire.agent_id, fire.schedule_id),
            ).fetchone()
            if definition is None:
                connection.rollback()
                raise ScheduleDefinitionNotFoundError(fire.schedule_id)
            parsed_definition = ScheduleDefinition.model_validate_json(
                definition["definition_json"],
            )
            if not parsed_definition.enabled:
                connection.rollback()
                raise ScheduleLeaseConflictError(
                    f"schedule '{fire.schedule_id}' is disabled",
                )
            existing = connection.execute(
                """
                SELECT lease_json FROM schedule_fire_leases
                WHERE agent_id = ? AND schedule_id = ?
                    AND idempotency_key = ?
                """,
                (
                    fire.agent_id,
                    fire.schedule_id,
                    fire.idempotency_key,
                ),
            ).fetchone()
            if existing is not None:
                lease = ScheduleLease.model_validate_json(
                    existing["lease_json"],
                )
                if not lease.fire.same_occurrence(fire):
                    connection.rollback()
                    raise ScheduleFireConflictError(
                        "schedule fire idempotency key has conflicting "
                        "content",
                    )
                connection.rollback()
                return lease
            now = datetime.now(timezone.utc)
            lease = ScheduleLease(
                fire=fire,
                owner_id=owner_id,
                acquired_at=now,
                expires_at=now + timedelta(seconds=lease_seconds),
            )
            connection.execute(
                """
                INSERT INTO schedule_fire_leases (
                    lease_id, fire_id, agent_id, schedule_id,
                    idempotency_key, owner_id, status, revision,
                    expires_at, lease_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._lease_row(lease),
            )
            connection.commit()
        return lease

    def _renew_sync(
        self,
        lease_id: UUID,
        owner_id: str,
        expected_revision: int,
        lease_seconds: float,
    ) -> ScheduleLease:
        now = datetime.now(timezone.utc)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._claimed_for_update(
                connection,
                lease_id,
                owner_id,
                expected_revision,
                now=now,
            )
            updated = current.model_copy(
                update={
                    "revision": current.revision + 1,
                    "expires_at": now + timedelta(seconds=lease_seconds),
                },
            )
            self._write_lease(connection, updated)
            connection.commit()
        return updated

    def _complete_sync(
        self,
        lease_id: UUID,
        owner_id: str,
        expected_revision: int,
        task_id: UUID,
        run_id: UUID | None,
    ) -> ScheduleLease:
        now = datetime.now(timezone.utc)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._claimed_for_update(
                connection,
                lease_id,
                owner_id,
                expected_revision,
                now=now,
            )
            updated = ScheduleLease.model_validate(
                {
                    **current.model_dump(),
                    "status": ScheduleLeaseStatus.COMPLETED,
                    "revision": current.revision + 1,
                    "finished_at": now,
                    "task_id": task_id,
                    "run_id": run_id,
                },
            )
            self._write_lease(connection, updated)
            connection.commit()
        return updated

    def _fail_sync(
        self,
        lease_id: UUID,
        owner_id: str,
        expected_revision: int,
        error_code: str,
        retry_at: datetime | None,
    ) -> ScheduleLease:
        now = datetime.now(timezone.utc)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._claimed_for_update(
                connection,
                lease_id,
                owner_id,
                expected_revision,
                now=now,
            )
            updated = ScheduleLease.model_validate(
                {
                    **current.model_dump(),
                    "status": ScheduleLeaseStatus.FAILED,
                    "revision": current.revision + 1,
                    "finished_at": now,
                    "error_code": error_code,
                    "retry_at": retry_at,
                },
            )
            self._write_lease(connection, updated)
            connection.commit()
        return updated

    def _recover_expired_sync(
        self,
        agent_id: str,
        now: datetime,
    ) -> tuple[ScheduleLease, ...]:
        now_text = now.isoformat()
        recovered: list[ScheduleLease] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT lease_json FROM schedule_fire_leases
                WHERE agent_id = ? AND status = ? AND expires_at <= ?
                ORDER BY expires_at, lease_id
                """,
                (
                    agent_id,
                    ScheduleLeaseStatus.CLAIMED.value,
                    now_text,
                ),
            ).fetchall()
            for row in rows:
                current = ScheduleLease.model_validate_json(
                    row["lease_json"],
                )
                updated = ScheduleLease.model_validate(
                    {
                        **current.model_dump(),
                        "status": ScheduleLeaseStatus.FAILED,
                        "revision": current.revision + 1,
                        "finished_at": now,
                        "error_code": "lease_expired",
                    },
                )
                self._write_lease(connection, updated)
                recovered.append(updated)
            connection.commit()
        return tuple(recovered)

    def _reconcile_cursor_sync(
        self,
        proposed: ScheduleTriggerCursor,
    ) -> ScheduleTriggerCursor:
        now = datetime.now(timezone.utc)
        normalized = ScheduleTriggerCursor.model_validate(
            {
                **proposed.model_dump(),
                "revision": 1,
                "next_fire_at": (
                    _utc(proposed.next_fire_at)
                    if proposed.next_fire_at is not None
                    else None
                ),
                "last_fire_at": None,
                "updated_at": now,
            },
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT cursor_json FROM schedule_trigger_cursors
                WHERE agent_id = ? AND schedule_id = ?
                """,
                (proposed.agent_id, proposed.schedule_id),
            ).fetchone()
            if row is not None:
                current = ScheduleTriggerCursor.model_validate_json(
                    row["cursor_json"],
                )
                if current.definition_hash == proposed.definition_hash:
                    connection.commit()
                    return current
                normalized = normalized.model_copy(
                    update={"revision": current.revision + 1},
                )
            connection.execute(
                """
                INSERT INTO schedule_trigger_cursors (
                    agent_id, schedule_id, definition_hash, revision,
                    next_fire_at, cursor_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id, schedule_id) DO UPDATE SET
                    definition_hash = excluded.definition_hash,
                    revision = excluded.revision,
                    next_fire_at = excluded.next_fire_at,
                    cursor_json = excluded.cursor_json
                """,
                self._cursor_row(normalized),
            )
            connection.commit()
        return normalized

    def _list_due_cursors_sync(
        self,
        agent_id: str,
        now: datetime,
        limit: int,
    ) -> tuple[ScheduleTriggerCursor, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT cursor_json FROM schedule_trigger_cursors
                WHERE agent_id = ?
                  AND next_fire_at IS NOT NULL
                  AND next_fire_at <= ?
                ORDER BY next_fire_at, schedule_id
                LIMIT ?
                """,
                (agent_id, now.isoformat(), limit),
            ).fetchall()
        return tuple(
            ScheduleTriggerCursor.model_validate_json(row["cursor_json"])
            for row in rows
        )

    def _get_cursor_sync(
        self,
        agent_id: str,
        schedule_id: str,
    ) -> ScheduleTriggerCursor | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT cursor_json FROM schedule_trigger_cursors
                WHERE agent_id = ? AND schedule_id = ?
                """,
                (agent_id, schedule_id),
            ).fetchone()
        if row is None:
            return None
        return ScheduleTriggerCursor.model_validate_json(row["cursor_json"])

    def _advance_cursor_sync(
        self,
        agent_id: str,
        schedule_id: str,
        definition_hash: str,
        expected_revision: int,
        scheduled_for: datetime,
        next_fire_at: datetime | None,
    ) -> ScheduleTriggerCursor:
        now = datetime.now(timezone.utc)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT cursor_json FROM schedule_trigger_cursors
                WHERE agent_id = ? AND schedule_id = ?
                """,
                (agent_id, schedule_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise ScheduleCursorConflictError("schedule cursor is absent")
            current = ScheduleTriggerCursor.model_validate_json(
                row["cursor_json"],
            )
            reasons = []
            if current.definition_hash != definition_hash:
                reasons.append("definition")
            if current.revision != expected_revision:
                reasons.append("revision")
            if current.next_fire_at != scheduled_for:
                reasons.append("occurrence")
            if reasons:
                connection.rollback()
                raise ScheduleCursorConflictError(
                    "schedule cursor advance conflicts on: "
                    f"{', '.join(reasons)}",
                )
            updated = ScheduleTriggerCursor.model_validate(
                {
                    **current.model_dump(),
                    "revision": current.revision + 1,
                    "last_fire_at": scheduled_for,
                    "next_fire_at": next_fire_at,
                    "updated_at": now,
                },
            )
            written = connection.execute(
                """
                UPDATE schedule_trigger_cursors SET
                    revision = ?, next_fire_at = ?, cursor_json = ?
                WHERE agent_id = ? AND schedule_id = ?
                  AND definition_hash = ? AND revision = ?
                """,
                (
                    updated.revision,
                    (
                        next_fire_at.isoformat()
                        if next_fire_at is not None
                        else None
                    ),
                    _canonical_json(updated),
                    agent_id,
                    schedule_id,
                    definition_hash,
                    expected_revision,
                ),
            )
            if written.rowcount != 1:
                connection.rollback()
                raise ScheduleCursorConflictError(
                    "schedule cursor changed during advance",
                )
            connection.commit()
        return updated

    def _remove_cursor_sync(
        self,
        agent_id: str,
        schedule_id: str,
        expected_definition_hash: str | None,
        expected_revision: int | None,
    ) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                DELETE FROM schedule_trigger_cursors
                WHERE agent_id = ? AND schedule_id = ?
                  AND (
                    ? IS NULL OR (
                        definition_hash = ? AND revision = ?
                    )
                  )
                """,
                (
                    agent_id,
                    schedule_id,
                    expected_definition_hash,
                    expected_definition_hash,
                    expected_revision,
                ),
            )
            connection.commit()
        return cursor.rowcount > 0

    @staticmethod
    def _cursor_row(cursor: ScheduleTriggerCursor) -> tuple[object, ...]:
        return (
            cursor.agent_id,
            cursor.schedule_id,
            cursor.definition_hash,
            cursor.revision,
            (
                _utc(cursor.next_fire_at).isoformat()
                if cursor.next_fire_at is not None
                else None
            ),
            _canonical_json(cursor),
        )

    def _claimed_for_update(
        self,
        connection: sqlite3.Connection,
        lease_id: UUID,
        owner_id: str,
        expected_revision: int,
        *,
        now: datetime,
    ) -> ScheduleLease:
        row = connection.execute(
            """
            SELECT lease_json FROM schedule_fire_leases
            WHERE lease_id = ?
            """,
            (str(lease_id),),
        ).fetchone()
        if row is None:
            connection.rollback()
            raise ScheduleLeaseNotFoundError(str(lease_id))
        lease = ScheduleLease.model_validate_json(row["lease_json"])
        reasons = []
        if lease.owner_id != owner_id:
            reasons.append("owner")
        if lease.revision != expected_revision:
            reasons.append("revision")
        if lease.status is not ScheduleLeaseStatus.CLAIMED:
            reasons.append("status")
        if lease.expires_at <= now:
            reasons.append("expired")
        if reasons:
            connection.rollback()
            raise ScheduleLeaseConflictError(
                f"schedule lease mutation conflicts on: "
                f"{', '.join(reasons)}",
            )
        return lease

    def _write_lease(
        self,
        connection: sqlite3.Connection,
        lease: ScheduleLease,
    ) -> None:
        cursor = connection.execute(
            """
            UPDATE schedule_fire_leases SET
                owner_id = ?, status = ?, revision = ?,
                expires_at = ?, lease_json = ?
            WHERE lease_id = ?
            """,
            (
                lease.owner_id,
                lease.status.value,
                lease.revision,
                _utc(lease.expires_at).isoformat(),
                _canonical_json(lease),
                str(lease.lease_id),
            ),
        )
        if cursor.rowcount != 1:
            raise ScheduleLeaseNotFoundError(str(lease.lease_id))

    @staticmethod
    def _lease_row(lease: ScheduleLease) -> tuple[object, ...]:
        return (
            str(lease.lease_id),
            str(lease.fire.fire_id),
            lease.fire.agent_id,
            lease.fire.schedule_id,
            lease.fire.idempotency_key,
            lease.owner_id,
            lease.status.value,
            lease.revision,
            _utc(lease.expires_at).isoformat(),
            _canonical_json(lease),
        )

    @staticmethod
    def _validate_owner_and_duration(
        owner_id: str,
        lease_seconds: float,
    ) -> None:
        if not owner_id.strip():
            raise ValueError("schedule lease owner_id must not be empty")
        if lease_seconds <= 0:
            raise ValueError("schedule lease duration must be positive")


__all__ = ["SQLiteSchedulerStore"]
