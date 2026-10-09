# -*- coding: utf-8 -*-
"""Tests for pure durable Schedule recurrence evaluation."""

from datetime import datetime, timedelta, timezone

from qwenpaw.kernel import ScheduleDefinition, ScheduleTrigger
from qwenpaw.plugins import sdk
from qwenpaw.scheduling import (
    first_schedule_fire_at,
    next_schedule_fire_at,
    schedule_definition_hash,
)


def _definition(trigger: ScheduleTrigger, *, enabled: bool = True):
    return ScheduleDefinition(
        schedule_id="reports.daily",
        agent_id="agent-a",
        name="Daily report",
        objective="Prepare report",
        trigger=trigger,
        runner_id="qwenpaw.system.tasks.console-agent",
        enabled=enabled,
    )


def test_trigger_cursor_remains_host_only() -> None:
    assert not hasattr(sdk, "ScheduleTriggerCursor")
    assert not hasattr(sdk, "ScheduleTriggerCursorStore")


def test_definition_hash_is_stable_and_covers_execution_facts() -> None:
    definition = _definition(
        ScheduleTrigger(kind="cron", cron="0 9 * * *"),
    )

    assert schedule_definition_hash(definition) == (
        schedule_definition_hash(definition.model_copy(deep=True))
    )
    assert schedule_definition_hash(definition) != schedule_definition_hash(
        definition.model_copy(update={"enabled": False}),
    )


def test_once_and_disabled_initial_occurrences() -> None:
    run_at = datetime(2026, 10, 10, 9, tzinfo=timezone.utc)
    definition = _definition(
        ScheduleTrigger(kind="once", run_at=run_at),
    )

    assert (
        first_schedule_fire_at(
            definition,
            now=run_at - timedelta(days=1),
        )
        == run_at
    )
    assert (
        next_schedule_fire_at(
            definition.trigger,
            previous=run_at,
        )
        is None
    )
    assert (
        first_schedule_fire_at(
            definition.model_copy(update={"enabled": False}),
            now=run_at,
        )
        is None
    )


def test_interval_respects_start_and_end_bounds() -> None:
    start = datetime(2026, 10, 10, 9, tzinfo=timezone.utc)
    trigger = ScheduleTrigger(
        kind="interval",
        interval_seconds=3600,
        start_at=start,
        end_at=start + timedelta(hours=1),
    )

    assert (
        first_schedule_fire_at(
            _definition(trigger),
            now=start - timedelta(days=1),
        )
        == start
    )
    assert next_schedule_fire_at(trigger, previous=start) == (
        start + timedelta(hours=1)
    )
    assert (
        next_schedule_fire_at(
            trigger,
            previous=start + timedelta(hours=1),
        )
        is None
    )

    unbounded = ScheduleTrigger(
        kind="interval",
        interval_seconds=3600,
        start_at=start,
    )
    assert first_schedule_fire_at(
        _definition(unbounded),
        now=start + timedelta(hours=2, minutes=10),
    ) == start + timedelta(hours=3)


def test_cron_evaluation_is_timezone_aware_and_stateless() -> None:
    trigger = ScheduleTrigger(
        kind="cron",
        cron="0 9 * * *",
        timezone="Asia/Shanghai",
    )
    now = datetime(2026, 10, 9, 0, tzinfo=timezone.utc)
    first = datetime(2026, 10, 9, 1, tzinfo=timezone.utc)

    assert first_schedule_fire_at(_definition(trigger), now=now) == first
    assert next_schedule_fire_at(trigger, previous=first) == (
        first + timedelta(days=1)
    )
