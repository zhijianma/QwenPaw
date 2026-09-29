# -*- coding: utf-8 -*-
"""Contract tests for backend-neutral scheduler domain models."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from qwenpaw.kernel import (
    ScheduleDefinition,
    ScheduleFire,
    ScheduleLease,
    ScheduleLeaseStatus,
    ScheduleTrigger,
    SchedulerPort,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("cron", {"cron": "0 9 * * mon"}),
        ("once", {"run_at": _now()}),
        ("interval", {"interval_seconds": 60}),
    ],
)
def test_schedule_trigger_accepts_exactly_one_shape(kind, value) -> None:
    trigger = ScheduleTrigger(kind=kind, **value)

    assert trigger.kind == kind


def test_schedule_trigger_rejects_missing_or_conflicting_values() -> None:
    with pytest.raises(ValidationError, match="cron trigger value"):
        ScheduleTrigger(kind="cron")
    with pytest.raises(ValidationError, match="cannot include"):
        ScheduleTrigger(
            kind="cron",
            cron="0 9 * * *",
            interval_seconds=60,
        )


def test_interval_trigger_preserves_optional_time_bounds() -> None:
    start = _now()
    end = start + timedelta(days=2)

    trigger = ScheduleTrigger(
        kind="interval",
        interval_seconds=86400,
        start_at=start,
        end_at=end,
    )

    assert trigger.start_at == start
    assert trigger.end_at == end
    with pytest.raises(ValidationError, match="cannot precede"):
        ScheduleTrigger(
            kind="interval",
            interval_seconds=86400,
            start_at=end,
            end_at=start,
        )
    with pytest.raises(ValidationError, match="interval bounds"):
        ScheduleTrigger(
            kind="once",
            run_at=start,
            start_at=start,
        )


def test_schedule_definition_and_fire_round_trip() -> None:
    definition = ScheduleDefinition(
        schedule_id="reports.daily",
        agent_id="default",
        conversation_id="chat-reports",
        name="Daily report",
        objective="Prepare the daily report",
        trigger=ScheduleTrigger(kind="cron", cron="0 9 * * mon-fri"),
        runner_id="qwenpaw.system.tasks.console-agent",
        strategy_id="qwenpaw.system.tasks.default-strategy",
    )
    fire = ScheduleFire(
        agent_id=definition.agent_id,
        schedule_id=definition.schedule_id,
        scheduled_for=_now(),
        idempotency_key="reports.daily:2026-09-28T09:00:00Z",
    )

    assert (
        ScheduleDefinition.model_validate_json(
            definition.model_dump_json(),
        )
        == definition
    )
    assert ScheduleFire.model_validate_json(fire.model_dump_json()) == fire
    assert definition.conversation_id == "chat-reports"
    assert fire.registry_generation == 1


def test_schedule_fire_identity_survives_trigger_reconstruction() -> None:
    scheduled_for = _now()
    values = {
        "agent_id": "default",
        "schedule_id": "reports.daily",
        "scheduled_for": scheduled_for,
        "idempotency_key": "reports.daily:scheduled-time",
        "registry_generation": 3,
    }

    first = ScheduleFire(**values)
    second = ScheduleFire(
        **values,
        fired_at=first.fired_at + timedelta(seconds=1),
    )

    assert first.fire_id == second.fire_id
    assert first.fired_at != second.fired_at
    assert first.same_occurrence(second)


def test_schedule_lease_requires_coherent_terminal_facts() -> None:
    now = _now()
    fire = ScheduleFire(
        agent_id="default",
        schedule_id="reports.daily",
        scheduled_for=now,
        idempotency_key="fire-1",
    )
    claimed = ScheduleLease(
        fire=fire,
        owner_id="worker.local",
        acquired_at=now,
        expires_at=now + timedelta(seconds=30),
    )
    completed = claimed.model_copy(
        update={
            "status": ScheduleLeaseStatus.COMPLETED,
            "revision": 2,
            "task_id": uuid4(),
            "finished_at": now + timedelta(seconds=1),
        },
    )

    assert completed.status is ScheduleLeaseStatus.COMPLETED
    with pytest.raises(ValidationError, match="requires task_id"):
        ScheduleLease(
            fire=fire,
            owner_id="worker.local",
            status=ScheduleLeaseStatus.COMPLETED,
            acquired_at=now,
            expires_at=now + timedelta(seconds=30),
            finished_at=now + timedelta(seconds=1),
        )
    with pytest.raises(ValidationError, match="requires error_code"):
        ScheduleLease(
            fire=fire,
            owner_id="worker.local",
            status=ScheduleLeaseStatus.FAILED,
            acquired_at=now,
            expires_at=now + timedelta(seconds=30),
            finished_at=now + timedelta(seconds=1),
        )


def test_scheduler_port_is_runtime_checkable() -> None:
    class _Scheduler:
        async def upsert(self, definition):
            return definition

        async def remove(self, **kwargs):
            return bool(kwargs)

        async def list_definitions(self, **kwargs):
            del kwargs
            return ()

        async def claim(self, fire, **kwargs):
            del fire, kwargs

        async def renew(self, lease_id, **kwargs):
            del lease_id, kwargs

        async def complete(self, lease_id, **kwargs):
            del lease_id, kwargs

        async def fail(self, lease_id, **kwargs):
            del lease_id, kwargs

        async def recover_expired(self, **kwargs):
            del kwargs
            return ()

    assert isinstance(_Scheduler(), SchedulerPort)
