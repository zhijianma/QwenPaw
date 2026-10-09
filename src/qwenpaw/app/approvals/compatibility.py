# -*- coding: utf-8 -*-
"""Persistent removal evidence for the legacy approval waiter."""

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

LegacyApprovalFallbackReason = Literal[
    "interaction_service_missing",
    "chat_identity_missing",
    "invocation_identity_missing",
]

_MINIMUM_ZERO_USAGE_WINDOW = timedelta(days=7)
logger = logging.getLogger(__name__)


class LegacyApprovalCompatibilityHit(BaseModel):
    """Content-free aggregate for one legacy-only approval path."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-zA-Z0-9_.-]+$",
    )
    reason: LegacyApprovalFallbackReason
    hit_count: int = Field(ge=1)
    first_seen_at: AwareDatetime
    last_seen_at: AwareDatetime


class _LegacyApprovalCompatibilityEvent(BaseModel):
    """Validated internal observation before persistence."""

    model_config = ConfigDict(extra="forbid")

    observation_id: str = Field(min_length=1, max_length=200)
    source: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-zA-Z0-9_.-]+$",
    )
    reason: LegacyApprovalFallbackReason
    observed_at: AwareDatetime


class LegacyApprovalCompatibilityReport(BaseModel):
    """Agent-scoped removal evidence without approval content."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "qwenpaw.legacy-approval-compatibility.v1"
    code: str = "legacy_approval_waiter"
    reason: str = (
        "Legacy-only approval waits are process-local and cannot recover "
        "after runtime replacement."
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
    hits: tuple[LegacyApprovalCompatibilityHit, ...] = ()
    replacement_contract: str = "InteractionService"
    removal_gates: tuple[str, ...] = (
        "legacy_approval_usage_zero_for_7_days",
        "legacy_approval_callers_confirmed_migrated",
    )
    removal_authorized: bool = False
    removal_blockers: tuple[str, ...]


class SQLiteLegacyApprovalCompatibilityStore:
    """Observe legitimate legacy-only approval waits idempotently."""

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
        source: str,
        reason: LegacyApprovalFallbackReason,
        observed_at: datetime | None = None,
    ) -> bool:
        """Record one actual fallback once and reset its zero-use window."""
        event = _LegacyApprovalCompatibilityEvent(
            observation_id=observation_id,
            source=source,
            reason=reason,
            observed_at=observed_at or utc_now(),
        )
        return await asyncio.to_thread(self._record_sync, event)

    async def report(
        self,
        *,
        now: datetime | None = None,
    ) -> LegacyApprovalCompatibilityReport:
        """Return read-only migration evidence for this workspace."""
        return await asyncio.to_thread(self._report_sync, now or utc_now())

    def _start_sync(self, started_at: datetime) -> None:
        self._prepare()
        timestamp = started_at.isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO legacy_approval_observation_v1 (
                    agent_id, observation_started_at,
                    zero_usage_started_at, last_legacy_hit_at
                ) VALUES (?, ?, ?, NULL)
                """,
                (self._agent_id, timestamp, timestamp),
            )

    def _record_sync(
        self,
        event: _LegacyApprovalCompatibilityEvent,
    ) -> bool:
        self._prepare()
        timestamp = event.observed_at.isoformat()
        digest = hashlib.sha256(
            event.observation_id.encode("utf-8"),
        ).hexdigest()
        with self._connect() as connection:
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO legacy_approval_events_v1 (
                    agent_id, observation_id, source, reason, observed_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    self._agent_id,
                    digest,
                    event.source,
                    event.reason,
                    timestamp,
                ),
            ).rowcount
            if not inserted:
                return False
            connection.execute(
                """
                INSERT INTO legacy_approval_observation_v1 (
                    agent_id, observation_started_at,
                    zero_usage_started_at, last_legacy_hit_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    zero_usage_started_at = excluded.zero_usage_started_at,
                    last_legacy_hit_at = excluded.last_legacy_hit_at
                """,
                (self._agent_id, timestamp, timestamp, timestamp),
            )
            connection.execute(
                """
                INSERT INTO legacy_approval_summary_v1 (
                    agent_id, source, reason, hit_count,
                    first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(agent_id, source, reason) DO UPDATE SET
                    hit_count = hit_count + 1,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    self._agent_id,
                    event.source,
                    event.reason,
                    timestamp,
                    timestamp,
                ),
            )
        return True

    def _report_sync(
        self,
        evaluated_at: datetime,
    ) -> LegacyApprovalCompatibilityReport:
        self._prepare()
        with self._connect() as connection:
            observation = connection.execute(
                """
                SELECT observation_started_at, zero_usage_started_at,
                       last_legacy_hit_at
                FROM legacy_approval_observation_v1
                WHERE agent_id = ?
                """,
                (self._agent_id,),
            ).fetchone()
            rows = connection.execute(
                """
                SELECT source, reason, hit_count,
                       first_seen_at, last_seen_at
                FROM legacy_approval_summary_v1
                WHERE agent_id = ?
                ORDER BY source, reason
                """,
                (self._agent_id,),
            ).fetchall()
        hits = tuple(
            LegacyApprovalCompatibilityHit(
                source=str(row["source"]),
                reason=str(row["reason"]),
                hit_count=int(row["hit_count"]),
                first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
                last_seen_at=datetime.fromisoformat(row["last_seen_at"]),
            )
            for row in rows
        )
        started_at = (
            datetime.fromisoformat(observation["observation_started_at"])
            if observation is not None
            else None
        )
        zero_started_at = (
            datetime.fromisoformat(observation["zero_usage_started_at"])
            if observation is not None
            else None
        )
        last_hit_at = (
            datetime.fromisoformat(observation["last_legacy_hit_at"])
            if observation is not None
            and observation["last_legacy_hit_at"] is not None
            else None
        )
        elapsed = (
            max(
                evaluated_at - zero_started_at,
                timedelta(0),
            )
            if zero_started_at is not None
            else timedelta(0)
        )
        complete = elapsed >= _MINIMUM_ZERO_USAGE_WINDOW
        minimum_seconds = int(_MINIMUM_ZERO_USAGE_WINDOW.total_seconds())
        elapsed_seconds = int(elapsed.total_seconds())
        blockers = ["legacy_approval_callers_not_confirmed_migrated"]
        if not complete:
            blockers.insert(0, "zero_usage_observation_incomplete")
        return LegacyApprovalCompatibilityReport(
            agent_id=self._agent_id,
            evaluated_at=evaluated_at,
            observation_started_at=started_at,
            zero_usage_started_at=zero_started_at,
            last_legacy_hit_at=last_hit_at,
            minimum_zero_usage_seconds=minimum_seconds,
            zero_usage_seconds=elapsed_seconds,
            zero_usage_seconds_remaining=max(
                minimum_seconds - elapsed_seconds,
                0,
            ),
            zero_usage_window_complete=complete,
            total_hits=sum(hit.hit_count for hit in hits),
            hits=hits,
            removal_blockers=tuple(blockers),
        )

    def _prepare(self) -> None:
        if self._prepared:
            return
        with self._prepare_lock:
            if self._prepared:
                return
            self._database_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS
                    legacy_approval_observation_v1 (
                        agent_id TEXT PRIMARY KEY,
                        observation_started_at TEXT NOT NULL,
                        zero_usage_started_at TEXT NOT NULL,
                        last_legacy_hit_at TEXT
                    );
                    CREATE TABLE IF NOT EXISTS legacy_approval_events_v1 (
                        agent_id TEXT NOT NULL,
                        observation_id TEXT NOT NULL,
                        source TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        observed_at TEXT NOT NULL,
                        PRIMARY KEY(agent_id, observation_id)
                    );
                    CREATE TABLE IF NOT EXISTS legacy_approval_summary_v1 (
                        agent_id TEXT NOT NULL,
                        source TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        hit_count INTEGER NOT NULL CHECK(hit_count > 0),
                        first_seen_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        PRIMARY KEY(agent_id, source, reason)
                    );
                    """,
                )
            self._prepared = True

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        return connection


async def observe_legacy_approval(
    store: object,
    *,
    observation_id: str,
    source: str,
    reason: LegacyApprovalFallbackReason,
) -> bool:
    """Best-effort observation that never changes approval behavior."""
    if not isinstance(store, SQLiteLegacyApprovalCompatibilityStore):
        return False
    try:
        return await asyncio.wait_for(
            store.record(
                observation_id=observation_id,
                source=source,
                reason=reason,
            ),
            timeout=0.25,
        )
    except Exception:  # pylint: disable=broad-except
        logger.warning(
            "legacy approval compatibility observation failed",
            exc_info=True,
        )
        return False


__all__ = [
    "LegacyApprovalCompatibilityHit",
    "LegacyApprovalCompatibilityReport",
    "LegacyApprovalFallbackReason",
    "SQLiteLegacyApprovalCompatibilityStore",
    "observe_legacy_approval",
]
