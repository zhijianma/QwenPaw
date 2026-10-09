# -*- coding: utf-8 -*-
"""Structured observation for Chat compatibility boundaries."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from ...kernel.models import utc_now

_MINIMUM_STOP_ZERO_USAGE_WINDOW = timedelta(days=7)
logger = logging.getLogger(__name__)


class ExternalQueueFallbackRequest(BaseModel):
    """Idempotent notice that an external backend used the host queue."""

    model_config = ConfigDict(extra="forbid")

    observation_id: str = Field(min_length=1, max_length=200)
    backend_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-zA-Z0-9_.-]+$",
    )


class ExternalQueueFallbackObservation(BaseModel):
    """Persistent summary for one external backend compatibility path."""

    model_config = ConfigDict(extra="forbid")

    code: str = "external_backend_legacy_queue"
    agent_id: str = Field(min_length=1)
    backend_id: str = Field(min_length=1)
    hit_count: int = Field(ge=1)
    first_seen_at: AwareDatetime
    last_seen_at: AwareDatetime
    replacement_capability: str = "conversation.queue"
    reason: str = (
        "The selected backend does not expose the QwenPaw Conversation and "
        "Queue control-plane contract."
    )
    removal_gates: tuple[str, ...] = (
        "backend_publishes_conversation_queue_capability",
        "server_queue_isolation_and_recovery_suite_passes",
        "legacy_queue_usage_observation_reaches_zero",
    )


class ExternalQueueCompatibilityReport(BaseModel):
    """Agent-scoped report without message or session content."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "qwenpaw.external-queue-compatibility.v1"
    agent_id: str = Field(min_length=1)
    total_hits: int = Field(ge=0)
    backends: tuple[ExternalQueueFallbackObservation, ...] = ()
    removal_authorized: bool = False
    removal_blockers: tuple[str, ...] = (
        "external_backend_queue_capability_unavailable",
        "zero_usage_observation_not_established",
    )


class SQLiteExternalQueueCompatibilityStore:
    """Persist deduplicated external queue fallback observations."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = Path(database_path)
        self._prepare_lock = threading.Lock()
        self._prepared = False

    async def record(
        self,
        *,
        agent_id: str,
        request: ExternalQueueFallbackRequest,
        observed_at: datetime | None = None,
    ) -> bool:
        """Record one fallback once and return whether it was new."""
        return await asyncio.to_thread(
            self._record_sync,
            agent_id,
            request,
            observed_at or utc_now(),
        )

    async def report(
        self,
        *,
        agent_id: str,
    ) -> ExternalQueueCompatibilityReport:
        """Return one agent's aggregate fallback observations."""
        return await asyncio.to_thread(self._report_sync, agent_id)

    def _record_sync(
        self,
        agent_id: str,
        request: ExternalQueueFallbackRequest,
        observed_at: datetime,
    ) -> bool:
        if not agent_id.strip():
            raise ValueError("agent_id cannot be empty")
        self._prepare()
        timestamp = observed_at.isoformat()
        with self._connect() as connection:
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO external_queue_fallback_events_v1 (
                    observation_id,
                    agent_id,
                    backend_id,
                    observed_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    request.observation_id,
                    agent_id,
                    request.backend_id,
                    timestamp,
                ),
            ).rowcount
            if inserted:
                connection.execute(
                    """
                    INSERT INTO external_queue_fallback_summary (
                        agent_id,
                        backend_id,
                        hit_count,
                        first_seen_at,
                        last_seen_at
                    ) VALUES (?, ?, 1, ?, ?)
                    ON CONFLICT(agent_id, backend_id) DO UPDATE SET
                        hit_count = hit_count + 1,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (agent_id, request.backend_id, timestamp, timestamp),
                )
        return bool(inserted)

    def _report_sync(
        self,
        agent_id: str,
    ) -> ExternalQueueCompatibilityReport:
        if not agent_id.strip():
            raise ValueError("agent_id cannot be empty")
        self._prepare()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT agent_id, backend_id, hit_count,
                       first_seen_at, last_seen_at
                FROM external_queue_fallback_summary
                WHERE agent_id = ?
                ORDER BY backend_id
                """,
                (agent_id,),
            ).fetchall()
        backends = tuple(
            ExternalQueueFallbackObservation(
                agent_id=str(row["agent_id"]),
                backend_id=str(row["backend_id"]),
                hit_count=int(row["hit_count"]),
                first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
                last_seen_at=datetime.fromisoformat(row["last_seen_at"]),
            )
            for row in rows
        )
        return ExternalQueueCompatibilityReport(
            agent_id=agent_id,
            total_hits=sum(item.hit_count for item in backends),
            backends=backends,
        )

    def _prepare(self) -> None:
        if self._prepared:
            return
        with self._prepare_lock:
            if self._prepared:
                return
            self._database_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS
                    external_queue_fallback_events_v1 (
                        observation_id TEXT NOT NULL,
                        agent_id TEXT NOT NULL,
                        backend_id TEXT NOT NULL,
                        observed_at TEXT NOT NULL,
                        PRIMARY KEY(agent_id, observation_id)
                    )
                    """,
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS
                    external_queue_fallback_summary (
                        agent_id TEXT NOT NULL,
                        backend_id TEXT NOT NULL,
                        hit_count INTEGER NOT NULL CHECK(hit_count > 0),
                        first_seen_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        PRIMARY KEY(agent_id, backend_id)
                    )
                    """,
                )
            self._prepared = True

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database_path,
            timeout=10.0,
        )
        connection.row_factory = sqlite3.Row
        return connection


LegacyStopEntrypoint = Literal["console.stop_api", "channel.slash_stop"]
LegacyStopDisposition = Literal[
    "os_interrupt",
    "compatibility_cancelled",
    "no_active_invocation",
    "chat_not_found",
]


class LegacyStopCompatibilityHit(BaseModel):
    """Content-free aggregate for one legacy stop outcome."""

    model_config = ConfigDict(extra="forbid")

    entrypoint: LegacyStopEntrypoint
    disposition: LegacyStopDisposition
    hit_count: int = Field(ge=1)
    first_seen_at: AwareDatetime
    last_seen_at: AwareDatetime


class _LegacyStopCompatibilityEvent(BaseModel):
    """Validated internal event before it reaches the SQLite adapter."""

    model_config = ConfigDict(extra="forbid")

    observation_id: str = Field(min_length=1, max_length=200)
    entrypoint: LegacyStopEntrypoint
    disposition: LegacyStopDisposition
    observed_at: AwareDatetime


class LegacyStopCompatibilityReport(BaseModel):
    """Agent-scoped removal evidence for legacy stop entrypoints."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "qwenpaw.legacy-stop-compatibility.v1"
    code: str = "legacy_stop_entrypoint"
    reason: str = (
        "Legacy stop entrypoints cannot express whether queued work should "
        "be preserved or cleared."
    )
    agent_id: str = Field(min_length=1)
    evaluated_at: AwareDatetime
    observation_started_at: AwareDatetime | None = None
    zero_usage_started_at: AwareDatetime | None = None
    last_legacy_hit_at: AwareDatetime | None = None
    minimum_zero_usage_seconds: int = Field(ge=0)
    zero_usage_seconds: int = Field(ge=0)
    zero_usage_seconds_remaining: int = Field(ge=0)
    zero_usage_window_complete: bool
    total_hits: int = Field(ge=0)
    hits: tuple[LegacyStopCompatibilityHit, ...] = ()
    replacement_endpoints: tuple[str, ...] = (
        "POST /api/chats/{chat_id}/control/interrupt",
        "POST /api/chats/{chat_id}/control/stop-and-clear",
    )
    removal_gates: tuple[str, ...] = (
        "legacy_stop_usage_zero_for_7_days",
        "legacy_clients_confirmed_migrated",
    )
    removal_authorized: bool = False
    removal_blockers: tuple[str, ...]


class SQLiteLegacyStopCompatibilityStore:
    """Observe legacy stop use without creating another control fact."""

    def __init__(self, database_path: Path, agent_id: str) -> None:
        if not agent_id.strip():
            raise ValueError("agent_id cannot be empty")
        self._database_path = Path(database_path)
        self._agent_id = agent_id
        self._prepare_lock = threading.Lock()
        self._prepared = False

    async def start(
        self,
        *,
        started_at: datetime | None = None,
    ) -> None:
        """Start an idempotent zero-use window at workspace startup."""
        await asyncio.to_thread(
            self._start_sync,
            started_at or utc_now(),
        )

    async def record(
        self,
        *,
        observation_id: str,
        entrypoint: LegacyStopEntrypoint,
        disposition: LegacyStopDisposition,
        observed_at: datetime | None = None,
    ) -> bool:
        """Record one actual legacy entrypoint invocation idempotently."""
        event = _LegacyStopCompatibilityEvent(
            observation_id=observation_id,
            entrypoint=entrypoint,
            disposition=disposition,
            observed_at=observed_at or utc_now(),
        )
        return await asyncio.to_thread(
            self._record_sync,
            event,
        )

    async def report(
        self,
        *,
        now: datetime | None = None,
    ) -> LegacyStopCompatibilityReport:
        """Return read-only migration evidence for this workspace."""
        return await asyncio.to_thread(self._report_sync, now or utc_now())

    def _start_sync(self, started_at: datetime) -> None:
        self._prepare()
        timestamp = started_at.isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO legacy_stop_observation_v1 (
                    agent_id,
                    observation_started_at,
                    zero_usage_started_at,
                    last_legacy_hit_at
                ) VALUES (?, ?, ?, NULL)
                """,
                (self._agent_id, timestamp, timestamp),
            )

    def _record_sync(
        self,
        event: _LegacyStopCompatibilityEvent,
    ) -> bool:
        self._prepare()
        timestamp = event.observed_at.isoformat()
        observation_digest = hashlib.sha256(
            event.observation_id.encode("utf-8"),
        ).hexdigest()
        with self._connect() as connection:
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO legacy_stop_events_v1 (
                    agent_id, observation_id, entrypoint,
                    disposition, observed_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    self._agent_id,
                    observation_digest,
                    event.entrypoint,
                    event.disposition,
                    timestamp,
                ),
            ).rowcount
            if not inserted:
                return False
            connection.execute(
                """
                INSERT INTO legacy_stop_observation_v1 (
                    agent_id,
                    observation_started_at,
                    zero_usage_started_at,
                    last_legacy_hit_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    zero_usage_started_at = excluded.zero_usage_started_at,
                    last_legacy_hit_at = excluded.last_legacy_hit_at
                """,
                (self._agent_id, timestamp, timestamp, timestamp),
            )
            connection.execute(
                """
                INSERT INTO legacy_stop_summary_v1 (
                    agent_id, entrypoint, disposition, hit_count,
                    first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(agent_id, entrypoint, disposition) DO UPDATE SET
                    hit_count = hit_count + 1,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    self._agent_id,
                    event.entrypoint,
                    event.disposition,
                    timestamp,
                    timestamp,
                ),
            )
        return True

    def _report_sync(
        self,
        evaluated_at: datetime,
    ) -> LegacyStopCompatibilityReport:
        self._prepare()
        with self._connect() as connection:
            observation = connection.execute(
                """
                SELECT observation_started_at, zero_usage_started_at,
                       last_legacy_hit_at
                FROM legacy_stop_observation_v1
                WHERE agent_id = ?
                """,
                (self._agent_id,),
            ).fetchone()
            rows = connection.execute(
                """
                SELECT entrypoint, disposition, hit_count,
                       first_seen_at, last_seen_at
                FROM legacy_stop_summary_v1
                WHERE agent_id = ?
                ORDER BY entrypoint, disposition
                """,
                (self._agent_id,),
            ).fetchall()
        hits = tuple(
            LegacyStopCompatibilityHit(
                entrypoint=str(row["entrypoint"]),
                disposition=str(row["disposition"]),
                hit_count=int(row["hit_count"]),
                first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
                last_seen_at=datetime.fromisoformat(row["last_seen_at"]),
            )
            for row in rows
        )
        observation_started_at = self._timestamp(
            observation,
            "observation_started_at",
        )
        zero_usage_started_at = self._timestamp(
            observation,
            "zero_usage_started_at",
        )
        last_legacy_hit_at = self._timestamp(
            observation,
            "last_legacy_hit_at",
        )
        zero_usage = (
            max(evaluated_at - zero_usage_started_at, timedelta(0))
            if zero_usage_started_at is not None
            else timedelta(0)
        )
        remaining = max(
            _MINIMUM_STOP_ZERO_USAGE_WINDOW - zero_usage,
            timedelta(0),
        )
        window_complete = (
            zero_usage_started_at is not None and remaining == timedelta(0)
        )
        blockers = []
        if observation is None:
            blockers.append("zero_usage_observation_not_started")
        elif not window_complete:
            blockers.append("zero_usage_observation_window_incomplete")
        blockers.append("legacy_clients_not_confirmed_migrated")
        return LegacyStopCompatibilityReport(
            agent_id=self._agent_id,
            evaluated_at=evaluated_at,
            observation_started_at=observation_started_at,
            zero_usage_started_at=zero_usage_started_at,
            last_legacy_hit_at=last_legacy_hit_at,
            minimum_zero_usage_seconds=int(
                _MINIMUM_STOP_ZERO_USAGE_WINDOW.total_seconds(),
            ),
            zero_usage_seconds=int(zero_usage.total_seconds()),
            zero_usage_seconds_remaining=int(remaining.total_seconds()),
            zero_usage_window_complete=window_complete,
            total_hits=sum(hit.hit_count for hit in hits),
            hits=hits,
            removal_blockers=tuple(blockers),
        )

    @staticmethod
    def _timestamp(
        row: sqlite3.Row | None,
        column: str,
    ) -> datetime | None:
        if row is None or row[column] is None:
            return None
        return datetime.fromisoformat(str(row[column]))

    def _prepare(self) -> None:
        if self._prepared:
            return
        with self._prepare_lock:
            if self._prepared:
                return
            self._database_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS legacy_stop_observation_v1 (
                        agent_id TEXT PRIMARY KEY,
                        observation_started_at TEXT NOT NULL,
                        zero_usage_started_at TEXT NOT NULL,
                        last_legacy_hit_at TEXT
                    )
                    """,
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS legacy_stop_events_v1 (
                        agent_id TEXT NOT NULL,
                        observation_id TEXT NOT NULL,
                        entrypoint TEXT NOT NULL,
                        disposition TEXT NOT NULL,
                        observed_at TEXT NOT NULL,
                        PRIMARY KEY(agent_id, observation_id)
                    )
                    """,
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS legacy_stop_summary_v1 (
                        agent_id TEXT NOT NULL,
                        entrypoint TEXT NOT NULL,
                        disposition TEXT NOT NULL,
                        hit_count INTEGER NOT NULL CHECK(hit_count > 0),
                        first_seen_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        PRIMARY KEY(agent_id, entrypoint, disposition)
                    )
                    """,
                )
            self._prepared = True

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database_path,
            timeout=10.0,
        )
        connection.row_factory = sqlite3.Row
        return connection


async def observe_legacy_stop(
    workspace: object,
    *,
    observation_id: str,
    entrypoint: LegacyStopEntrypoint,
    disposition: LegacyStopDisposition,
) -> bool:
    """Best-effort observation that never blocks an urgent stop command."""
    store = getattr(workspace, "legacy_stop_compatibility", None)
    if not isinstance(store, SQLiteLegacyStopCompatibilityStore):
        return False
    try:
        return await asyncio.wait_for(
            store.record(
                observation_id=observation_id,
                entrypoint=entrypoint,
                disposition=disposition,
            ),
            timeout=0.25,
        )
    except Exception:  # pylint: disable=broad-except
        logger.warning(
            "legacy stop compatibility observation failed",
            exc_info=True,
        )
        return False


__all__ = [
    "ExternalQueueCompatibilityReport",
    "ExternalQueueFallbackObservation",
    "ExternalQueueFallbackRequest",
    "LegacyStopCompatibilityHit",
    "LegacyStopCompatibilityReport",
    "LegacyStopDisposition",
    "LegacyStopEntrypoint",
    "SQLiteExternalQueueCompatibilityStore",
    "SQLiteLegacyStopCompatibilityStore",
    "observe_legacy_stop",
]
