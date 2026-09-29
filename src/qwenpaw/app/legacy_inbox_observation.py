# -*- coding: utf-8 -*-
"""Durable observation gate for retiring the legacy Inbox dual read."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from ..kernel.models import utc_now

_MINIMUM_OBSERVATION = timedelta(days=7)
_MINIMUM_STABLE_CLEAN_SCANS = 3


class LegacyInboxMigrationObservation(BaseModel):
    """Persistent, agent-scoped summary of legacy Inbox replay scans."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "qwenpaw.legacy-inbox-observation.v1"
    agent_id: str = Field(min_length=1)
    observation_started_at: AwareDatetime
    last_scan_started_at: AwareDatetime
    last_scan_completed_at: AwareDatetime
    scan_count: int = Field(ge=1)
    cumulative_attempted: int = Field(ge=0)
    cumulative_migrated: int = Field(ge=0)
    cumulative_failed: int = Field(ge=0)
    last_attempted: int = Field(ge=0)
    last_migrated: int = Field(ge=0)
    last_failed: int = Field(ge=0)
    source_fingerprint: str = Field(min_length=64, max_length=64)
    stable_clean_scans: int = Field(ge=0)
    last_scan_error: str | None = Field(default=None, max_length=500)
    last_failure_reason: str | None = Field(default=None, max_length=500)
    last_failure_at: AwareDatetime | None = None


class LegacyInboxDualReadAssessment(BaseModel):
    """Read-only decision for disabling the legacy Inbox dual read."""

    model_config = ConfigDict(extra="forbid")

    can_disable_dual_read: bool
    blocker_codes: tuple[str, ...] = ()
    minimum_observation_seconds: int = Field(ge=0)
    minimum_stable_clean_scans: int = Field(ge=1)
    observation: LegacyInboxMigrationObservation | None = None


def assess_legacy_inbox_dual_read(
    observation: LegacyInboxMigrationObservation | None,
    *,
    now: datetime | None = None,
    minimum_observation: timedelta = _MINIMUM_OBSERVATION,
    minimum_stable_clean_scans: int = _MINIMUM_STABLE_CLEAN_SCANS,
) -> LegacyInboxDualReadAssessment:
    """Evaluate measurable gates without authorizing JSON deletion."""
    if minimum_observation < timedelta(0):
        raise ValueError("minimum observation must be non-negative")
    if minimum_stable_clean_scans < 1:
        raise ValueError("minimum stable clean scans must be positive")
    blockers: list[str] = []
    if observation is None:
        blockers.append("observation_missing")
    else:
        evaluated_at = now or utc_now()
        if (
            evaluated_at - observation.observation_started_at
            < minimum_observation
        ):
            blockers.append("observation_window_incomplete")
        if observation.last_failed:
            blockers.append("latest_scan_failed")
        if observation.last_scan_error is not None:
            blockers.append("latest_scan_error")
        if observation.last_migrated != observation.last_attempted:
            blockers.append("latest_scan_incomplete")
        if observation.stable_clean_scans < minimum_stable_clean_scans:
            blockers.append("stable_clean_scans_incomplete")
    return LegacyInboxDualReadAssessment(
        can_disable_dual_read=not blockers,
        blocker_codes=tuple(blockers),
        minimum_observation_seconds=int(minimum_observation.total_seconds()),
        minimum_stable_clean_scans=minimum_stable_clean_scans,
        observation=observation,
    )


class SQLiteLegacyInboxObservationStore:
    """Persist scan summaries beside, but separate from, Inbox items."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = Path(database_path)
        self._prepare_lock = asyncio.Lock()
        self._prepared = False

    async def get(
        self,
        *,
        agent_id: str,
    ) -> LegacyInboxMigrationObservation | None:
        """Return one agent's observation without exposing other rows."""
        await self._prepare()
        return await asyncio.to_thread(self._get_sync, agent_id)

    async def assess(
        self,
        *,
        agent_id: str,
    ) -> LegacyInboxDualReadAssessment:
        """Return the default seven-day, three-clean-scan assessment."""
        return assess_legacy_inbox_dual_read(
            await self.get(agent_id=agent_id),
        )

    async def record_scan(
        self,
        *,
        agent_id: str,
        started_at: datetime,
        completed_at: datetime,
        attempted: int,
        migrated: int,
        failed: int,
        source_fingerprint: str,
        scan_error: str | None,
    ) -> LegacyInboxMigrationObservation:
        """Atomically append one scan summary to an agent observation."""
        if completed_at < started_at:
            raise ValueError("scan completion precedes scan start")
        if attempted < 0 or migrated < 0 or failed < 0:
            raise ValueError("scan counts must be non-negative")
        if migrated + failed != attempted:
            raise ValueError("scan counts do not reconcile")
        if len(source_fingerprint) != 64:
            raise ValueError("source fingerprint must be sha256 hex")
        await self._prepare()
        return await asyncio.to_thread(
            self._record_scan_sync,
            agent_id,
            started_at,
            completed_at,
            attempted,
            migrated,
            failed,
            source_fingerprint,
            scan_error,
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
        return connection

    def _prepare_sync(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS legacy_inbox_observations (
                    agent_id TEXT PRIMARY KEY,
                    data TEXT NOT NULL
                )
                """,
            )

    def _get_sync(
        self,
        agent_id: str,
    ) -> LegacyInboxMigrationObservation | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT data FROM legacy_inbox_observations
                WHERE agent_id = ?
                """,
                (agent_id,),
            ).fetchone()
        if row is None:
            return None
        return LegacyInboxMigrationObservation.model_validate_json(
            row["data"],
        )

    def _record_scan_sync(  # pylint: disable=too-many-arguments
        self,
        agent_id: str,
        started_at: datetime,
        completed_at: datetime,
        attempted: int,
        migrated: int,
        failed: int,
        source_fingerprint: str,
        scan_error: str | None,
    ) -> LegacyInboxMigrationObservation:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT data FROM legacy_inbox_observations
                WHERE agent_id = ?
                """,
                (agent_id,),
            ).fetchone()
            existing = (
                LegacyInboxMigrationObservation.model_validate_json(
                    row["data"],
                )
                if row is not None
                else None
            )
            clean = (
                failed == 0 and migrated == attempted and scan_error is None
            )
            stable_clean_scans = 0
            if clean:
                stable_clean_scans = 1
                if (
                    existing is not None
                    and existing.source_fingerprint == source_fingerprint
                ):
                    stable_clean_scans += existing.stable_clean_scans
            observation = LegacyInboxMigrationObservation(
                agent_id=agent_id,
                observation_started_at=(
                    existing.observation_started_at
                    if existing is not None
                    else started_at
                ),
                last_scan_started_at=started_at,
                last_scan_completed_at=completed_at,
                scan_count=(existing.scan_count + 1 if existing else 1),
                cumulative_attempted=(
                    (existing.cumulative_attempted if existing else 0)
                    + attempted
                ),
                cumulative_migrated=(
                    (existing.cumulative_migrated if existing else 0)
                    + migrated
                ),
                cumulative_failed=(
                    (existing.cumulative_failed if existing else 0) + failed
                ),
                last_attempted=attempted,
                last_migrated=migrated,
                last_failed=failed,
                source_fingerprint=source_fingerprint,
                stable_clean_scans=stable_clean_scans,
                last_scan_error=scan_error,
                last_failure_reason=(
                    scan_error
                    if scan_error is not None
                    else (existing.last_failure_reason if existing else None)
                ),
                last_failure_at=(
                    completed_at
                    if scan_error is not None
                    else (existing.last_failure_at if existing else None)
                ),
            )
            connection.execute(
                """
                INSERT INTO legacy_inbox_observations(agent_id, data)
                VALUES (?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET data = excluded.data
                """,
                (agent_id, observation.model_dump_json()),
            )
            connection.commit()
        return observation


__all__ = [
    "LegacyInboxDualReadAssessment",
    "LegacyInboxMigrationObservation",
    "SQLiteLegacyInboxObservationStore",
    "assess_legacy_inbox_dual_read",
]
