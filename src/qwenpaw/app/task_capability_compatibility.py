# -*- coding: utf-8 -*-
"""Observation for legacy Task capability identifier resolution."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from ..kernel.models import utc_now

_MINIMUM_ZERO_USAGE_WINDOW = timedelta(days=7)


class TaskCapabilityAlias(BaseModel):
    """One supported legacy-to-canonical Task capability mapping."""

    model_config = ConfigDict(extra="forbid")

    legacy_id: str = Field(min_length=1)
    canonical_id: str = Field(min_length=1)
    slot: str = Field(min_length=1)


TASK_CAPABILITY_ALIASES: Mapping[str, TaskCapabilityAlias] = {
    alias.legacy_id: alias
    for alias in (
        TaskCapabilityAlias(
            legacy_id="qwenpaw.system.basic-planner",
            canonical_id="qwenpaw.system.tasks.basic-planner",
            slot="planner",
        ),
        TaskCapabilityAlias(
            legacy_id="qwenpaw.system.console-agent",
            canonical_id="qwenpaw.system.tasks.console-agent",
            slot="runner",
        ),
        TaskCapabilityAlias(
            legacy_id="qwenpaw.system.default-strategy",
            canonical_id="qwenpaw.system.tasks.default-strategy",
            slot="strategy",
        ),
        TaskCapabilityAlias(
            legacy_id="qwenpaw.system.safe-artifact-renderer",
            canonical_id=("qwenpaw.system.tasks.safe-artifact-renderer"),
            slot="artifact.renderer",
        ),
        TaskCapabilityAlias(
            legacy_id="qwenpaw.system.coding-strategy",
            canonical_id="qwenpaw.system.tasks.coding-strategy",
            slot="strategy",
        ),
        TaskCapabilityAlias(
            legacy_id="qwenpaw.system.goal-strategy",
            canonical_id="qwenpaw.system.tasks.goal-strategy",
            slot="strategy",
        ),
        TaskCapabilityAlias(
            legacy_id="qwenpaw.system.mission-strategy",
            canonical_id="qwenpaw.system.tasks.mission-strategy",
            slot="strategy",
        ),
    )
}


class TaskCapabilityCompatibilityHit(BaseModel):
    """Persistent usage summary for one legacy Task capability ID."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "qwenpaw.task-capability-compatibility-hit.v1"
    agent_id: str = Field(min_length=1)
    legacy_id: str = Field(min_length=1)
    canonical_id: str = Field(min_length=1)
    slot: str = Field(min_length=1)
    hit_count: int = Field(ge=1)
    first_seen_at: AwareDatetime
    last_seen_at: AwareDatetime
    migration_advice: str = Field(min_length=1)


class TaskCapabilityCompatibilityReport(BaseModel):
    """Agent-scoped, read-only compatibility diagnostics."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "qwenpaw.task-capability-compatibility-report.v2"
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
    observed_aliases: tuple[TaskCapabilityCompatibilityHit, ...] = ()
    removal_authorized: bool = False
    removal_blockers: tuple[str, ...]


def canonical_task_capability_alias(
    capability_id: str,
) -> TaskCapabilityAlias | None:
    """Return the exact compatibility mapping for a legacy ID."""
    return TASK_CAPABILITY_ALIASES.get(capability_id)


class SQLiteTaskCapabilityCompatibilityStore:
    """Atomically record legacy Task capability resolution hits."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = Path(database_path)
        self._prepare_lock = threading.Lock()
        self._prepared = False

    async def start_observation(
        self,
        *,
        agent_id: str,
        started_at: datetime | None = None,
    ) -> None:
        """Start an idempotent zero-usage window outside read requests."""
        await asyncio.to_thread(
            self._start_observation_sync,
            agent_id,
            started_at or utc_now(),
        )

    def record(
        self,
        *,
        agent_id: str,
        alias: TaskCapabilityAlias,
        observed_at: datetime | None = None,
    ) -> None:
        """Record one actual alias resolution without changing selection."""
        if not agent_id.strip():
            raise ValueError("agent_id cannot be empty")
        self._prepare()
        timestamp = (observed_at or utc_now()).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO task_capability_compatibility_observation (
                    agent_id,
                    observation_started_at,
                    zero_usage_started_at,
                    last_legacy_hit_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    zero_usage_started_at = excluded.zero_usage_started_at,
                    last_legacy_hit_at = excluded.last_legacy_hit_at
                """,
                (agent_id, timestamp, timestamp, timestamp),
            )
            connection.execute(
                """
                INSERT INTO task_capability_compatibility_hits (
                    agent_id,
                    legacy_id,
                    canonical_id,
                    slot,
                    hit_count,
                    first_seen_at,
                    last_seen_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(agent_id, legacy_id) DO UPDATE SET
                    canonical_id = excluded.canonical_id,
                    slot = excluded.slot,
                    hit_count = hit_count + 1,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    agent_id,
                    alias.legacy_id,
                    alias.canonical_id,
                    alias.slot,
                    timestamp,
                    timestamp,
                ),
            )

    async def report(
        self,
        *,
        agent_id: str,
        now: datetime | None = None,
    ) -> TaskCapabilityCompatibilityReport:
        """Return structured migration diagnostics for one agent."""
        return await asyncio.to_thread(
            self._report_sync,
            agent_id,
            now or utc_now(),
        )

    def _report_sync(
        self,
        agent_id: str,
        evaluated_at: datetime,
    ) -> TaskCapabilityCompatibilityReport:
        if not agent_id.strip():
            raise ValueError("agent_id cannot be empty")
        self._prepare()
        with self._connect() as connection:
            observation = connection.execute(
                """
                SELECT observation_started_at, zero_usage_started_at,
                       last_legacy_hit_at
                FROM task_capability_compatibility_observation
                WHERE agent_id = ?
                """,
                (agent_id,),
            ).fetchone()
            rows = connection.execute(
                """
                SELECT agent_id, legacy_id, canonical_id, slot, hit_count,
                       first_seen_at, last_seen_at
                FROM task_capability_compatibility_hits
                WHERE agent_id = ?
                ORDER BY legacy_id
                """,
                (agent_id,),
            ).fetchall()
        hits = tuple(
            TaskCapabilityCompatibilityHit(
                agent_id=str(row["agent_id"]),
                legacy_id=str(row["legacy_id"]),
                canonical_id=str(row["canonical_id"]),
                slot=str(row["slot"]),
                hit_count=int(row["hit_count"]),
                first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
                last_seen_at=datetime.fromisoformat(row["last_seen_at"]),
                migration_advice=(
                    f"Replace '{row['legacy_id']}' with "
                    f"'{row['canonical_id']}' in persisted Task or Profile "
                    f"configuration."
                ),
            )
            for row in rows
        )
        observation_started_at = (
            datetime.fromisoformat(observation["observation_started_at"])
            if observation is not None
            else None
        )
        zero_usage_started_at = (
            datetime.fromisoformat(observation["zero_usage_started_at"])
            if observation is not None
            else None
        )
        last_legacy_hit_at = (
            datetime.fromisoformat(observation["last_legacy_hit_at"])
            if observation is not None
            and observation["last_legacy_hit_at"] is not None
            else None
        )
        zero_usage = (
            max(evaluated_at - zero_usage_started_at, timedelta(0))
            if zero_usage_started_at is not None
            else timedelta(0)
        )
        remaining = max(
            _MINIMUM_ZERO_USAGE_WINDOW - zero_usage,
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
        blockers.append("persisted_references_not_migrated")
        return TaskCapabilityCompatibilityReport(
            agent_id=agent_id,
            evaluated_at=evaluated_at,
            observation_started_at=observation_started_at,
            zero_usage_started_at=zero_usage_started_at,
            last_legacy_hit_at=last_legacy_hit_at,
            minimum_zero_usage_seconds=int(
                _MINIMUM_ZERO_USAGE_WINDOW.total_seconds(),
            ),
            zero_usage_seconds=int(zero_usage.total_seconds()),
            zero_usage_seconds_remaining=int(remaining.total_seconds()),
            zero_usage_window_complete=window_complete,
            total_hits=sum(hit.hit_count for hit in hits),
            observed_aliases=hits,
            removal_blockers=tuple(blockers),
        )

    def _start_observation_sync(
        self,
        agent_id: str,
        started_at: datetime,
    ) -> None:
        if not agent_id.strip():
            raise ValueError("agent_id cannot be empty")
        self._prepare()
        timestamp = started_at.isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO
                task_capability_compatibility_observation (
                    agent_id,
                    observation_started_at,
                    zero_usage_started_at,
                    last_legacy_hit_at
                ) VALUES (?, ?, ?, NULL)
                """,
                (agent_id, timestamp, timestamp),
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
                    task_capability_compatibility_observation (
                        agent_id TEXT PRIMARY KEY,
                        observation_started_at TEXT NOT NULL,
                        zero_usage_started_at TEXT NOT NULL,
                        last_legacy_hit_at TEXT
                    )
                    """,
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS
                    task_capability_compatibility_hits (
                        agent_id TEXT NOT NULL,
                        legacy_id TEXT NOT NULL,
                        canonical_id TEXT NOT NULL,
                        slot TEXT NOT NULL,
                        hit_count INTEGER NOT NULL CHECK(hit_count > 0),
                        first_seen_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        PRIMARY KEY(agent_id, legacy_id)
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
    "SQLiteTaskCapabilityCompatibilityStore",
    "TASK_CAPABILITY_ALIASES",
    "TaskCapabilityAlias",
    "TaskCapabilityCompatibilityHit",
    "TaskCapabilityCompatibilityReport",
    "canonical_task_capability_alias",
]
