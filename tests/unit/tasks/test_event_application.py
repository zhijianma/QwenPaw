# -*- coding: utf-8 -*-
"""Tests for transport-neutral Task event queries."""

from pathlib import Path
from uuid import uuid4

import pytest

from qwenpaw.kernel.models import PlanStep
from qwenpaw.tasks.event_application import TaskEventApplicationService
from qwenpaw.tasks.ledger import SQLiteExecutionLedger
from qwenpaw.tasks.service import TaskNotFoundError, TaskService


async def _application(
    tmp_path: Path,
) -> tuple[TaskEventApplicationService, TaskService]:
    store = SQLiteExecutionLedger(tmp_path / "ledger.db")
    await store.initialize()
    service = TaskService(store=store, registry_generation=1)
    return TaskEventApplicationService(service), service


@pytest.mark.asyncio
async def test_page_returns_stable_sequence_cursor(tmp_path: Path) -> None:
    application, service = await _application(tmp_path)
    task = await service.create_task(
        objective="Read task events",
        agent_id="default",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Read", objective=task.objective),),
    )

    first = await application.page(task.task_id, limit=1)
    second = await application.page(
        task.task_id,
        after_sequence=first.next_sequence,
    )

    assert [event.event_type for event in first.items] == ["task.created"]
    assert [event.event_type for event in second.items] == ["task.planned"]
    assert second.next_sequence == 2


@pytest.mark.asyncio
async def test_follow_replays_and_stops_at_terminal_task(
    tmp_path: Path,
) -> None:
    application, service = await _application(tmp_path)
    task = await service.create_task(
        objective="Follow task events",
        agent_id="default",
    )
    await service.cancel_task(task.task_id)

    events = [
        event
        async for event in application.follow(
            task.task_id,
            poll_interval=0,
        )
    ]

    assert [event.event_type for event in events] == [
        "task.created",
        "task.cancelled",
    ]


@pytest.mark.asyncio
async def test_event_queries_reject_unknown_task(tmp_path: Path) -> None:
    application, _ = await _application(tmp_path)

    with pytest.raises(TaskNotFoundError):
        await application.page(uuid4())
