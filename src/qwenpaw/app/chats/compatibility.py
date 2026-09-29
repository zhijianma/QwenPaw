# -*- coding: utf-8 -*-
"""Structured observation for Chat compatibility boundaries."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from ...kernel.models import utc_now


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


__all__ = [
    "ExternalQueueCompatibilityReport",
    "ExternalQueueFallbackObservation",
    "ExternalQueueFallbackRequest",
    "SQLiteExternalQueueCompatibilityStore",
]
