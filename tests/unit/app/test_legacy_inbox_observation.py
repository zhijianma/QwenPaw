# -*- coding: utf-8 -*-
"""Tests for durable legacy Inbox migration observation gates."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from qwenpaw.app.legacy_inbox_observation import (
    SQLiteLegacyInboxObservationStore,
    assess_legacy_inbox_dual_read,
)


@pytest.mark.asyncio
async def test_clean_stable_scans_survive_restart_and_open_gate(
    tmp_path: Path,
) -> None:
    database = tmp_path / "inbox.db"
    started = datetime(2030, 1, 1, tzinfo=timezone.utc)
    fingerprint = "a" * 64

    for days in (0, 4, 8):
        store = SQLiteLegacyInboxObservationStore(database)
        scan_started = started + timedelta(days=days)
        await store.record_scan(
            agent_id="default",
            started_at=scan_started,
            completed_at=scan_started + timedelta(seconds=1),
            attempted=2,
            migrated=2,
            failed=0,
            source_fingerprint=fingerprint,
            scan_error=None,
        )

    restarted = SQLiteLegacyInboxObservationStore(database)
    observation = await restarted.get(agent_id="default")
    assessment = assess_legacy_inbox_dual_read(
        observation,
        now=started + timedelta(days=8, seconds=2),
    )

    assert observation is not None
    assert observation.scan_count == 3
    assert observation.cumulative_attempted == 6
    assert observation.cumulative_migrated == 6
    assert observation.stable_clean_scans == 3
    assert assessment.can_disable_dual_read
    assert not assessment.blocker_codes
    assert await restarted.get(agent_id="another-agent") is None


@pytest.mark.asyncio
async def test_failure_resets_clean_stability_and_remains_auditable(
    tmp_path: Path,
) -> None:
    store = SQLiteLegacyInboxObservationStore(tmp_path / "inbox.db")
    started = datetime(2030, 1, 1, tzinfo=timezone.utc)
    await store.record_scan(
        agent_id="default",
        started_at=started,
        completed_at=started + timedelta(seconds=1),
        attempted=1,
        migrated=1,
        failed=0,
        source_fingerprint="a" * 64,
        scan_error=None,
    )
    failed = await store.record_scan(
        agent_id="default",
        started_at=started + timedelta(days=8),
        completed_at=started + timedelta(days=8, seconds=1),
        attempted=1,
        migrated=0,
        failed=1,
        source_fingerprint="a" * 64,
        scan_error="ValueError: invalid legacy row",
    )

    assessment = assess_legacy_inbox_dual_read(
        failed,
        now=started + timedelta(days=8, seconds=2),
    )

    assert failed.stable_clean_scans == 0
    assert failed.last_failure_reason == "ValueError: invalid legacy row"
    assert failed.last_failure_at is not None
    assert not assessment.can_disable_dual_read
    assert "latest_scan_failed" in assessment.blocker_codes
    assert "latest_scan_error" in assessment.blocker_codes


def test_missing_observation_fails_closed() -> None:
    assessment = assess_legacy_inbox_dual_read(None)

    assert not assessment.can_disable_dual_read
    assert assessment.blocker_codes == ("observation_missing",)
