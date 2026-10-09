# -*- coding: utf-8 -*-
"""Durable service schedule catalog and restart coverage."""

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from qwenpaw.app.crons.contracts import ServiceCronJob
from qwenpaw.app.crons.service_schedule_runtime import (
    LiteServiceScheduleRuntime,
)
from qwenpaw.app.crons.task_runtime import LiteCronTaskRuntime
from qwenpaw.kernel import (
    ScheduleFire,
    ScheduleLeaseStatus,
    ScheduleWorkKind,
)
from qwenpaw.plugins.generations import GenerationRegistry
from qwenpaw.scheduling import SQLiteSchedulerStore
from qwenpaw.tasks.bootstrap import task_service_for_workspace


def _workspace(tmp_path: Path):
    return SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        capability_registry=GenerationRegistry(),
    )


@pytest.mark.asyncio
async def test_service_schedule_reconciles_jitter_and_prunes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Service declarations retain jitter and stale cursors are removed."""
    monkeypatch.setattr("qwenpaw.constant.WORKING_DIR", tmp_path)
    runtime = LiteServiceScheduleRuntime(_workspace(tmp_path))
    declaration = ServiceCronJob(
        key="dream",
        cron="0 3 * * *",
        callback=lambda: None,
        jitter_seconds=60,
    )

    schedule_id = await runtime.synchronize(
        source="memory",
        declaration=declaration,
        timezone="Asia/Shanghai",
    )

    store = SQLiteSchedulerStore(tmp_path / "scheduler.db")
    [definition] = await store.list_definitions(agent_id="default")
    assert definition.schedule_id == schedule_id
    assert definition.work_kind is ScheduleWorkKind.SERVICE
    assert definition.trigger.jitter_seconds == 60
    assert definition.trigger.jitter_seed == schedule_id
    assert definition.trigger.timezone == "Asia/Shanghai"
    assert (
        await store.get_cursor(
            agent_id="default",
            schedule_id=schedule_id,
        )
        is not None
    )

    removed = await runtime.prune(source="memory", active_keys=set())

    assert removed == (schedule_id,)
    assert await store.list_definitions(agent_id="default") == ()
    assert (
        await store.get_cursor(
            agent_id="default",
            schedule_id=schedule_id,
        )
        is None
    )


@pytest.mark.asyncio
async def test_restarted_worker_routes_service_without_creating_task(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A fresh worker consumes the persisted service cursor directly."""
    monkeypatch.setattr("qwenpaw.constant.WORKING_DIR", tmp_path)
    workspace = _workspace(tmp_path)
    callback_calls = []

    async def callback() -> None:
        callback_calls.append("called")

    declaration = ServiceCronJob(
        key="daily-paper",
        cron="0 8 * * *",
        callback=callback,
    )
    runtime = LiteServiceScheduleRuntime(workspace)
    schedule_id = await runtime.synchronize(
        source="memory",
        declaration=declaration,
        timezone="UTC",
    )
    store = SQLiteSchedulerStore(tmp_path / "scheduler.db")
    cursor = await store.get_cursor(
        agent_id="default",
        schedule_id=schedule_id,
    )
    assert cursor is not None and cursor.next_fire_at is not None
    worker = LiteCronTaskRuntime(workspace)
    observed = []

    async def execute(definition, occurrence: datetime):
        assert definition.work_kind is ScheduleWorkKind.SERVICE
        observed.append((definition.schedule_id, occurrence))
        return await runtime.execute(
            definition=definition,
            declaration=declaration,
            scheduled_for=occurrence,
        )

    report = await worker.run_due(
        now=cursor.next_fire_at,
        execute=execute,
    )

    assert report.outcomes[0].disposition.value == "dispatched"
    assert observed == [(schedule_id, cursor.next_fire_at)]
    assert callback_calls == ["called"]
    assert not await task_service_for_workspace(workspace).list_tasks()
    lease = await store.claim(
        ScheduleFire(
            agent_id="default",
            schedule_id=schedule_id,
            registry_generation=workspace.capability_registry.generation,
            scheduled_for=cursor.next_fire_at,
            idempotency_key=(
                f"scheduled:{schedule_id}:{cursor.next_fire_at.isoformat()}"
            ),
        ),
        owner_id="replay",
        lease_seconds=30,
    )
    assert lease.status is ScheduleLeaseStatus.COMPLETED
    assert lease.task_id is None
    assert lease.completion_ref == f"service:{schedule_id}"
    [definition] = await store.list_definitions(agent_id="default")

    replay = await runtime.execute(
        definition=definition,
        declaration=declaration,
        scheduled_for=cursor.next_fire_at,
    )

    assert replay.value == "handled"
    assert callback_calls == ["called"]


@pytest.mark.asyncio
async def test_service_failure_is_accounted_before_cursor_advances(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A failed callback is terminal evidence, not an untracked retry."""
    monkeypatch.setattr("qwenpaw.constant.WORKING_DIR", tmp_path)
    workspace = _workspace(tmp_path)

    async def callback() -> None:
        raise RuntimeError("maintenance failed")

    declaration = ServiceCronJob(
        key="auto-fin",
        cron="0 4 * * *",
        callback=callback,
    )
    runtime = LiteServiceScheduleRuntime(workspace)
    schedule_id = await runtime.synchronize(
        source="memory",
        declaration=declaration,
        timezone="UTC",
    )
    store = SQLiteSchedulerStore(tmp_path / "scheduler.db")
    cursor = await store.get_cursor(
        agent_id="default",
        schedule_id=schedule_id,
    )
    assert cursor is not None and cursor.next_fire_at is not None
    worker = LiteCronTaskRuntime(workspace)

    async def execute(definition, occurrence: datetime):
        return await runtime.execute(
            definition=definition,
            declaration=declaration,
            scheduled_for=occurrence,
        )

    report = await worker.run_due(
        now=cursor.next_fire_at,
        execute=execute,
    )

    assert report.outcomes[0].disposition.value == "dispatched"
    lease = await store.claim(
        ScheduleFire(
            agent_id="default",
            schedule_id=schedule_id,
            registry_generation=workspace.capability_registry.generation,
            scheduled_for=cursor.next_fire_at,
            idempotency_key=(
                f"scheduled:{schedule_id}:{cursor.next_fire_at.isoformat()}"
            ),
        ),
        owner_id="replay",
        lease_seconds=30,
    )
    assert lease.status is ScheduleLeaseStatus.FAILED
    assert lease.error_code == "RuntimeError"


@pytest.mark.asyncio
async def test_concurrent_service_occurrence_executes_callback_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A second worker observes the Fire lease instead of duplicating work."""
    monkeypatch.setattr("qwenpaw.constant.WORKING_DIR", tmp_path)
    workspace = _workspace(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def callback() -> None:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()

    declaration = ServiceCronJob(
        key="dream",
        cron="0 3 * * *",
        callback=callback,
    )
    runtime = LiteServiceScheduleRuntime(workspace)
    schedule_id = await runtime.synchronize(
        source="memory",
        declaration=declaration,
        timezone="UTC",
    )
    store = SQLiteSchedulerStore(tmp_path / "scheduler.db")
    [definition] = await store.list_definitions(agent_id="default")
    cursor = await store.get_cursor(
        agent_id="default",
        schedule_id=schedule_id,
    )
    assert cursor is not None and cursor.next_fire_at is not None
    first = asyncio.create_task(
        runtime.execute(
            definition=definition,
            declaration=declaration,
            scheduled_for=cursor.next_fire_at,
        ),
    )
    await started.wait()

    second = await runtime.execute(
        definition=definition,
        declaration=declaration,
        scheduled_for=cursor.next_fire_at,
    )
    release.set()
    first_result = await first

    assert second.value == "retry"
    assert first_result.value == "handled"
    assert calls == 1
