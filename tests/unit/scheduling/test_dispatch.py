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
    DeliveryDestination,
    DeliveryKind,
    DeliveryMode,
    DeliveryRequest,
    ModelSelection,
    PlanStep,
    RunnerSignal,
    ScheduleDefinition,
    ScheduleLeaseStatus,
    ScheduleTrigger,
    ScheduleTriggerCursor,
    ScheduleWorkKind,
    TaskSource,
    TaskStatus,
)
from qwenpaw.plugins.generations import GenerationRegistry
from qwenpaw.scheduling import (
    ScheduleDispatchDisposition,
    ServiceScheduleDispatchDisposition,
    ScheduleTriggerDisposition,
    ScheduledServiceCallbackDispatcher,
    ScheduledDeliveryDispatcher,
    ScheduledTaskDispatcher,
    SQLiteSchedulerStore,
    first_schedule_fire_at,
    schedule_definition_hash,
)
from qwenpaw.delivery import SQLiteDeliveryProjectionStore
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


class _BlockingDeliveryAdapter:
    adapter_id = "schedule-delivery-test.delivery"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.calls = 0

    def supports(self, _request) -> bool:
        return True

    async def deliver(self, _request, *, attempt: int):
        del attempt
        self.calls += 1
        self.started.set()
        await asyncio.Event().wait()


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


def _delivery_bundle() -> CapabilityBundle:
    return CapabilityBundle(
        provider_id="schedule-delivery-test",
        provider_kind=CapabilityProviderKind.SYSTEM,
        version="1.0.0",
        contributions=(
            CapabilityContribution(
                contribution_id="scheduler",
                slot="scheduler",
                entrypoint="tests.schedule:scheduler",
            ),
            CapabilityContribution(
                contribution_id="delivery",
                slot="delivery.adapter",
                entrypoint="tests.schedule:delivery",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_task_dispatcher_rejects_service_schedule(
    tmp_path: Path,
) -> None:
    """Non-Task schedules cannot accidentally materialize fake Tasks."""
    dispatcher = ScheduledTaskDispatcher(
        capability_resolver=None,  # type: ignore[arg-type]
        task_application=None,  # type: ignore[arg-type]
        task_orchestrator=None,  # type: ignore[arg-type]
        ledger_workspace_dir=tmp_path,
    )
    definition = _definition().model_copy(
        update={"work_kind": ScheduleWorkKind.SERVICE},
    )

    with pytest.raises(ValueError, match="task schedules only"):
        await dispatcher.dispatch(
            definition,
            scheduled_for=datetime.now(timezone.utc),
            idempotency_key="service:test",
            owner_id="test",
        )


@pytest.mark.asyncio
async def test_service_dispatcher_renews_lease_during_long_callback(
    tmp_path: Path,
) -> None:
    """A live callback renews ownership before its short lease expires."""
    scheduler = SQLiteSchedulerStore(tmp_path / "scheduler.db")
    registry = GenerationRegistry()

    await registry.activate_bundle(
        _bundle(),
        lambda declaration: {
            "scheduler": scheduler,
            "planner": _Planner(),
            "runner": _Runner(asyncio.Event()),
            "strategy": _Strategy(),
        }[declaration.contribution_id],
    )
    definition = _definition().model_copy(
        update={"work_kind": ScheduleWorkKind.SERVICE},
    )

    async def callback() -> None:
        await asyncio.sleep(0.05)

    result = await ScheduledServiceCallbackDispatcher(
        capability_resolver=registry,
        scheduler_capability_id=_SCHEDULER_ID,
        lease_seconds=0.03,
    ).dispatch(
        definition,
        scheduled_for=datetime.now(timezone.utc),
        idempotency_key="service:renewal",
        owner_id="worker.service",
        callback=callback,
    )

    assert result.disposition is ServiceScheduleDispatchDisposition.EXECUTED
    assert result.lease.revision >= 3


@pytest.mark.asyncio
async def test_delivery_dispatcher_recovers_cancelled_attempt_as_uncertain(
    tmp_path: Path,
) -> None:
    """A restart does not resend an expired attempt with unknown outcome."""
    scheduler = SQLiteSchedulerStore(tmp_path / "scheduler.db")
    deliveries = SQLiteDeliveryProjectionStore(tmp_path / "delivery.db")
    adapter = _BlockingDeliveryAdapter()
    registry = GenerationRegistry()
    await registry.activate_bundle(
        _delivery_bundle(),
        lambda declaration: (
            scheduler
            if declaration.contribution_id == "scheduler"
            else adapter
        ),
    )
    definition = _definition().model_copy(
        update={"work_kind": ScheduleWorkKind.DELIVERY},
    )
    scheduled_for = datetime.now(timezone.utc)

    def request_factory(fire) -> DeliveryRequest:
        return DeliveryRequest(
            source_event_id=fire.fire_id,
            idempotency_key=f"delivery:{fire.fire_id}",
            agent_id=fire.agent_id,
            registry_generation=fire.registry_generation,
            kind=DeliveryKind.RESULT,
            mode=DeliveryMode.FINAL,
            destination=DeliveryDestination(
                adapter_id=adapter.adapter_id,
                address="test-address",
            ),
            payload={"text": "hello"},
        )

    dispatcher = ScheduledDeliveryDispatcher(
        capability_resolver=registry,
        delivery_projection=deliveries,
        scheduler_capability_id="schedule-delivery-test.scheduler",
        lease_seconds=0.03,
    )
    interrupted = asyncio.create_task(
        dispatcher.dispatch(
            definition,
            scheduled_for=scheduled_for,
            idempotency_key="delivery:restart",
            owner_id="worker.first",
            request_factory=request_factory,
        ),
    )
    await adapter.started.wait()
    interrupted.cancel()
    with pytest.raises(asyncio.CancelledError):
        await interrupted
    await asyncio.sleep(0.04)

    recovered = await dispatcher.dispatch(
        definition,
        scheduled_for=scheduled_for,
        idempotency_key="delivery:restart",
        owner_id="worker.restarted",
        request_factory=request_factory,
    )

    assert recovered.delivery is not None
    assert recovered.delivery.attempt.status.value == "uncertain"
    assert adapter.calls == 1


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
