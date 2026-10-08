# -*- coding: utf-8 -*-
"""Tests for the low-boilerplate local runner integration."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from qwenpaw.kernel import BudgetLease, BudgetLeaseStatus
from qwenpaw.kernel.models import (
    ArtifactRef,
    ArtifactRequirement,
    CostAccountingMode,
    EvidenceRef,
    ExecutionBudget,
    ExecutionContract,
    ExitCondition,
    PlanStep,
    Run,
    RunnerSignal,
    RunStatus,
    RuntimeContext,
    TaskOrder,
    TaskStatus,
)
from qwenpaw.plugins.architecture import PluginManifest
from qwenpaw.plugins.generations import GenerationRegistry
from qwenpaw.tasks.ledger import SQLiteExecutionLedger
from qwenpaw.tasks.runner import (
    LocalAgentRunner,
    RunnerCapabilityUnavailableError,
    TaskExecutionCoordinator,
    TaskExecutionBudgetExceededError,
    TaskExecutionTimeoutError,
)
from qwenpaw.tasks.results import CompletionRequirementsError
from qwenpaw.tasks.service import TaskService


async def _signals(
    order: TaskOrder,
    run: Run,
) -> AsyncIterator[RunnerSignal]:
    artifact = ArtifactRef(
        kind="report",
        uri="qwenpaw-artifact://sha256/example",
        media_type="text/plain",
        content_hash="0" * 64,
        size_bytes=12,
    )
    yield RunnerSignal(
        event_type="tool.completed",
        payload={
            "task_id": str(order.task_id),
            "run_id": str(run.run_id),
        },
        artifact_refs=(artifact,),
        evidence_refs=(
            EvidenceRef(
                artifact_id=artifact.artifact_id,
                claim="The report was generated",
                producer="runner.example",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_local_runner_needs_only_an_async_signal_generator(
    tmp_path: Path,
) -> None:
    store = SQLiteExecutionLedger(tmp_path / "ledger.db")
    service = TaskService(store=store, registry_generation=3)
    task = await service.create_task(
        objective="Exercise the extension boundary",
        agent_id="default",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Run", objective="Yield a signal"),),
    )
    order = TaskOrder(
        task_id=task.task_id,
        objective=task.objective,
    )
    runner = LocalAgentRunner("runner.example", _signals)

    completed_run = await TaskExecutionCoordinator(
        service,
        GenerationRegistry(),
    ).execute(
        order,
        runner,
    )

    stored_task = await service.get_task(task.task_id)
    events = await service.list_events(task.task_id)
    assert stored_task is not None
    assert stored_task.status is TaskStatus.COMPLETED
    assert completed_run.status is RunStatus.SUCCEEDED
    assert [event.event_type for event in events][-3:] == [
        "run.started",
        "tool.completed",
        "run.completed",
    ]
    assert events[-2].evidence_refs[0].claim == "The report was generated"


@pytest.mark.asyncio
async def test_coordinator_fails_task_without_persisting_exception_text(
    tmp_path: Path,
) -> None:
    async def fail(
        order: TaskOrder,
        run: Run,
    ) -> AsyncIterator[RunnerSignal]:
        del run
        if order.objective == "emit-before-failure":
            yield RunnerSignal(event_type="runner.progress")
        raise RuntimeError("sensitive failure details")

    store = SQLiteExecutionLedger(tmp_path / "ledger.db")
    service = TaskService(store=store, registry_generation=1)
    task = await service.create_task(
        objective="Fail safely",
        agent_id="default",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Fail", objective="Raise safely"),),
    )
    runner = LocalAgentRunner("runner.failing", fail)

    with pytest.raises(RuntimeError, match="sensitive failure details"):
        await TaskExecutionCoordinator(
            service,
            GenerationRegistry(),
        ).execute(
            TaskOrder(task_id=task.task_id, objective=task.objective),
            runner,
        )

    events = await service.list_events(task.task_id)
    assert events[-1].event_type == "run.failed"
    assert events[-1].payload == {"error_summary": "RuntimeError"}


@pytest.mark.asyncio
async def test_coordinator_rejects_unfulfilled_result_contract(
    tmp_path: Path,
) -> None:
    async def no_results(
        order: TaskOrder,
        run: Run,
    ) -> AsyncIterator[RunnerSignal]:
        del order, run
        yield RunnerSignal(event_type="runner.progress")

    contract = ExecutionContract(
        goal="Produce a verified report",
        required_artifacts=(ArtifactRequirement(kind="report"),),
    )
    store = SQLiteExecutionLedger(tmp_path / "ledger.db")
    service = TaskService(store=store, registry_generation=1)
    task = await service.create_task(
        objective=contract.goal,
        agent_id="default",
        execution_contract=contract,
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Report", objective="Produce report"),),
    )

    with pytest.raises(CompletionRequirementsError):
        await TaskExecutionCoordinator(
            service,
            GenerationRegistry(),
        ).execute(
            TaskOrder(
                task_id=task.task_id,
                objective=task.objective,
                execution_contract=contract,
            ),
            LocalAgentRunner("runner.empty", no_results),
        )

    stored = await service.get_task(task.task_id)
    events = await service.list_events(task.task_id)
    assert stored is not None
    assert stored.status is TaskStatus.FAILED
    assert events[-1].event_type == "run.failed"
    assert events[-1].payload == {
        "error_summary": "CompletionRequirementsError",
    }


@pytest.mark.asyncio
async def test_coordinator_pins_plugin_generation_for_entire_run(
    tmp_path: Path,
) -> None:
    def manifest(version: str) -> PluginManifest:
        return PluginManifest.from_dict(
            {
                "schema_version": "qwenpaw.plugin.v2",
                "id": "runner-plugin",
                "version": version,
                "contributions": [
                    {
                        "id": "runner",
                        "slot": "runner",
                        "entrypoint": "runner_plugin:create",
                    },
                ],
            },
        )

    registry = GenerationRegistry()
    initial_runner = LocalAgentRunner("runner-plugin.runner", _signals)
    await registry.activate(manifest("1.0.0"), lambda _: initial_runner)

    async def upgrade_during_run(
        order: TaskOrder,
        run: Run,
    ) -> AsyncIterator[RunnerSignal]:
        del order
        assert run.registry_generation == 2
        upgraded_runner = LocalAgentRunner(
            "runner-plugin.runner",
            _signals,
        )
        await registry.activate(
            manifest("2.0.0"),
            lambda _: upgraded_runner,
        )
        assert registry.retained_generations() == (2, 3)
        yield RunnerSignal(event_type="runner.progress")

    store = SQLiteExecutionLedger(tmp_path / "ledger.db")
    service = TaskService(store=store, registry_generation=1)
    task = await service.create_task(
        objective="Keep the old generation alive",
        agent_id="default",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Run", objective=task.objective),),
    )
    runner = LocalAgentRunner("runner.example", upgrade_during_run)

    completed = await TaskExecutionCoordinator(service, registry).execute(
        TaskOrder(task_id=task.task_id, objective=task.objective),
        runner,
    )

    assert completed.registry_generation == 2
    assert registry.retained_generations() == (3,)


@pytest.mark.asyncio
async def test_coordinator_executes_selected_contributed_runner(
    tmp_path: Path,
) -> None:
    manifest = PluginManifest.from_dict(
        {
            "schema_version": "qwenpaw.plugin.v2",
            "id": "runner-plugin",
            "version": "1.0.0",
            "contributions": [
                {
                    "id": "summary",
                    "slot": "runner",
                    "entrypoint": "runner_plugin:create",
                },
            ],
        },
    )

    async def selected_signals(
        order: TaskOrder,
        run: Run,
    ) -> AsyncIterator[RunnerSignal]:
        yield RunnerSignal(
            event_type="plugin.runner-plugin.summary",
            payload={
                "objective": order.objective,
                "generation": run.registry_generation,
            },
        )

    registry = GenerationRegistry()
    await registry.activate(
        manifest,
        lambda _: LocalAgentRunner(
            "runner-plugin.summary",
            selected_signals,
        ),
    )
    store = SQLiteExecutionLedger(tmp_path / "ledger.db")
    service = TaskService(store=store, registry_generation=1)
    task = await service.create_task(
        objective="Use the contributed runner",
        agent_id="default",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Summarize", objective=task.objective),),
    )

    completed = await TaskExecutionCoordinator(
        service,
        registry,
    ).execute_selected(
        TaskOrder(task_id=task.task_id, objective=task.objective),
        "runner-plugin.summary",
    )

    events = await service.list_events(task.task_id)
    assert completed.runner_id == "runner-plugin.summary"
    assert completed.registry_generation == 2
    assert events[-2].event_type == "plugin.runner-plugin.summary"
    assert events[-2].payload["generation"] == 2


@pytest.mark.asyncio
async def test_selected_runner_fails_closed_before_creating_run(
    tmp_path: Path,
) -> None:
    store = SQLiteExecutionLedger(tmp_path / "ledger.db")
    service = TaskService(store=store, registry_generation=1)
    task = await service.create_task(
        objective="Do not fall back silently",
        agent_id="default",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Run", objective=task.objective),),
    )

    with pytest.raises(
        RunnerCapabilityUnavailableError,
        match="not present in the pinned registry generation",
    ):
        await TaskExecutionCoordinator(
            service,
            GenerationRegistry(),
        ).start_selected(
            TaskOrder(task_id=task.task_id, objective=task.objective),
            "missing.runner",
        )

    assert not await service.list_runs(task.task_id)
    stored = await service.get_task(task.task_id)
    assert stored is not None
    assert stored.status is TaskStatus.PLANNED


@pytest.mark.asyncio
async def test_coordinator_timeout_fails_and_cancels_runner(
    tmp_path: Path,
) -> None:
    cancelled = asyncio.Event()

    async def block(
        order: TaskOrder,
        run: Run,
    ) -> AsyncIterator[RunnerSignal]:
        del order, run
        try:
            yield RunnerSignal(event_type="runner.responding")
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    store = SQLiteExecutionLedger(tmp_path / "ledger.db")
    service = TaskService(store=store, registry_generation=1)
    task = await service.create_task(
        objective="Bound the runner lifetime",
        agent_id="default",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Wait", objective=task.objective),),
    )

    with pytest.raises(TaskExecutionTimeoutError):
        await TaskExecutionCoordinator(
            service,
            GenerationRegistry(),
        ).execute(
            TaskOrder(task_id=task.task_id, objective=task.objective),
            LocalAgentRunner("runner.blocking", block),
            timeout_seconds=0.01,
        )

    stored_task = await service.get_task(task.task_id)
    runs = await service.list_runs(task.task_id)
    events = await service.list_events(task.task_id)
    assert cancelled.is_set()
    assert stored_task is not None
    assert stored_task.status is TaskStatus.FAILED
    assert runs[-1].status is RunStatus.FAILED
    assert events[-1].event_type == "run.failed"
    assert events[-1].payload == {
        "error_summary": "TaskExecutionTimeoutError",
    }


@pytest.mark.asyncio
async def test_execution_contract_duration_budget_fails_the_run(
    tmp_path: Path,
) -> None:
    cancelled = asyncio.Event()

    async def block(
        order: TaskOrder,
        run: Run,
    ) -> AsyncIterator[RunnerSignal]:
        del order, run
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        yield RunnerSignal(event_type="runner.unreachable")

    contract = ExecutionContract(
        goal="Bound execution by contract",
        budget=ExecutionBudget(max_duration_seconds=0.01),
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
        steps=(PlanStep(title="Wait", objective=task.objective),),
    )

    with pytest.raises(TaskExecutionBudgetExceededError):
        await TaskExecutionCoordinator(
            service,
            GenerationRegistry(),
        ).execute(
            TaskOrder(
                task_id=task.task_id,
                objective=task.objective,
                execution_contract=contract,
            ),
            LocalAgentRunner("runner.blocking", block),
        )

    stored = await service.get_task(task.task_id)
    events = await service.list_events(task.task_id)
    assert cancelled.is_set()
    assert stored is not None
    assert stored.status is TaskStatus.FAILED
    assert events[-1].payload == {
        "error_summary": "TaskExecutionBudgetExceededError",
    }


@pytest.mark.asyncio
async def test_execution_contract_token_budget_records_then_fails(
    tmp_path: Path,
) -> None:
    async def report_usage(
        _order: TaskOrder,
        _run: Run,
    ) -> AsyncIterator[RunnerSignal]:
        yield RunnerSignal(
            event_type="usage.recorded",
            payload={
                "delta": {
                    "input_tokens": 8,
                    "output_tokens": 3,
                },
            },
        )
        yield RunnerSignal(event_type="runner.unreachable")

    contract = ExecutionContract(
        goal="Bound token usage",
        budget=ExecutionBudget(max_tokens=10),
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

    with pytest.raises(TaskExecutionBudgetExceededError):
        await TaskExecutionCoordinator(
            service,
            GenerationRegistry(),
        ).execute(
            TaskOrder(
                task_id=task.task_id,
                objective=task.objective,
                execution_contract=contract,
            ),
            LocalAgentRunner("runner.usage", report_usage),
        )

    events = await service.list_events(task.task_id)
    usage_events = [
        event for event in events if event.event_type == "usage.recorded"
    ]
    assert len(usage_events) == 1
    assert usage_events[0].payload["total"]["input_tokens"] == 8
    assert usage_events[0].payload["total"]["output_tokens"] == 3
    assert "runner.unreachable" not in {event.event_type for event in events}
    assert events[-1].payload == {
        "error_summary": "TaskExecutionBudgetExceededError",
    }


@pytest.mark.asyncio
async def test_optional_exit_condition_closes_runner_at_safe_result(
    tmp_path: Path,
) -> None:
    closed = False
    artifact = ArtifactRef(
        kind="report",
        uri="qwenpaw-artifact://sha256/optional-report",
        media_type="text/plain",
        content_hash="1" * 64,
        size_bytes=8,
    )

    async def emit_result(
        _order: TaskOrder,
        _run: Run,
    ) -> AsyncIterator[RunnerSignal]:
        nonlocal closed
        try:
            yield RunnerSignal(
                event_type="artifact.created",
                artifact_refs=(artifact,),
            )
            yield RunnerSignal(event_type="runner.unreachable")
        finally:
            closed = True

    contract = ExecutionContract(
        goal="Stop after the optional report",
        exit_conditions=(
            ExitCondition(
                condition_id="report-ready",
                kind="artifact_emitted",
                parameters={"kind": "report"},
                required=False,
            ),
        ),
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

    completed = await TaskExecutionCoordinator(
        service,
        GenerationRegistry(),
    ).execute(
        TaskOrder(
            task_id=task.task_id,
            objective=task.objective,
            execution_contract=contract,
        ),
        LocalAgentRunner("runner.optional", emit_result),
    )

    events = await service.list_events(task.task_id)
    event_types = [event.event_type for event in events]
    trigger = next(
        event
        for event in events
        if event.event_type == "exit_condition.triggered"
    )
    artifact_event = next(
        event for event in events if event.event_type == "artifact.created"
    )
    assert completed.status is RunStatus.SUCCEEDED
    assert closed is True
    assert "runner.unreachable" not in event_types
    assert trigger.payload == {"condition_ids": ["report-ready"]}
    assert trigger.cause_event_id == artifact_event.event_id
    assert trigger.source == "qwenpaw.system.tasks.execution-coordinator"


@pytest.mark.asyncio
async def test_plugin_checkpoint_survives_failed_runner_for_resume(
    tmp_path: Path,
) -> None:
    async def checkpoint_then_fail(
        _order: TaskOrder,
        _run: Run,
        context: RuntimeContext,
    ) -> AsyncIterator[RunnerSignal]:
        assert context.checkpoint_broker is not None
        await context.checkpoint_broker.save(
            runner_cursor={"next_step": 4},
            workspace_checkpoint_ref="refs/qwenpaw/snap/plugin",
            idempotency_key="plugin-checkpoint-1",
        )
        yield RunnerSignal(event_type="runner.progress")
        raise RuntimeError("runner process lost")

    service = TaskService(
        store=SQLiteExecutionLedger(tmp_path / "ledger.db"),
        registry_generation=1,
    )
    task = await service.create_task(
        objective="Recover plugin work",
        agent_id="default",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Run", objective=task.objective),),
    )

    with pytest.raises(RuntimeError, match="runner process lost"):
        await TaskExecutionCoordinator(
            service,
            GenerationRegistry(),
        ).execute(
            TaskOrder(task_id=task.task_id, objective=task.objective),
            LocalAgentRunner(
                "runner.checkpoint-plugin",
                execute_context=checkpoint_then_fail,
            ),
        )

    failed = await service.get_task(task.task_id)
    snapshot = await service.projection_snapshot(task.task_id)
    assert failed is not None
    assert failed.status is TaskStatus.FAILED
    assert snapshot.checkpoint is not None
    assert snapshot.checkpoint.runner_cursor == {"next_step": 4}
    resumed, next_run = await service.resume_task(task.task_id)
    assert resumed.status is TaskStatus.RUNNING
    assert next_run.attempt == 2
    assert next_run.checkpoint_id == snapshot.checkpoint.checkpoint_id


@pytest.mark.asyncio
async def test_contextual_runner_receives_released_budget_lease(
    tmp_path: Path,
) -> None:
    captured: BudgetLease | None = None

    async def inspect_context(
        _order: TaskOrder,
        _run: Run,
        context: RuntimeContext,
    ) -> AsyncIterator[RunnerSignal]:
        nonlocal captured
        assert isinstance(context.usage_meter, BudgetLease)
        captured = context.usage_meter
        yield RunnerSignal(event_type="runner.progress")

    service = TaskService(
        store=SQLiteExecutionLedger(tmp_path / "ledger.db"),
        registry_generation=1,
    )
    task = await service.create_task(
        objective="Inspect budget authorization",
        agent_id="default",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Run", objective=task.objective),),
    )

    await TaskExecutionCoordinator(
        service,
        GenerationRegistry(),
    ).execute(
        TaskOrder(task_id=task.task_id, objective=task.objective),
        LocalAgentRunner(
            "runner.budget-lease",
            execute_context=inspect_context,
        ),
    )

    assert captured is not None
    assert captured.lease_snapshot().status is BudgetLeaseStatus.RELEASED


@pytest.mark.asyncio
async def test_required_exit_condition_does_not_close_runner_early(
    tmp_path: Path,
) -> None:
    artifact = ArtifactRef(
        kind="report",
        uri="qwenpaw-artifact://sha256/required-report",
        media_type="text/plain",
        content_hash="2" * 64,
        size_bytes=8,
    )

    async def emit_result(
        _order: TaskOrder,
        _run: Run,
    ) -> AsyncIterator[RunnerSignal]:
        yield RunnerSignal(
            event_type="artifact.created",
            artifact_refs=(artifact,),
        )
        yield RunnerSignal(event_type="runner.after-required-condition")

    contract = ExecutionContract(
        goal="Continue after the required report",
        exit_conditions=(
            ExitCondition(
                condition_id="report-required",
                kind="artifact_emitted",
                parameters={"kind": "report"},
            ),
        ),
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

    await TaskExecutionCoordinator(service, GenerationRegistry()).execute(
        TaskOrder(
            task_id=task.task_id,
            objective=task.objective,
            execution_contract=contract,
        ),
        LocalAgentRunner("runner.required", emit_result),
    )

    event_types = [
        event.event_type for event in await service.list_events(task.task_id)
    ]
    assert "runner.after-required-condition" in event_types
    assert "exit_condition.triggered" not in event_types


@pytest.mark.asyncio
async def test_optional_exit_waits_for_completion_requirements(
    tmp_path: Path,
) -> None:
    artifact = ArtifactRef(
        kind="report",
        uri="qwenpaw-artifact://sha256/gated-report",
        media_type="text/plain",
        content_hash="3" * 64,
        size_bytes=8,
    )

    async def emit_result(
        _order: TaskOrder,
        _run: Run,
    ) -> AsyncIterator[RunnerSignal]:
        yield RunnerSignal(
            event_type="exit_condition.met",
            payload={"condition_id": "runner-ready"},
        )
        yield RunnerSignal(
            event_type="artifact.created",
            artifact_refs=(artifact,),
        )
        yield RunnerSignal(event_type="runner.unreachable")

    contract = ExecutionContract(
        goal="Wait for the required artifact",
        exit_conditions=(
            ExitCondition(
                condition_id="runner-ready",
                kind="explicit_signal",
                required=False,
            ),
        ),
        required_artifacts=(ArtifactRequirement(kind="report"),),
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

    await TaskExecutionCoordinator(service, GenerationRegistry()).execute(
        TaskOrder(
            task_id=task.task_id,
            objective=task.objective,
            execution_contract=contract,
        ),
        LocalAgentRunner("runner.gated", emit_result),
    )

    events = await service.list_events(task.task_id)
    event_types = [event.event_type for event in events]
    trigger = next(
        event
        for event in events
        if event.event_type == "exit_condition.triggered"
    )
    artifact_event = next(
        event for event in events if event.event_type == "artifact.created"
    )
    assert "runner.unreachable" not in event_types
    assert trigger.cause_event_id == artifact_event.event_id


@pytest.mark.asyncio
async def test_iteration_exit_condition_fails_non_console_runner(
    tmp_path: Path,
) -> None:
    async def iterate(
        _order: TaskOrder,
        _run: Run,
    ) -> AsyncIterator[RunnerSignal]:
        for iteration in range(1, 4):
            yield RunnerSignal(
                event_type="runner.iteration",
                payload={"iteration": iteration},
            )

    contract = ExecutionContract(
        goal="Bound plugin iterations",
        exit_conditions=(
            ExitCondition(
                condition_id="iteration-limit",
                kind="max_iterations",
                parameters={"limit": 2},
            ),
        ),
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
        steps=(PlanStep(title="Iterate", objective=task.objective),),
    )

    with pytest.raises(
        TaskExecutionBudgetExceededError,
        match="max_iterations",
    ):
        await TaskExecutionCoordinator(
            service,
            GenerationRegistry(),
        ).execute(
            TaskOrder(
                task_id=task.task_id,
                objective=task.objective,
                execution_contract=contract,
            ),
            LocalAgentRunner("runner.iterating", iterate),
        )

    events = await service.list_events(task.task_id)
    assert [
        event.payload["iteration"]
        for event in events
        if event.event_type == "runner.iteration"
    ] == [1, 2]
    assert events[-1].payload == {
        "error_summary": "TaskExecutionBudgetExceededError",
    }


@pytest.mark.asyncio
async def test_iteration_events_cannot_replay_low_counter_to_bypass_limit(
    tmp_path: Path,
) -> None:
    async def replay_counter(
        _order: TaskOrder,
        _run: Run,
    ) -> AsyncIterator[RunnerSignal]:
        for _ in range(3):
            yield RunnerSignal(
                event_type="runner.iteration",
                payload={"iteration": 1},
            )

    contract = ExecutionContract(
        goal="Reject replayed iteration counters",
        exit_conditions=(
            ExitCondition(
                condition_id="iteration-limit",
                kind="max_iterations",
                parameters={"limit": 2},
            ),
        ),
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
        steps=(PlanStep(title="Iterate", objective=task.objective),),
    )

    with pytest.raises(TaskExecutionBudgetExceededError):
        await TaskExecutionCoordinator(
            service,
            GenerationRegistry(),
        ).execute(
            TaskOrder(
                task_id=task.task_id,
                objective=task.objective,
                execution_contract=contract,
            ),
            LocalAgentRunner("runner.replaying", replay_counter),
        )

    events = await service.list_events(task.task_id)
    assert sum(event.event_type == "runner.iteration" for event in events) == 2


@pytest.mark.asyncio
async def test_cost_budget_rejects_unknown_runner_before_execution(
    tmp_path: Path,
) -> None:
    executed = False

    async def unpriced(
        _order: TaskOrder,
        _run: Run,
    ) -> AsyncIterator[RunnerSignal]:
        nonlocal executed
        executed = True
        yield RunnerSignal(event_type="runner.unreachable")

    contract = ExecutionContract(
        goal="Fail closed when provider pricing is unavailable",
        budget=ExecutionBudget(max_cost_micros=100),
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

    with pytest.raises(
        TaskExecutionBudgetExceededError,
        match="provider cost is unknown",
    ):
        await TaskExecutionCoordinator(
            service,
            GenerationRegistry(),
        ).execute(
            TaskOrder(
                task_id=task.task_id,
                objective=task.objective,
                execution_contract=contract,
            ),
            LocalAgentRunner("runner.unpriced", unpriced),
        )

    events = await service.list_events(task.task_id)
    usage = next(
        event for event in events if event.event_type == "usage.recorded"
    )
    assert executed is False
    assert usage.payload["delta"]["cost_unknown"] is True
    assert usage.payload["total"]["cost_unknown"] is True
    assert events[-1].payload == {
        "error_summary": "TaskExecutionBudgetExceededError",
    }


@pytest.mark.asyncio
async def test_reported_cost_runner_can_enforce_cost_budget(
    tmp_path: Path,
) -> None:
    async def priced(
        _order: TaskOrder,
        _run: Run,
    ) -> AsyncIterator[RunnerSignal]:
        yield RunnerSignal(
            event_type="usage.recorded",
            payload={"delta": {"cost_micros": 75}},
        )

    contract = ExecutionContract(
        goal="Run with trusted provider cost",
        budget=ExecutionBudget(max_cost_micros=100),
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

    completed = await TaskExecutionCoordinator(
        service,
        GenerationRegistry(),
    ).execute(
        TaskOrder(
            task_id=task.task_id,
            objective=task.objective,
            execution_contract=contract,
        ),
        LocalAgentRunner(
            "runner.priced",
            priced,
            cost_accounting=CostAccountingMode.REPORTED,
        ),
    )

    events = await service.list_events(task.task_id)
    usage = next(
        event for event in events if event.event_type == "usage.recorded"
    )
    assert completed.status is RunStatus.SUCCEEDED
    assert usage.payload["total"]["cost_micros"] == 75
    assert usage.payload["total"]["cost_unknown"] is False


@pytest.mark.asyncio
async def test_execution_contract_tool_budget_stops_before_next_action(
    tmp_path: Path,
) -> None:
    actions: list[int] = []

    async def call_tools(
        _order: TaskOrder,
        _run: Run,
    ) -> AsyncIterator[RunnerSignal]:
        yield RunnerSignal(event_type="tool.started")
        actions.append(1)
        yield RunnerSignal(event_type="tool.completed")
        yield RunnerSignal(event_type="tool.started")
        actions.append(2)

    contract = ExecutionContract(
        goal="Bound tool usage",
        budget=ExecutionBudget(max_tool_calls=1),
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

    with pytest.raises(TaskExecutionBudgetExceededError):
        await TaskExecutionCoordinator(
            service,
            GenerationRegistry(),
        ).execute(
            TaskOrder(
                task_id=task.task_id,
                objective=task.objective,
                execution_contract=contract,
            ),
            LocalAgentRunner("runner.tools", call_tools),
        )

    events = await service.list_events(task.task_id)
    assert actions == [1]
    assert [event.event_type for event in events].count("usage.recorded") == 2
    assert [event.event_type for event in events].count("tool.started") == 1


@pytest.mark.asyncio
async def test_coordinator_rejects_non_positive_timeout_before_start(
    tmp_path: Path,
) -> None:
    store = SQLiteExecutionLedger(tmp_path / "ledger.db")
    service = TaskService(store=store, registry_generation=1)
    task = await service.create_task(
        objective="Reject an invalid timeout",
        agent_id="default",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Wait", objective=task.objective),),
    )

    with pytest.raises(ValueError, match="timeout_seconds"):
        await TaskExecutionCoordinator(
            service,
            GenerationRegistry(),
        ).start(
            TaskOrder(task_id=task.task_id, objective=task.objective),
            LocalAgentRunner("runner.example", _signals),
            timeout_seconds=0,
        )

    stored_task = await service.get_task(task.task_id)
    assert stored_task is not None
    assert stored_task.status is TaskStatus.PLANNED
    assert not await service.list_runs(task.task_id)
