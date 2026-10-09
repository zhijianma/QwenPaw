# -*- coding: utf-8 -*-
"""Pure recurrence evaluation for durable Schedule trigger workers."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from apscheduler.triggers.cron import CronTrigger

from ..kernel import ScheduleDefinition, ScheduleTrigger


def schedule_definition_hash(definition: ScheduleDefinition) -> str:
    """Return a domain-separated digest for cursor reset fencing."""
    payload = json.dumps(
        definition.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.sha256(
        b"qwenpaw:schedule-definition:v1\x00" + payload,
    ).hexdigest()
    return f"sha256:{digest}"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("schedule trigger time must be timezone-aware")
    return value.astimezone(timezone.utc)


def first_schedule_fire_at(
    definition: ScheduleDefinition,
    *,
    now: datetime,
) -> datetime | None:
    """Calculate initial progress without mutating durable state."""
    if not definition.enabled:
        return None
    trigger = definition.trigger
    current = _utc(now)
    if trigger.kind == "once":
        assert trigger.run_at is not None
        return _utc(trigger.run_at)
    if trigger.kind == "interval":
        candidate = _utc(trigger.start_at or current)
        assert trigger.interval_seconds is not None
        if candidate < current:
            elapsed = (current - candidate).total_seconds()
            periods = int(elapsed // trigger.interval_seconds)
            candidate += timedelta(
                seconds=periods * trigger.interval_seconds,
            )
            if candidate < current:
                candidate += timedelta(seconds=trigger.interval_seconds)
        if trigger.end_at is not None and candidate > _utc(trigger.end_at):
            return None
        return candidate
    assert trigger.cron is not None
    candidate = CronTrigger.from_crontab(
        trigger.cron,
        timezone=trigger.timezone,
    ).get_next_fire_time(None, current)
    return _utc(candidate) if candidate is not None else None


def next_schedule_fire_at(
    trigger: ScheduleTrigger,
    *,
    previous: datetime,
) -> datetime | None:
    """Calculate the occurrence after one committed scheduled instant."""
    committed = _utc(previous)
    if trigger.kind == "once":
        return None
    if trigger.kind == "interval":
        assert trigger.interval_seconds is not None
        candidate = committed + timedelta(seconds=trigger.interval_seconds)
        if trigger.end_at is not None and candidate > _utc(trigger.end_at):
            return None
        return candidate
    assert trigger.cron is not None
    candidate = CronTrigger.from_crontab(
        trigger.cron,
        timezone=trigger.timezone,
    ).get_next_fire_time(committed, committed)
    return _utc(candidate) if candidate is not None else None


def next_schedule_fire_after(
    trigger: ScheduleTrigger,
    *,
    previous: datetime,
    not_before: datetime,
) -> datetime | None:
    """Calculate the first occurrence strictly after one time boundary."""
    committed = _utc(previous)
    boundary = _utc(not_before)
    if trigger.kind == "once":
        return None
    if trigger.kind == "interval":
        assert trigger.interval_seconds is not None
        candidate = committed + timedelta(seconds=trigger.interval_seconds)
        if candidate <= boundary:
            elapsed = (boundary - committed).total_seconds()
            periods = int(elapsed // trigger.interval_seconds) + 1
            candidate = committed + timedelta(
                seconds=periods * trigger.interval_seconds,
            )
        if trigger.end_at is not None and candidate > _utc(trigger.end_at):
            return None
        return candidate
    assert trigger.cron is not None
    cron = CronTrigger.from_crontab(
        trigger.cron,
        timezone=trigger.timezone,
    )
    candidate = cron.get_next_fire_time(committed, boundary)
    while candidate is not None and _utc(candidate) <= boundary:
        candidate = cron.get_next_fire_time(candidate, candidate)
    return _utc(candidate) if candidate is not None else None


__all__ = [
    "first_schedule_fire_at",
    "next_schedule_fire_after",
    "next_schedule_fire_at",
    "schedule_definition_hash",
]
