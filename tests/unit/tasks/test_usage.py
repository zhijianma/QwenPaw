# -*- coding: utf-8 -*-
"""Tests for durable Task usage accounting."""

from pathlib import Path
from uuid import UUID

import pytest

from qwenpaw.kernel.models import (
    ExecutionBudget,
    PlanStep,
    RunnerSignal,
    UsageDelta,
)
from qwenpaw.tasks.ledger import SQLiteExecutionLedger
from qwenpaw.tasks.service import TaskService
from qwenpaw.tasks.usage import (
    TaskUsageMeter,
    UsageAccountingUnavailableError,
    UsageBudgetExceededError,
    load_task_usage_snapshot,
)


async def _running_meter(
    tmp_path: Path,
    budget: ExecutionBudget | None = None,
) -> tuple[TaskService, TaskUsageMeter, UUID, UUID]:
    service = TaskService(
        store=SQLiteExecutionLedger(tmp_path / "ledger.db"),
        registry_generation=1,
    )
    task = await service.create_task(
        objective="Measure durable usage",
        agent_id="default",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Measure", objective=task.objective),),
    )
    _, run = await service.start_task(
        task.task_id,
        runner_id="runner.usage",
    )
    meter = TaskUsageMeter(
        service,
        task.task_id,
        run.run_id,
        budget or ExecutionBudget(),
    )
    return service, meter, task.task_id, run.run_id


@pytest.mark.asyncio
async def test_usage_replay_crosses_event_pages_and_honors_watermark(
    tmp_path: Path,
) -> None:
    service, meter, task_id, run_id = await _running_meter(tmp_path)
    await meter.record(UsageDelta(input_tokens=2))
    first_usage_event = next(
        event
        for event in await service.list_events(
            task_id,
            limit=20,
        )
        if event.event_type == "usage.recorded"
    )
    for index in range(205):
        await service.record_runner_signal(
            task_id,
            run_id,
            RunnerSignal(
                event_type="runner.progress",
                payload={"index": index},
            ),
        )
    await meter.record(UsageDelta(output_tokens=3))

    current = await meter.snapshot()
    at_first_usage = await load_task_usage_snapshot(
        service,
        task_id,
        through_sequence=first_usage_event.sequence,
    )

    assert current.input_tokens == 2
    assert current.output_tokens == 3
    assert current.total_tokens == 5
    assert at_first_usage.total_tokens == 2


@pytest.mark.asyncio
async def test_cost_budget_records_actual_usage_before_failing(
    tmp_path: Path,
) -> None:
    _, meter, _, _ = await _running_meter(
        tmp_path,
        ExecutionBudget(max_cost_micros=10),
    )

    with pytest.raises(UsageBudgetExceededError) as raised:
        await meter.record(UsageDelta(cost_micros=11))

    assert raised.value.resource == "cost_micros"
    assert (await meter.snapshot()).cost_micros == 11


@pytest.mark.asyncio
async def test_unknown_cost_is_durable_before_budget_fails(
    tmp_path: Path,
) -> None:
    _, meter, _, _ = await _running_meter(
        tmp_path,
        ExecutionBudget(max_cost_micros=10),
    )

    with pytest.raises(UsageAccountingUnavailableError):
        await meter.record(UsageDelta(cost_unknown=True))

    snapshot = await meter.snapshot()
    assert snapshot.cost_micros == 0
    assert snapshot.cost_unknown is True


@pytest.mark.asyncio
async def test_final_budget_check_detects_descendant_overrun(
    tmp_path: Path,
) -> None:
    _, meter, _, _ = await _running_meter(
        tmp_path,
        ExecutionBudget(max_tokens=5),
    )
    with pytest.raises(UsageBudgetExceededError):
        await meter.record(UsageDelta(input_tokens=6))

    with pytest.raises(UsageBudgetExceededError) as raised:
        await meter.assert_within_budget()

    assert raised.value.resource == "tokens"


@pytest.mark.asyncio
async def test_concurrency_release_requires_matching_acquire(
    tmp_path: Path,
) -> None:
    _, meter, _, _ = await _running_meter(tmp_path)

    with pytest.raises(RuntimeError, match="without acquire"):
        meter.release_concurrency()

    await meter.acquire_concurrency()
    meter.release_concurrency()
