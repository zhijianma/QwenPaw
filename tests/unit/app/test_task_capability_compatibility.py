# -*- coding: utf-8 -*-
"""Tests for legacy Task capability compatibility observation."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from qwenpaw.app.task_capability_compatibility import (
    SQLiteTaskCapabilityCompatibilityStore,
    canonical_task_capability_alias,
)
from qwenpaw.app.task_runtime import canonical_task_capability_id


@pytest.mark.asyncio
async def test_store_counts_concurrent_alias_hits_by_agent(tmp_path) -> None:
    store = SQLiteTaskCapabilityCompatibilityStore(tmp_path / "tasks.db")
    started_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    await store.start_observation(
        agent_id="agent-a",
        started_at=started_at,
    )
    alias = canonical_task_capability_alias(
        "qwenpaw.system.console-agent",
    )
    assert alias is not None

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(
                store.record,
                agent_id="agent-a",
                alias=alias,
                observed_at=started_at + timedelta(days=2),
            )
            for _ in range(40)
        ]
        for future in futures:
            future.result()

    report = await store.report(
        agent_id="agent-a",
        now=started_at + timedelta(days=3),
    )
    other = await store.report(
        agent_id="agent-b",
        now=started_at + timedelta(days=3),
    )

    assert report.total_hits == 40
    assert report.observed_aliases[0].hit_count == 40
    assert report.observed_aliases[0].slot == "runner"
    assert report.zero_usage_seconds == 24 * 60 * 60
    assert report.zero_usage_seconds_remaining == 6 * 24 * 60 * 60
    assert report.removal_authorized is False
    assert "zero_usage_observation_window_incomplete" in (
        report.removal_blockers
    )
    assert other.total_hits == 0
    assert other.removal_blockers[0] == "zero_usage_observation_not_started"


@pytest.mark.asyncio
async def test_canonical_ids_do_not_create_false_positive_hits(
    tmp_path,
) -> None:
    store = SQLiteTaskCapabilityCompatibilityStore(tmp_path / "tasks.db")
    started_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    await store.start_observation(
        agent_id="agent-a",
        started_at=started_at,
    )

    canonical = canonical_task_capability_id(
        "qwenpaw.system.tasks.console-agent",
        on_legacy_hit=lambda alias: store.record(
            agent_id="agent-a",
            alias=alias,
        ),
    )

    report = await store.report(
        agent_id="agent-a",
        now=started_at + timedelta(days=7),
    )

    assert canonical == "qwenpaw.system.tasks.console-agent"
    assert report.total_hits == 0
    assert report.zero_usage_window_complete is True
    assert report.removal_authorized is False
    assert report.removal_blockers == ("persisted_references_not_migrated",)


@pytest.mark.asyncio
async def test_observation_start_is_idempotent_and_hit_resets_window(
    tmp_path,
) -> None:
    store = SQLiteTaskCapabilityCompatibilityStore(tmp_path / "tasks.db")
    started_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    alias = canonical_task_capability_alias(
        "qwenpaw.system.goal-strategy",
    )
    assert alias is not None
    await store.start_observation(
        agent_id="agent-a",
        started_at=started_at,
    )
    await store.start_observation(
        agent_id="agent-a",
        started_at=started_at + timedelta(days=1),
    )
    store.record(
        agent_id="agent-a",
        alias=alias,
        observed_at=started_at + timedelta(days=6),
    )

    report = await store.report(
        agent_id="agent-a",
        now=started_at + timedelta(days=8),
    )

    assert report.observation_started_at == started_at
    assert report.zero_usage_started_at == started_at + timedelta(days=6)
    assert report.last_legacy_hit_at == started_at + timedelta(days=6)
    assert report.zero_usage_seconds == 2 * 24 * 60 * 60
    assert report.zero_usage_window_complete is False


def test_alias_observation_does_not_change_canonical_selection() -> None:
    observed = []

    canonical = canonical_task_capability_id(
        "qwenpaw.system.goal-strategy",
        on_legacy_hit=observed.append,
    )

    assert canonical == "qwenpaw.system.tasks.goal-strategy"
    assert observed[0].slot == "strategy"
