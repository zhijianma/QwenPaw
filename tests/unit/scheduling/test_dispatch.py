# -*- coding: utf-8 -*-
"""Focused tests for Schedule Fire to Task Runtime dispatch."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from qwenpaw.kernel import (
    ApprovalLevel,
    CapabilityBundle,
    CapabilityContribution,
    CapabilityProviderKind,
    ModelSelection,
    PlanStep,
    RunnerSignal,
    ScheduleDefinition,
    ScheduleLeaseStatus,
    ScheduleTrigger,
    ScheduleTriggerCursor,
    TaskSource,
    TaskStatus,
)
from qwenpaw.plugins.generations import GenerationRegistry
from qwenpaw.scheduling import (
    ScheduleDispatchDisposition,
    ScheduleTriggerDisposition,
    ScheduledTaskDispatcher,
    SQLiteSchedulerStore,
    first_schedule_fire_at,
    schedule_definition_hash,
)
from qwenpaw.tasks.application import TaskApplicationService
from qwenpaw.tasks.ledger import SQLiteExecutionLedger
from qwenpaw.tasks.runtime import (
    TaskRuntimeOrchestrator,
    TaskRuntimeSupervisor,
)
from qwenpaw.tasks.service import TaskService

_PROVIDER_ID = "schedule-test"
_SCHEDULER_ID = f"{_PROVIDER_ID}.scheduler"
_PLANNER_ID = f"{_PROVIDER_ID}.planner"
_RUNNER_ID = f"{_PROVIDER_ID}.runner"
_STRATEGY_ID = f"{_PROVIDER_ID}.strategy"


class _Planner:
    planner_id = _PLANNER_ID

    async def plan(self, order):
        return (PlanStep(title="Scheduled step", objective=order.objective),)


class _Runner:
    runner_id = _RUNNER_ID

    def __init__(self, release: asyncio.Event) -> None:
        self._release = release

    async def execute(self, order, _run):
        yield RunnerSignal(
            event_type="runner.progress",
            payload={"objective": order.objective},
        )
        await self._release.wait()


class _Strategy:
    strategy_id = _STRATEGY_ID

    async def prepare(self, context, _order):
        assert context.conversation_id == "chat-reports"
        assert context.approval_level is ApprovalLevel.OFF
        assert context.model_selection == ModelSelection(
            provider_id="dashscope",
            model="qwen-max",
        )
        return {"mode": "scheduled"}


class _FailOnceOrchestrator:
    def __init__(self, delegate: TaskRuntimeOrchestrator) -> None:
        self._delegate = delegate
        self.calls = 0

    async def start(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("runner startup failed")
        return await self._delegate.start(*args, **kwargs)


def _definition() -> ScheduleDefinition:
    return ScheduleDefinition(
        schedule_id="reports.daily",
        agent_id="default",
        conversation_id="chat-reports",
        name="Daily report",
        objective="Prepare the daily report",
        trigger=ScheduleTrigger(kind="cron", cron="0 9 * * *"),
        planner_id=_PLANNER_ID,
        runner_id=_RUNNER_ID,
        strategy_id=_STRATEGY_ID,
    )


def _bundle() -> CapabilityBundle:
    return CapabilityBundle(
        provider_id=_PROVIDER_ID,
        provider_kind=CapabilityProviderKind.SYSTEM,
        version="1.0.0",
        contributions=(
            CapabilityContribution(
                contribution_id="scheduler",
                slot="scheduler",
                entrypoint="tests.schedule:scheduler",
            ),
            CapabilityContribution(
                contribution_id="planner",
                slot="planner",
                entrypoint="tests.schedule:planner",
            ),
            CapabilityContribution(
                contribution_id="runner",
                slot="runner",
                entrypoint="tests.schedule:runner",
            ),
            CapabilityContribution(
                contribution_id="strategy",
                slot="strategy",
                entrypoint="tests.schedule:strategy",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_due_triggers_use_generation_pinned_scheduler_catalog(
    tmp_path: Path,
) -> None:
    release = asyncio.Event()
    scheduler = SQLiteSchedulerStore(tmp_path / "scheduler.db")
    registry = GenerationRegistry()

    def factory(declaration):
        implementations = {
            "scheduler": scheduler,
            "planner": _Planner(),
            "runner": _Runner(release),
            "strategy": _Strategy(),
        }
        return implementations[declaration.contribution_id]

    await registry.activate_bundle(_bundle(), factory)
    service = TaskService(
        store=SQLiteExecutionLedger(tmp_path / "tasks.db"),
        registry_generation=registry.generation,
    )
    definition = _definition()
    scheduled_for = datetime(2026, 10, 10, 9, tzinfo=timezone.utc)
    await scheduler.upsert(definition)
    await scheduler.reconcile_cursor(
        ScheduleTriggerCursor(
            agent_id=definition.agent_id,
            schedule_id=definition.schedule_id,
            definition_hash=schedule_definition_hash(definition),
            next_fire_at=first_schedule_fire_at(
                definition,
                now=scheduled_for,
            ),
        ),
    )
    handled = []

    async def handle(
        received: ScheduleDefinition,
        received_at: datetime,
    ) -> None:
        handled.append((received.schedule_id, received_at))

    report = await ScheduledTaskDispatcher(
        capability_resolver=registry,
        task_application=TaskApplicationService(
            service,
            agent_id="default",
            default_project_dir=tmp_path,
        ),
        task_orchestrator=TaskRuntimeOrchestrator(
            service,
            registry,
            TaskRuntimeSupervisor(),
        ),
        ledger_workspace_dir=tmp_path,
        scheduler_capability_id=_SCHEDULER_ID,
    ).run_due_triggers(
        agent_id="default",
        now=scheduled_for,
        cursors=scheduler,
        handle=handle,
    )

    assert handled == [(definition.schedule_id, scheduled_for)]
    assert report.outcomes[0].disposition is (
        ScheduleTriggerDisposition.DISPATCHED
    )


@pytest.mark.asyncio
async def test_concurrent_trigger_creates_one_generation_pinned_task(
    tmp_path: Path,
) -> None:
    release = asyncio.Event()
    scheduler = SQLiteSchedulerStore(tmp_path / "scheduler.db")
    registry = GenerationRegistry()

    def factory(declaration):
        implementations = {
            "scheduler": scheduler,
            "planner": _Planner(),
            "runner": _Runner(release),
            "strategy": _Strategy(),
        }
        return implementations[declaration.contribution_id]

    await registry.activate_bundle(_bundle(), factory)
    service = TaskService(
        store=SQLiteExecutionLedger(tmp_path / "tasks.db"),
        registry_generation=registry.generation,
    )
    application = TaskApplicationService(
        service,
        agent_id="default",
        default_project_dir=tmp_path,
    )
    dispatcher = ScheduledTaskDispatcher(
        capability_resolver=registry,
        task_application=application,
        task_orchestrator=TaskRuntimeOrchestrator(
            service,
            registry,
            TaskRuntimeSupervisor(),
        ),
        ledger_workspace_dir=tmp_path,
        scheduler_capability_id=_SCHEDULER_ID,
    )
    definition = ScheduleDefinition(
        schedule_id="reports.daily",
        agent_id="default",
        conversation_id="chat-reports",
        name="Daily report",
        objective="Prepare the daily report",
        trigger=ScheduleTrigger(kind="cron", cron="0 9 * * *"),
        planner_id=_PLANNER_ID,
        runner_id=_RUNNER_ID,
        strategy_id=_STRATEGY_ID,
        metadata={
            "approval_level": "off",
            "model_selection": ModelSelection(
                provider_id="dashscope",
                model="qwen-max",
            ).model_dump(mode="json"),
        },
    )
    scheduled_for = datetime.now(timezone.utc)

    results = await asyncio.gather(
        dispatcher.dispatch(
            definition,
            scheduled_for=scheduled_for,
            idempotency_key="reports.daily:scheduled-time",
            owner_id="worker.first",
        ),
        dispatcher.dispatch(
            definition,
            scheduled_for=scheduled_for,
            idempotency_key="reports.daily:scheduled-time",
            owner_id="worker.second",
        ),
    )

    executing = [
        result
        for result in results
        if result.disposition
        in {
            ScheduleDispatchDisposition.STARTED,
            ScheduleDispatchDisposition.RECOVERED,
        }
    ]
    assert len(executing) == 1
    assert results[0].lease.lease_id == results[1].lease.lease_id
    assert all(
        result.disposition
        in {
            ScheduleDispatchDisposition.STARTED,
            ScheduleDispatchDisposition.RECOVERED,
            ScheduleDispatchDisposition.OWNED_ELSEWHERE,
            ScheduleDispatchDisposition.REPLAYED,
        }
        for result in results
    )
    dispatched = executing[0]
    assert dispatched.lease.status is ScheduleLeaseStatus.COMPLETED
    assert dispatched.task is not None
    assert dispatched.run is not None
    assert dispatched.task.source is TaskSource.SCHEDULE
    assert dispatched.task.metadata["schedule_id"] == "reports.daily"
    assert dispatched.task.metadata["conversation_id"] == "chat-reports"
    assert dispatched.task.metadata["model_selection"]["model"] == ("qwen-max")
    assert (
        dispatched.run.registry_generation
        == dispatched.lease.fire.registry_generation
        == registry.generation
    )
    assert len(await service.list_tasks(cursor=None, limit=10)) == 1

    release.set()
    for _ in range(100):
        task = await service.get_task(dispatched.task.task_id)
        if task is not None and task.status is TaskStatus.COMPLETED:
            break
        await asyncio.sleep(0.01)
    assert task is not None
    assert task.status is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_replay_recovers_a_bound_task_after_startup_failure(
    tmp_path: Path,
) -> None:
    release = asyncio.Event()
    scheduler = SQLiteSchedulerStore(tmp_path / "scheduler.db")
    registry = GenerationRegistry()

    def factory(declaration):
        implementations = {
            "scheduler": scheduler,
            "planner": _Planner(),
            "runner": _Runner(release),
            "strategy": _Strategy(),
        }
        return implementations[declaration.contribution_id]

    await registry.activate_bundle(_bundle(), factory)
    service = TaskService(
        store=SQLiteExecutionLedger(tmp_path / "tasks.db"),
        registry_generation=registry.generation,
    )
    application = TaskApplicationService(
        service,
        agent_id="default",
        default_project_dir=tmp_path,
    )
    orchestrator = _FailOnceOrchestrator(
        TaskRuntimeOrchestrator(
            service,
            registry,
            TaskRuntimeSupervisor(),
        ),
    )
    dispatcher = ScheduledTaskDispatcher(
        capability_resolver=registry,
        task_application=application,
        task_orchestrator=orchestrator,
        ledger_workspace_dir=tmp_path,
        scheduler_capability_id=_SCHEDULER_ID,
    )
    definition = ScheduleDefinition(
        schedule_id="reports.recover",
        agent_id="default",
        conversation_id="chat-reports",
        name="Recover report",
        objective="Recover the scheduled report",
        trigger=ScheduleTrigger(kind="cron", cron="0 9 * * *"),
        planner_id=_PLANNER_ID,
        runner_id=_RUNNER_ID,
        strategy_id=_STRATEGY_ID,
        metadata={
            "approval_level": "off",
            "model_selection": ModelSelection(
                provider_id="dashscope",
                model="qwen-max",
            ).model_dump(mode="json"),
        },
    )
    scheduled_for = datetime.now(timezone.utc)

    with pytest.raises(RuntimeError, match="runner startup failed"):
        await dispatcher.dispatch(
            definition,
            scheduled_for=scheduled_for,
            idempotency_key="reports.recover:scheduled-time",
            owner_id="worker.first",
        )

    created = await service.list_tasks(cursor=None, limit=10)
    assert len(created) == 1
    assert created[0].status is TaskStatus.CREATED

    recoveries = await asyncio.gather(
        dispatcher.dispatch(
            definition,
            scheduled_for=scheduled_for,
            idempotency_key="reports.recover:scheduled-time",
            owner_id="worker.second",
        ),
        dispatcher.dispatch(
            definition,
            scheduled_for=scheduled_for,
            idempotency_key="reports.recover:scheduled-time",
            owner_id="worker.third",
        ),
    )
    recovered_items = [
        item
        for item in recoveries
        if item.disposition is ScheduleDispatchDisposition.RECOVERED
    ]
    assert len(recovered_items) == 1
    assert all(
        item.disposition
        in {
            ScheduleDispatchDisposition.RECOVERED,
            ScheduleDispatchDisposition.REPLAYED,
        }
        for item in recoveries
    )
    recovered = recovered_items[0]
    assert recovered.disposition is ScheduleDispatchDisposition.RECOVERED
    assert recovered.task is not None
    assert recovered.task.task_id == created[0].task_id
    assert recovered.run is not None
    assert recovered.run.registry_generation == registry.generation
    assert len(await service.list_tasks(cursor=None, limit=10)) == 1
    assert len(await service.list_runs(created[0].task_id)) == 1

    release.set()
