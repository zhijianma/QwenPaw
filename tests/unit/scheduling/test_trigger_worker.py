# -*- coding: utf-8 -*-
"""Focused tests for the durable Schedule trigger worker."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from qwenpaw.kernel import (
    ScheduleDefinition,
    ScheduleTrigger,
    ScheduleTriggerCursor,
)
from qwenpaw.scheduling import (
    DurableScheduleTriggerWorker,
    SQLiteSchedulerStore,
    ScheduleOccurrenceHandling,
    ScheduleTriggerDisposition,
    first_schedule_fire_at,
    schedule_definition_hash,
)


def _definition(
    *,
    interval_seconds: float = 60,
    grace_seconds: int = 600,
    enabled: bool = True,
) -> ScheduleDefinition:
    return ScheduleDefinition(
        schedule_id="reports.minute",
        agent_id="agent-a",
        name="Minute report",
        objective="Prepare one report",
        trigger=ScheduleTrigger(
            kind="interval",
            interval_seconds=interval_seconds,
            start_at=datetime(2026, 10, 9, tzinfo=timezone.utc),
        ),
        runner_id="qwenpaw.system.tasks.console-agent",
        misfire_grace_seconds=grace_seconds,
        enabled=enabled,
    )


async def _register(
    store: SQLiteSchedulerStore,
    definition: ScheduleDefinition,
    *,
    now: datetime,
) -> ScheduleTriggerCursor:
    await store.upsert(definition)
    return await store.reconcile_cursor(
        ScheduleTriggerCursor(
            agent_id=definition.agent_id,
            schedule_id=definition.schedule_id,
            definition_hash=schedule_definition_hash(definition),
            next_fire_at=first_schedule_fire_at(definition, now=now),
        ),
    )


async def _unexpected_handler(
    _definition_value: ScheduleDefinition,
    _scheduled_for: datetime,
) -> None:
    raise AssertionError("handler must not run")


@pytest.mark.asyncio
async def test_successful_occurrence_advances_cursor(tmp_path) -> None:
    store = SQLiteSchedulerStore(tmp_path / "scheduler.db")
    scheduled_for = datetime(2026, 10, 9, tzinfo=timezone.utc)
    definition = _definition()
    await _register(store, definition, now=scheduled_for)
    handled = []

    async def handle(
        received: ScheduleDefinition,
        received_at: datetime,
    ) -> None:
        handled.append((received.schedule_id, received_at))

    report = await DurableScheduleTriggerWorker(
        catalog=store,
        cursors=store,
    ).tick(
        agent_id="agent-a",
        now=scheduled_for,
        handle=handle,
    )

    assert handled == [(definition.schedule_id, scheduled_for)]
    assert report.outcomes[0].disposition is (
        ScheduleTriggerDisposition.DISPATCHED
    )
    cursor = await store.get_cursor(
        agent_id="agent-a",
        schedule_id=definition.schedule_id,
    )
    assert cursor is not None
    assert cursor.last_fire_at == scheduled_for
    assert cursor.next_fire_at == scheduled_for + timedelta(minutes=1)
    assert cursor.revision == 2


@pytest.mark.asyncio
async def test_handler_failure_leaves_due_cursor_retryable(tmp_path) -> None:
    store = SQLiteSchedulerStore(tmp_path / "scheduler.db")
    scheduled_for = datetime(2026, 10, 9, tzinfo=timezone.utc)
    definition = _definition()
    original = await _register(store, definition, now=scheduled_for)

    async def fail(
        _definition_value: ScheduleDefinition,
        _scheduled_for: datetime,
    ) -> None:
        raise RuntimeError("transient private failure")

    report = await DurableScheduleTriggerWorker(
        catalog=store,
        cursors=store,
    ).tick(
        agent_id="agent-a",
        now=scheduled_for,
        handle=fail,
    )

    assert report.outcomes[0].disposition is (
        ScheduleTriggerDisposition.RETRY_PENDING
    )
    assert (
        await store.get_cursor(
            agent_id="agent-a",
            schedule_id=definition.schedule_id,
        )
        == original
    )


@pytest.mark.asyncio
async def test_handler_can_explicitly_request_retry_without_exception(
    tmp_path,
) -> None:
    store = SQLiteSchedulerStore(tmp_path / "scheduler.db")
    scheduled_for = datetime(2026, 10, 9, tzinfo=timezone.utc)
    definition = _definition()
    original = await _register(store, definition, now=scheduled_for)

    async def retry(
        _definition_value: ScheduleDefinition,
        _scheduled_for: datetime,
    ) -> ScheduleOccurrenceHandling:
        return ScheduleOccurrenceHandling.RETRY

    report = await DurableScheduleTriggerWorker(
        catalog=store,
        cursors=store,
    ).tick(
        agent_id="agent-a",
        now=scheduled_for,
        handle=retry,
    )

    assert report.outcomes[0].disposition is (
        ScheduleTriggerDisposition.RETRY_PENDING
    )
    assert (
        await store.get_cursor(
            agent_id="agent-a",
            schedule_id=definition.schedule_id,
        )
        == original
    )


@pytest.mark.asyncio
async def test_expired_occurrence_is_skipped_to_first_future_boundary(
    tmp_path,
) -> None:
    store = SQLiteSchedulerStore(tmp_path / "scheduler.db")
    scheduled_for = datetime(2026, 10, 9, tzinfo=timezone.utc)
    definition = _definition(grace_seconds=10)
    await _register(store, definition, now=scheduled_for)
    current = scheduled_for + timedelta(minutes=3, seconds=30)

    report = await DurableScheduleTriggerWorker(
        catalog=store,
        cursors=store,
    ).tick(
        agent_id="agent-a",
        now=current,
        handle=_unexpected_handler,
    )

    assert report.outcomes[0].disposition is (
        ScheduleTriggerDisposition.MISFIRED
    )
    cursor = await store.get_cursor(
        agent_id="agent-a",
        schedule_id=definition.schedule_id,
    )
    assert cursor is not None
    assert cursor.last_fire_at == scheduled_for
    assert cursor.next_fire_at == scheduled_for + timedelta(minutes=4)


@pytest.mark.asyncio
async def test_catalog_change_reconciles_stale_cursor_without_dispatch(
    tmp_path,
) -> None:
    store = SQLiteSchedulerStore(tmp_path / "scheduler.db")
    scheduled_for = datetime(2026, 10, 9, tzinfo=timezone.utc)
    original = _definition()
    await _register(store, original, now=scheduled_for)
    changed = _definition(interval_seconds=300)
    await store.upsert(changed)

    report = await DurableScheduleTriggerWorker(
        catalog=store,
        cursors=store,
    ).tick(
        agent_id="agent-a",
        now=scheduled_for,
        handle=_unexpected_handler,
    )

    assert report.outcomes[0].disposition is (
        ScheduleTriggerDisposition.RECONCILED
    )
    cursor = await store.get_cursor(
        agent_id="agent-a",
        schedule_id=changed.schedule_id,
    )
    assert cursor is not None
    assert cursor.definition_hash == schedule_definition_hash(changed)


@pytest.mark.asyncio
async def test_disabled_definition_reconciles_to_dormant_cursor(
    tmp_path,
) -> None:
    store = SQLiteSchedulerStore(tmp_path / "scheduler.db")
    scheduled_for = datetime(2026, 10, 9, tzinfo=timezone.utc)
    await _register(store, _definition(), now=scheduled_for)
    disabled = _definition(enabled=False)
    await store.upsert(disabled)

    report = await DurableScheduleTriggerWorker(
        catalog=store,
        cursors=store,
    ).tick(
        agent_id="agent-a",
        now=scheduled_for,
        handle=_unexpected_handler,
    )

    assert report.outcomes[0].disposition is (
        ScheduleTriggerDisposition.RECONCILED
    )
    cursor = await store.get_cursor(
        agent_id="agent-a",
        schedule_id=disabled.schedule_id,
    )
    assert cursor is not None
    assert cursor.next_fire_at is None


@pytest.mark.asyncio
async def test_orphan_cursor_is_removed_without_dispatch(tmp_path) -> None:
    store = SQLiteSchedulerStore(tmp_path / "scheduler.db")
    scheduled_for = datetime(2026, 10, 9, tzinfo=timezone.utc)
    cursor = ScheduleTriggerCursor(
        agent_id="agent-a",
        schedule_id="reports.orphan",
        definition_hash="sha256:orphan",
        next_fire_at=scheduled_for,
    )
    await store.reconcile_cursor(cursor)

    report = await DurableScheduleTriggerWorker(
        catalog=store,
        cursors=store,
    ).tick(
        agent_id="agent-a",
        now=scheduled_for,
        handle=_unexpected_handler,
    )

    assert report.outcomes[0].disposition is (
        ScheduleTriggerDisposition.REMOVED
    )
    assert (
        await store.get_cursor(
            agent_id="agent-a",
            schedule_id=cursor.schedule_id,
        )
        is None
    )


@pytest.mark.asyncio
async def test_concurrent_workers_report_one_cursor_cas_winner(
    tmp_path,
) -> None:
    path = tmp_path / "scheduler.db"
    first_store = SQLiteSchedulerStore(path)
    second_store = SQLiteSchedulerStore(path)
    scheduled_for = datetime(2026, 10, 9, tzinfo=timezone.utc)
    await _register(first_store, _definition(), now=scheduled_for)
    release = asyncio.Event()
    ready = 0

    async def synchronize(
        _definition_value: ScheduleDefinition,
        _scheduled_for: datetime,
    ) -> None:
        nonlocal ready
        ready += 1
        if ready == 2:
            release.set()
        await release.wait()

    reports = await asyncio.gather(
        DurableScheduleTriggerWorker(
            catalog=first_store,
            cursors=first_store,
        ).tick(
            agent_id="agent-a",
            now=scheduled_for,
            handle=synchronize,
        ),
        DurableScheduleTriggerWorker(
            catalog=second_store,
            cursors=second_store,
        ).tick(
            agent_id="agent-a",
            now=scheduled_for,
            handle=synchronize,
        ),
    )

    dispositions = {report.outcomes[0].disposition for report in reports}
    assert dispositions == {
        ScheduleTriggerDisposition.DISPATCHED,
        ScheduleTriggerDisposition.RACE_LOST,
    }
