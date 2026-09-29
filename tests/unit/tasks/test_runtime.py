# -*- coding: utf-8 -*-
"""Tests for the unified task runtime orchestrator."""

import asyncio
from pathlib import Path

import pytest

from qwenpaw.kernel.models import (
    CapabilityBundle,
    CapabilityContribution,
    CapabilityProviderKind,
    ExecutionBudget,
    ExecutionContract,
    PlanStep,
    Run,
    RunnerSignal,
    Task,
    TaskStatus,
)
from qwenpaw.plugins.generations import ActivationError, GenerationRegistry
from qwenpaw.tasks.ledger import SQLiteExecutionLedger
from qwenpaw.tasks.runner import (
    RunnerCapabilityUnavailableError,
    TaskExecutionBudgetExceededError,
)
from qwenpaw.tasks.runtime import (
    TaskRuntimeOrchestrator,
    TaskRuntimeSupervisor,
)
from qwenpaw.tasks.service import TaskService

_PLANNER_ID = "runtime-test.planner"
_RUNNER_ID = "runtime-test.runner"
_STRATEGY_ID = "runtime-test.strategy"


class _Planner:
    planner_id = _PLANNER_ID

    async def health_check(self) -> bool:
        return True

    async def plan(self, order):
        return (
            PlanStep(
                title="Plan through catalog",
                objective=order.objective,
            ),
        )


class _Runner:
    runner_id = _RUNNER_ID

    def __init__(self, release: asyncio.Event) -> None:
        self._release = release

    async def health_check(self) -> bool:
        return True

    async def execute(self, order, run):
        yield RunnerSignal(
            event_type="runner.progress",
            payload={
                "generation": run.registry_generation,
                "objective": order.objective,
            },
        )
        await self._release.wait()


class _Strategy:
    strategy_id = _STRATEGY_ID

    def __init__(self) -> None:
        self.prepared_generations: list[int] = []

    async def health_check(self) -> bool:
        return True

    async def prepare(self, context, _order):
        self.prepared_generations.append(context.registry_generation)
        return {"mode": "runtime-test"}


class _WrongIdStrategy(_Strategy):
    strategy_id = "runtime-test.wrong-strategy"


class _FailingStrategy(_Strategy):
    async def prepare(self, _context, _order):
        raise ValueError("strategy preparation failed")


class _InvalidStrategy(_Strategy):
    async def prepare(self, _context, _order):
        return {"invalid": object()}


def _bundle(version: str) -> CapabilityBundle:
    return CapabilityBundle(
        provider_id="runtime-test",
        provider_kind=CapabilityProviderKind.SYSTEM,
        version=version,
        contributions=(
            CapabilityContribution(
                contribution_id="planner",
                slot="planner",
                entrypoint="tests.runtime:planner",
            ),
            CapabilityContribution(
                contribution_id="runner",
                slot="runner",
                entrypoint="tests.runtime:runner",
            ),
            CapabilityContribution(
                contribution_id="strategy",
                slot="strategy",
                entrypoint="tests.runtime:strategy",
            ),
        ),
    )


async def _assert_completed_resume_replay(
    registry: GenerationRegistry,
    orchestrator: TaskRuntimeOrchestrator,
    suspended: Task,
    expected_run: Run,
    runner_orders: list,
) -> None:
    await registry.activate_bundle(
        CapabilityBundle(
            provider_id="runtime-test",
            provider_kind=CapabilityProviderKind.SYSTEM,
            version="2.0.0",
            contributions=(),
        ),
        lambda _declaration: object(),
    )
    replayed = await orchestrator.resume(
        suspended,
        idempotency_key="resume-once",
    )
    assert replayed.run_id == expected_run.run_id
    assert replayed.status.value == "succeeded"
    assert len(runner_orders) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("slot", "implementation", "reason"),
    [
        ("runner", _Strategy(), "does not implement the 'runner' contract"),
        (
            "strategy",
            object(),
            "does not implement the 'strategy' contract",
        ),
        (
            "strategy",
            _WrongIdStrategy(),
            "declares strategy_id='runtime-test.wrong-strategy'",
        ),
    ],
)
async def test_invalid_strategy_activation_fails_closed(
    slot,
    implementation,
    reason: str,
) -> None:
    registry = GenerationRegistry()
    bundle = CapabilityBundle(
        provider_id="runtime-test",
        provider_kind=CapabilityProviderKind.PLUGIN,
        version="1.0.0",
        contributions=(
            CapabilityContribution(
                contribution_id="strategy",
                slot=slot,
                entrypoint="tests.runtime:strategy",
            ),
        ),
    )
    with pytest.raises(ActivationError, match=reason):
        await registry.activate_bundle(bundle, lambda _: implementation)

    assert registry.generation == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("strategy", "error_summary"),
    [
        (_FailingStrategy(), "ValueError"),
        (_InvalidStrategy(), "ValidationError"),
    ],
)
async def test_strategy_failure_becomes_durable_run_failure(
    tmp_path: Path,
    strategy,
    error_summary: str,
) -> None:
    release = asyncio.Event()
    registry = GenerationRegistry()

    def factory(declaration):
        if declaration.contribution_id == "planner":
            return _Planner()
        if declaration.contribution_id == "strategy":
            return strategy
        return _Runner(release)

    await registry.activate_bundle(_bundle("1.0.0"), factory)
    service = TaskService(
        store=SQLiteExecutionLedger(tmp_path / "ledger.db"),
        registry_generation=registry.generation,
    )
    task = await service.create_task(
        objective="Fail during strategy preparation",
        agent_id="default",
    )
    supervisor = TaskRuntimeSupervisor()
    orchestrator = TaskRuntimeOrchestrator(
        service,
        registry,
        supervisor,
    )

    await orchestrator.start(
        task,
        planner_id=_PLANNER_ID,
        runner_id=_RUNNER_ID,
        strategy_id=_STRATEGY_ID,
    )
    for _ in range(100):
        failed = await service.get_task(task.task_id)
        if failed is not None and failed.status is TaskStatus.FAILED:
            break
        await asyncio.sleep(0.01)

    assert failed is not None
    assert failed.status is TaskStatus.FAILED
    events = await service.list_events(task.task_id)
    assert events[-1].event_type == "run.failed"
    assert events[-1].payload == {"error_summary": error_summary}


@pytest.mark.asyncio
async def test_orchestrator_pins_planner_and_runner_generation(
    tmp_path: Path,
) -> None:
    release = asyncio.Event()
    registry = GenerationRegistry()
    strategies: list[_Strategy] = []

    def factory(declaration):
        if declaration.contribution_id == "planner":
            return _Planner()
        if declaration.contribution_id == "strategy":
            strategy = _Strategy()
            strategies.append(strategy)
            return strategy
        return _Runner(release)

    await registry.activate_bundle(_bundle("1.0.0"), factory)
    service = TaskService(
        store=SQLiteExecutionLedger(tmp_path / "ledger.db"),
        registry_generation=registry.generation,
    )
    task = await service.create_task(
        objective="Use one immutable generation",
        agent_id="default",
    )
    supervisor = TaskRuntimeSupervisor()
    orchestrator = TaskRuntimeOrchestrator(
        service,
        registry,
        supervisor,
    )

    run = await orchestrator.start(
        task,
        planner_id=_PLANNER_ID,
        runner_id=_RUNNER_ID,
        strategy_id=_STRATEGY_ID,
    )

    assert run.registry_generation == 2
    assert run.strategy_id == _STRATEGY_ID
    assert supervisor.contains(task.task_id)
    plan = await service.latest_plan(task.task_id)
    assert plan is not None
    assert plan.steps[0].title == "Plan through catalog"

    await registry.activate_bundle(_bundle("2.0.0"), factory)
    assert registry.retained_generations() == (2, 3)

    release.set()
    for _ in range(100):
        stored = await service.get_task(task.task_id)
        if stored is not None and stored.status is TaskStatus.COMPLETED:
            break
        await asyncio.sleep(0.01)

    assert stored is not None
    assert stored.status is TaskStatus.COMPLETED
    assert not supervisor.contains(task.task_id)
    assert registry.retained_generations() == (3,)
    events = await service.list_events(task.task_id)
    assert events[-2].payload["generation"] == 2
    assert strategies[0].prepared_generations == [2]


@pytest.mark.asyncio
async def test_orchestrator_can_start_from_retained_generation(
    tmp_path: Path,
) -> None:
    release = asyncio.Event()
    registry = GenerationRegistry()

    def factory(declaration):
        if declaration.contribution_id == "planner":
            return _Planner()
        if declaration.contribution_id == "strategy":
            return _Strategy()
        return _Runner(release)

    await registry.activate_bundle(_bundle("1.0.0"), factory)
    generation_guard = await registry.pin()
    await registry.activate_bundle(_bundle("2.0.0"), factory)
    service = TaskService(
        store=SQLiteExecutionLedger(tmp_path / "ledger.db"),
        registry_generation=registry.generation,
    )
    task = await service.create_task(
        objective="Use the Schedule Fire generation",
        agent_id="default",
    )
    orchestrator = TaskRuntimeOrchestrator(
        service,
        registry,
        TaskRuntimeSupervisor(),
    )

    run = await orchestrator.start(
        task,
        planner_id=_PLANNER_ID,
        runner_id=_RUNNER_ID,
        strategy_id=_STRATEGY_ID,
        registry_generation=generation_guard.generation,
    )

    assert run.registry_generation == generation_guard.generation == 2
    await generation_guard.close()
    release.set()
    for _ in range(100):
        completed = await service.get_task(task.task_id)
        if completed is not None and completed.status is TaskStatus.COMPLETED:
            break
        await asyncio.sleep(0.01)
    assert completed is not None
    assert completed.status is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_supervisor_cancels_owned_execution(tmp_path: Path) -> None:
    release = asyncio.Event()
    registry = GenerationRegistry()

    def factory(declaration):
        if declaration.contribution_id == "planner":
            return _Planner()
        if declaration.contribution_id == "strategy":
            return _Strategy()
        return _Runner(release)

    await registry.activate_bundle(_bundle("1.0.0"), factory)
    service = TaskService(
        store=SQLiteExecutionLedger(tmp_path / "ledger.db"),
        registry_generation=registry.generation,
    )
    task = await service.create_task(
        objective="Cancel through the supervisor",
        agent_id="default",
    )
    supervisor = TaskRuntimeSupervisor()
    orchestrator = TaskRuntimeOrchestrator(
        service,
        registry,
        supervisor,
    )
    await orchestrator.start(
        task,
        planner_id=_PLANNER_ID,
        runner_id=_RUNNER_ID,
        strategy_id=_STRATEGY_ID,
    )

    cancelled = await orchestrator.cancel(task.task_id)
    assert cancelled.status is TaskStatus.CANCELLED
    assert not supervisor.contains(task.task_id)
    assert registry.retained_generations() == (2,)


@pytest.mark.asyncio
async def test_contextual_runner_observes_cooperative_cancellation(
    tmp_path: Path,
) -> None:
    started = asyncio.Event()
    observed_reason: list[str | None] = []
    observed_strategy: list[dict] = []

    class _ContextualRunner:
        runner_id = _RUNNER_ID

        async def health_check(self) -> bool:
            return True

        async def execute(self, _order, _run):
            yield RunnerSignal(event_type="runner.legacy")

        async def execute_context(self, _order, _run, context):
            yield RunnerSignal(event_type="runner.progress")
            observed_strategy.append(context.strategy_parameters)
            started.set()
            await context.cancellation.wait()
            observed_reason.append(context.cancellation.reason)

    runner = _ContextualRunner()
    registry = GenerationRegistry()

    def factory(declaration):
        if declaration.contribution_id == "planner":
            return _Planner()
        if declaration.contribution_id == "strategy":
            return _Strategy()
        return runner

    await registry.activate_bundle(_bundle("1.0.0"), factory)
    service = TaskService(
        store=SQLiteExecutionLedger(tmp_path / "ledger.db"),
        registry_generation=registry.generation,
    )
    task = await service.create_task(
        objective="Cancel cooperatively",
        agent_id="default",
    )
    supervisor = TaskRuntimeSupervisor()
    orchestrator = TaskRuntimeOrchestrator(
        service,
        registry,
        supervisor,
    )
    await orchestrator.start(
        task,
        planner_id=_PLANNER_ID,
        runner_id=_RUNNER_ID,
        strategy_id=_STRATEGY_ID,
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    cancelled = await orchestrator.cancel(task.task_id)

    assert cancelled.status is TaskStatus.CANCELLED
    assert observed_reason == ["Task cancelled by user"]
    assert observed_strategy == [{"mode": "runtime-test"}]
    events = await service.list_events(task.task_id)
    assert "run.completed" not in {event.event_type for event in events}


@pytest.mark.asyncio
async def test_orchestrator_resumes_checkpoint_in_new_run(
    tmp_path: Path,
) -> None:
    """Resume inherits the checkpointed run's runner and strategy."""
    first_started = asyncio.Event()
    resumed_release = asyncio.Event()

    class _ResumeRunner:
        runner_id = _RUNNER_ID

        def __init__(self) -> None:
            self.orders = []

        async def health_check(self) -> bool:
            return True

        async def execute(self, order, run):
            self.orders.append(order)
            yield RunnerSignal(
                event_type="runner.progress",
                payload={"attempt": run.attempt},
            )
            if run.attempt == 1:
                first_started.set()
                await asyncio.Event().wait()
            await resumed_release.wait()

    runner = _ResumeRunner()
    registry = GenerationRegistry()

    def factory(declaration):
        if declaration.contribution_id == "planner":
            return _Planner()
        if declaration.contribution_id == "strategy":
            return _Strategy()
        return runner

    await registry.activate_bundle(_bundle("1.0.0"), factory)
    service = TaskService(
        store=SQLiteExecutionLedger(tmp_path / "ledger.db"),
        registry_generation=registry.generation,
    )
    task = await service.create_task(
        objective="Resume through the selected runner",
        agent_id="default",
    )
    supervisor = TaskRuntimeSupervisor()
    orchestrator = TaskRuntimeOrchestrator(
        service,
        registry,
        supervisor,
    )
    await orchestrator.start(
        task,
        planner_id=_PLANNER_ID,
        runner_id=_RUNNER_ID,
        strategy_id=_STRATEGY_ID,
    )
    await asyncio.wait_for(first_started.wait(), timeout=1)

    checkpoint = await orchestrator.suspend(
        task.task_id,
        runner_cursor={"next_step": 2},
    )
    suspended = await service.get_task(task.task_id)
    assert suspended is not None
    assert suspended.status is TaskStatus.SUSPENDED

    resumed_run, concurrent_replay = await asyncio.gather(
        orchestrator.resume(
            suspended,
            idempotency_key="resume-once",
        ),
        orchestrator.resume(
            suspended,
            idempotency_key="resume-once",
        ),
    )
    assert concurrent_replay.run_id == resumed_run.run_id
    assert resumed_run.attempt == 2
    assert resumed_run.strategy_id == _STRATEGY_ID
    assert resumed_run.checkpoint_id == checkpoint.checkpoint_id
    for _ in range(100):
        if len(runner.orders) == 2:
            break
        await asyncio.sleep(0.01)
    assert len(runner.orders) == 2
    assert runner.orders[-1].metadata["resume_checkpoint"][
        "runner_cursor"
    ] == {"next_step": 2}
    resumed_release.set()
    for _ in range(100):
        restored = await service.get_task(task.task_id)
        if restored is not None and restored.status is TaskStatus.COMPLETED:
            break
        await asyncio.sleep(0.01)

    assert restored is not None
    assert restored.status is TaskStatus.COMPLETED
    await _assert_completed_resume_replay(
        registry,
        orchestrator,
        suspended,
        resumed_run,
        runner.orders,
    )
    runs = await service.list_runs(task.task_id)
    assert [run.attempt for run in runs] == [1, 2]
    assert not supervisor.contains(task.task_id)


@pytest.mark.asyncio
async def test_resume_resolves_runner_from_new_generation(
    tmp_path: Path,
) -> None:
    """A recovered attempt uses the current implementation of the same ID."""
    first_started = asyncio.Event()

    class _GenerationRunner:
        runner_id = _RUNNER_ID

        def __init__(self, label: str) -> None:
            self.label = label
            self.attempts: list[int] = []

        async def health_check(self) -> bool:
            return True

        async def execute(self, _order, run):
            self.attempts.append(run.attempt)
            yield RunnerSignal(
                event_type="runner.progress",
                payload={"implementation": self.label},
            )
            if run.attempt == 1:
                first_started.set()
                await asyncio.Event().wait()

    first_runner = _GenerationRunner("v1")
    replacement_runner = _GenerationRunner("v2")
    selected_runner = first_runner
    registry = GenerationRegistry()

    def factory(declaration):
        if declaration.contribution_id == "planner":
            return _Planner()
        if declaration.contribution_id == "strategy":
            return _Strategy()
        return selected_runner

    await registry.activate_bundle(_bundle("1.0.0"), factory)
    service = TaskService(
        store=SQLiteExecutionLedger(tmp_path / "ledger.db"),
        registry_generation=registry.generation,
    )
    task = await service.create_task(
        objective="Resume through a replacement runner",
        agent_id="default",
    )
    supervisor = TaskRuntimeSupervisor()
    orchestrator = TaskRuntimeOrchestrator(
        service,
        registry,
        supervisor,
    )
    first_run = await orchestrator.start(
        task,
        planner_id=_PLANNER_ID,
        runner_id=_RUNNER_ID,
        strategy_id=_STRATEGY_ID,
    )
    await asyncio.wait_for(first_started.wait(), timeout=1)
    await orchestrator.suspend(task.task_id, runner_cursor={"step": 2})

    selected_runner = replacement_runner
    await registry.activate_bundle(_bundle("2.0.0"), factory)
    suspended = await service.get_task(task.task_id)
    assert suspended is not None
    resumed_run = await orchestrator.resume(suspended)

    for _ in range(100):
        completed = await service.get_task(task.task_id)
        if completed is not None and completed.status is TaskStatus.COMPLETED:
            break
        await asyncio.sleep(0.01)

    assert completed is not None
    assert completed.status is TaskStatus.COMPLETED
    assert first_run.registry_generation == 2
    assert resumed_run.registry_generation == 3
    assert first_runner.attempts == [1]
    assert replacement_runner.attempts == [2]
    assert registry.retained_generations() == (3,)


@pytest.mark.asyncio
async def test_new_resume_fails_closed_when_runner_was_removed(
    tmp_path: Path,
) -> None:
    release = asyncio.Event()
    registry = GenerationRegistry()

    def factory(declaration):
        if declaration.contribution_id == "planner":
            return _Planner()
        if declaration.contribution_id == "strategy":
            return _Strategy()
        return _Runner(release)

    await registry.activate_bundle(_bundle("1.0.0"), factory)
    service = TaskService(
        store=SQLiteExecutionLedger(tmp_path / "ledger.db"),
        registry_generation=registry.generation,
    )
    task = await service.create_task(
        objective="Fail closed after runner removal",
        agent_id="default",
    )
    orchestrator = TaskRuntimeOrchestrator(
        service,
        registry,
        TaskRuntimeSupervisor(),
    )
    await orchestrator.start(
        task,
        planner_id=_PLANNER_ID,
        runner_id=_RUNNER_ID,
        strategy_id=_STRATEGY_ID,
    )
    await orchestrator.suspend(task.task_id, runner_cursor={"step": 2})
    await registry.activate_bundle(
        CapabilityBundle(
            provider_id="runtime-test",
            provider_kind=CapabilityProviderKind.SYSTEM,
            version="2.0.0",
            contributions=(),
        ),
        lambda _declaration: object(),
    )
    suspended = await service.get_task(task.task_id)
    assert suspended is not None

    with pytest.raises(
        RunnerCapabilityUnavailableError,
        match="not present in the pinned registry generation",
    ):
        await orchestrator.resume(suspended)

    current = await service.get_task(task.task_id)
    assert current is not None
    assert current.status is TaskStatus.SUSPENDED
    assert len(await service.list_runs(task.task_id)) == 1


@pytest.mark.asyncio
async def test_orchestrator_rejects_resume_after_retry_budget(
    tmp_path: Path,
) -> None:
    contract = ExecutionContract(
        goal="Do not retry",
        budget=ExecutionBudget(max_retries=0),
    )
    service = TaskService(
        store=SQLiteExecutionLedger(tmp_path / "ledger.db"),
        registry_generation=1,
    )
    task = await service.create_task(
        objective=contract.goal,
        agent_id="default",
        execution_contract=contract,
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Run", objective=task.objective),),
    )
    await service.start_task(task.task_id, runner_id=_RUNNER_ID)
    await service.suspend_task(task.task_id)
    suspended = await service.get_task(task.task_id)
    assert suspended is not None

    orchestrator = TaskRuntimeOrchestrator(
        service,
        GenerationRegistry(),
        TaskRuntimeSupervisor(),
    )
    with pytest.raises(
        TaskExecutionBudgetExceededError,
        match="retry budget",
    ):
        await orchestrator.resume(suspended)

    assert len(await service.list_runs(task.task_id)) == 1
