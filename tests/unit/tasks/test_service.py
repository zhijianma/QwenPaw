# -*- coding: utf-8 -*-
"""Tests for the durable Lite task application service."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from qwenpaw.kernel.models import (
    ActorRef,
    ActorType,
    ApprovalContinuation,
    ApprovalDecisionValue,
    ApprovalStatus,
    ExecutionBudget,
    ExecutionContract,
    PlanStep,
    RiskLevel,
    RunnerSignal,
    RunStatus,
    TaskStatus,
)
from qwenpaw.kernel.state_machine import InvalidTaskTransition
from qwenpaw.tasks.ledger import SQLiteExecutionLedger
from qwenpaw.tasks.service import TaskService


async def _service(database_path: Path) -> TaskService:
    store = SQLiteExecutionLedger(database_path)
    await store.initialize()
    return TaskService(store=store, registry_generation=1)


@pytest.mark.asyncio
async def test_task_service_persists_full_approval_lifecycle(
    tmp_path: Path,
) -> None:
    service = await _service(tmp_path / "ledger.db")
    actor = ActorRef(type=ActorType.USER, id="local-user")

    created = await service.create_task(
        objective="Prepare a release report",
        agent_id="default",
    )
    planned, plan = await service.plan_task(
        created.task_id,
        steps=(
            PlanStep(
                title="Prepare",
                objective="Prepare the report",
            ),
        ),
    )
    running, run = await service.start_task(
        created.task_id,
        runner_id="runner.local",
    )
    approval = await service.request_approval(
        created.task_id,
        action="file.write",
        risk=RiskLevel.MEDIUM,
        requester=actor,
        redacted_arguments={"path": "report.md"},
    )
    waiting = await service.get_task(created.task_id)
    decision = await service.decide_approval(
        created.task_id,
        approval.approval_id,
        decision=ApprovalDecisionValue.APPROVED,
        actor=actor,
        reason="Approved for this task",
    )
    completed = await service.complete_task(created.task_id)

    assert created.status is TaskStatus.CREATED
    assert planned.status is TaskStatus.PLANNED
    assert plan.task_id == created.task_id
    assert running.status is TaskStatus.RUNNING
    assert run.status is RunStatus.RUNNING
    assert waiting is not None
    assert waiting.status is TaskStatus.WAITING_APPROVAL
    assert decision.decision is ApprovalDecisionValue.APPROVED
    assert completed.status is TaskStatus.COMPLETED

    events = await service.list_events(created.task_id)
    assert [event.event_type for event in events] == [
        "task.created",
        "task.planned",
        "run.started",
        "approval.requested",
        "approval.decided",
        "run.resumed",
        "run.completed",
    ]
    assert run.invocation_id is not None
    assert run.correlation_id is not None
    assert approval.invocation_id == run.invocation_id
    assert {event.invocation_id for event in events[2:]} == {run.invocation_id}
    assert {event.correlation_id for event in events[2:]} == {
        run.correlation_id,
    }


@pytest.mark.asyncio
async def test_task_state_survives_service_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "ledger.db"
    service = await _service(database_path)
    task = await service.create_task(
        objective="Persist across restart",
        agent_id="default",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Persist", objective="Persist state"),),
    )

    reopened = await _service(database_path)
    restored = await reopened.get_task(task.task_id)

    assert restored is not None
    assert restored.status is TaskStatus.PLANNED
    assert restored.version == 2


@pytest.mark.asyncio
async def test_execution_contract_survives_service_restart(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "ledger.db"
    service = await _service(database_path)
    contract = ExecutionContract(
        goal="Persist the execution contract",
        acceptance=("Contract is restored",),
    )
    task = await service.create_task(
        objective=contract.goal,
        agent_id="default",
        acceptance_criteria=contract.acceptance,
        execution_contract=contract,
    )

    reopened = await _service(database_path)
    restored = await reopened.get_task(task.task_id)

    assert restored is not None
    assert restored.execution_contract == contract


@pytest.mark.asyncio
async def test_runner_signal_preserves_causal_identity(
    tmp_path: Path,
) -> None:
    service = await _service(tmp_path / "ledger.db")
    task = await service.create_task(
        objective="Trace one runner action",
        agent_id="default",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Trace", objective="Trace the action"),),
    )
    _, run = await service.start_task(
        task.task_id,
        runner_id="runner.local",
    )
    events = await service.list_events(task.task_id)
    step_id = uuid4()
    cause_event_id = events[-1].event_id
    correlation_id = uuid4()
    forged_invocation_id = uuid4()

    event = await service.record_runner_signal(
        task.task_id,
        run.run_id,
        RunnerSignal(
            event_type="runner.progress",
            invocation_id=forged_invocation_id,
            step_id=step_id,
            cause_event_id=cause_event_id,
            correlation_id=correlation_id,
        ),
    )

    assert event.step_id == step_id
    assert event.source == "runner.local"
    assert event.cause_event_id == cause_event_id
    assert event.correlation_id == correlation_id
    assert event.invocation_id == run.invocation_id
    assert event.invocation_id != forged_invocation_id


@pytest.mark.asyncio
async def test_resume_from_safe_checkpoint_creates_new_attempt(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "ledger.db"
    service = await _service(database_path)
    task = await service.create_task(
        objective="Resume a local task",
        agent_id="default",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Work", objective="Perform work"),),
    )
    _, first_run = await service.start_task(
        task.task_id,
        runner_id="runner.local",
    )
    checkpoint = await service.suspend_task(
        task.task_id,
        runner_cursor={"next_step": 1},
        workspace_checkpoint_ref="refs/qwenpaw/snap/resume",
    )

    reopened = await _service(database_path)
    resumed, second_run = await reopened.resume_task(task.task_id)
    runs = await reopened.list_runs(task.task_id)

    assert checkpoint.run_id == first_run.run_id
    assert resumed.status is TaskStatus.RUNNING
    assert second_run.attempt == 2
    assert second_run.run_id != first_run.run_id
    assert first_run.correlation_id is not None
    assert second_run.correlation_id == first_run.correlation_id
    assert first_run.invocation_id is not None
    assert second_run.invocation_id is not None
    assert second_run.invocation_id != first_run.invocation_id
    assert second_run.checkpoint_id == checkpoint.checkpoint_id
    assert [run.status for run in runs] == [
        RunStatus.SUSPENDED,
        RunStatus.RUNNING,
    ]


@pytest.mark.asyncio
async def test_resume_inherits_durable_execution_deadline(
    tmp_path: Path,
) -> None:
    started_at = datetime(2026, 10, 9, 8, 0, tzinfo=timezone.utc)
    current = [started_at]
    store = SQLiteExecutionLedger(tmp_path / "ledger.db")
    await store.initialize()
    service = TaskService(
        store=store,
        registry_generation=1,
        clock=lambda: current[0],
    )
    contract = ExecutionContract(
        goal="Resume within one duration budget",
        budget=ExecutionBudget(max_duration_seconds=60),
    )
    task = await service.create_task(
        objective=contract.goal,
        agent_id="default",
        execution_contract=contract,
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Work", objective=task.objective),),
    )
    _, first_run = await service.start_task(
        task.task_id,
        runner_id="runner.local",
    )
    await service.suspend_task(task.task_id)

    current[0] = started_at + timedelta(seconds=20)
    reopened = TaskService(
        store=store,
        registry_generation=2,
        clock=lambda: current[0],
    )
    _, resumed_run = await reopened.resume_task(task.task_id)

    expected = started_at + timedelta(seconds=60)
    assert first_run.execution_deadline_at == expected
    assert resumed_run.execution_deadline_at == expected
    assert resumed_run.started_at == current[0]
    assert resumed_run.registry_generation == 2


@pytest.mark.asyncio
async def test_active_checkpoint_is_bounded_and_idempotent(
    tmp_path: Path,
) -> None:
    service = await _service(tmp_path / "ledger.db")
    task = await service.create_task(
        objective="Persist a plugin checkpoint",
        agent_id="default",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Work", objective=task.objective),),
    )
    _, run = await service.start_task(
        task.task_id,
        runner_id="runner.plugin",
    )

    first = await service.create_checkpoint(
        task.task_id,
        run.run_id,
        runner_cursor={"next_step": 2},
        workspace_checkpoint_ref="refs/qwenpaw/snap/plugin",
        idempotency_key="checkpoint-1",
    )
    retry = await service.create_checkpoint(
        task.task_id,
        run.run_id,
        runner_cursor={"next_step": 2},
        workspace_checkpoint_ref="refs/qwenpaw/snap/plugin",
        idempotency_key="checkpoint-1",
    )

    current = await service.get_task(task.task_id)
    events = await service.list_events(task.task_id)
    assert first == retry
    assert current is not None
    assert current.status is TaskStatus.RUNNING
    assert (
        sum(event.event_type == "checkpoint.created" for event in events) == 1
    )
    assert events[-1].payload == {
        "checkpoint_id": str(first.checkpoint_id),
    }
    assert events[-1].correlation_id == run.correlation_id

    with pytest.raises(ValueError, match="exceeds 32 KiB"):
        await service.create_checkpoint(
            task.task_id,
            run.run_id,
            runner_cursor={"state": "x" * (33 * 1024)},
        )


@pytest.mark.asyncio
async def test_resume_retry_replays_same_attempt(tmp_path: Path) -> None:
    service = await _service(tmp_path / "ledger.db")
    task = await service.create_task(
        objective="Resume exactly once",
        agent_id="default",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Work", objective="Perform work"),),
    )
    await service.start_task(task.task_id, runner_id="runner.local")
    await service.suspend_task(task.task_id)

    first = await service.resume_task(
        task.task_id,
        idempotency_key="resume-1",
    )
    retry = await service.resume_task(
        task.task_id,
        idempotency_key="resume-1",
    )

    assert first == retry
    assert len(await service.list_runs(task.task_id)) == 2


@pytest.mark.asyncio
async def test_cancel_and_invalid_transition_are_durable(
    tmp_path: Path,
) -> None:
    service = await _service(tmp_path / "ledger.db")
    task = await service.create_task(
        objective="Cancel a task",
        agent_id="default",
    )
    cancelled = await service.cancel_task(task.task_id)

    assert cancelled.status is TaskStatus.CANCELLED
    with pytest.raises(InvalidTaskTransition):
        await service.plan_task(
            task.task_id,
            steps=(PlanStep(title="Too late", objective="Must fail"),),
        )

    events = await service.list_events(task.task_id)
    assert [event.event_type for event in events] == [
        "task.created",
        "task.cancelled",
    ]


@pytest.mark.asyncio
async def test_cancel_resolves_pending_run_approvals(
    tmp_path: Path,
) -> None:
    service = await _service(tmp_path / "ledger.db")
    task = await service.create_task(
        objective="Cancel while approval is pending",
        agent_id="default",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Guard", objective="Request permission"),),
    )
    await service.start_task(task.task_id, runner_id="runner.local")
    approval = await service.request_approval(
        task.task_id,
        action="tool.read",
        risk=RiskLevel.LOW,
        requester=ActorRef(type=ActorType.AGENT, id="default"),
    )

    cancelled = await service.cancel_task(task.task_id)

    record = await service.get_approval(approval.approval_id)
    pending = await service.list_approvals(
        task.task_id,
        status=ApprovalStatus.PENDING,
    )
    events = await service.list_events(task.task_id)
    assert cancelled.status is TaskStatus.CANCELLED
    assert record is not None
    assert record[1] is not None
    assert record[1].decision is ApprovalDecisionValue.CANCELLED
    assert pending == []
    assert [event.event_type for event in events[-2:]] == [
        "approval.decided",
        "run.cancelled",
    ]


@pytest.mark.asyncio
async def test_denied_approval_fails_task_and_run(tmp_path: Path) -> None:
    service = await _service(tmp_path / "ledger.db")
    actor = ActorRef(type=ActorType.USER, id="local-user")
    task = await service.create_task(
        objective="Request a risky action",
        agent_id="default",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Risk", objective="Perform action"),),
    )
    await service.start_task(task.task_id, runner_id="runner.local")
    approval = await service.request_approval(
        task.task_id,
        action="shell.execute",
        risk=RiskLevel.HIGH,
        requester=actor,
    )

    await service.decide_approval(
        task.task_id,
        approval.approval_id,
        decision=ApprovalDecisionValue.DENIED,
        actor=actor,
        reason="Not allowed",
    )
    failed = await service.get_task(task.task_id)
    runs = await service.list_runs(task.task_id)

    assert failed is not None
    assert failed.status is TaskStatus.FAILED
    assert runs[-1].status is RunStatus.FAILED


@pytest.mark.asyncio
async def test_tool_denial_resumes_task_for_agent_explanation(
    tmp_path: Path,
) -> None:
    service = await _service(tmp_path / "ledger.db")
    actor = ActorRef(type=ActorType.AGENT, id="default")
    task = await service.create_task(
        objective="Request a guarded tool",
        agent_id="default",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Guard", objective="Request permission"),),
    )
    await service.start_task(task.task_id, runner_id="runner.local")
    approval = await service.request_approval(
        task.task_id,
        action="tool.shell_execute",
        risk=RiskLevel.HIGH,
        requester=actor,
        continuation=ApprovalContinuation.RESUME_ON_DECISION,
    )

    await service.decide_approval(
        task.task_id,
        approval.approval_id,
        decision=ApprovalDecisionValue.DENIED,
        actor=ActorRef(type=ActorType.USER, id="local-user"),
        reason="Do not execute the command",
    )

    resumed = await service.get_task(task.task_id)
    runs = await service.list_runs(task.task_id)
    events = await service.list_events(task.task_id)
    assert resumed is not None
    assert resumed.status is TaskStatus.RUNNING
    assert runs[-1].status is RunStatus.RUNNING
    assert [event.event_type for event in events][-2:] == [
        "approval.decided",
        "run.resumed",
    ]
    assert events[-2].payload["decision"] == "denied"


@pytest.mark.asyncio
async def test_multiple_approvals_resume_only_after_last_decision(
    tmp_path: Path,
) -> None:
    service = await _service(tmp_path / "ledger.db")
    actor = ActorRef(type=ActorType.AGENT, id="default")
    task = await service.create_task(
        objective="Run two guarded tools",
        agent_id="default",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Guard", objective="Request permission"),),
    )
    await service.start_task(task.task_id, runner_id="runner.local")
    first = await service.request_approval(
        task.task_id,
        action="tool.first",
        risk=RiskLevel.HIGH,
        requester=actor,
        continuation=ApprovalContinuation.RESUME_ON_DECISION,
    )
    second = await service.request_approval(
        task.task_id,
        action="tool.second",
        risk=RiskLevel.MEDIUM,
        requester=actor,
        continuation=ApprovalContinuation.RESUME_ON_DECISION,
    )

    pending = await service.list_approvals(
        task.task_id,
        status=ApprovalStatus.PENDING,
    )
    assert [record[0].approval_id for record in pending] == [
        first.approval_id,
        second.approval_id,
    ]

    await service.decide_approval(
        task.task_id,
        first.approval_id,
        decision=ApprovalDecisionValue.APPROVED,
        actor=ActorRef(type=ActorType.USER, id="local-user"),
        reason="Approve the first tool",
    )
    waiting = await service.get_task(task.task_id)
    events = await service.list_events(task.task_id)
    assert waiting is not None
    assert waiting.status is TaskStatus.WAITING_APPROVAL
    assert events[-1].event_type == "approval.decided"

    await service.decide_approval(
        task.task_id,
        second.approval_id,
        decision=ApprovalDecisionValue.DENIED,
        actor=ActorRef(type=ActorType.USER, id="local-user"),
        reason="Deny the second tool",
    )
    resumed = await service.get_task(task.task_id)
    pending = await service.list_approvals(
        task.task_id,
        status=ApprovalStatus.PENDING,
    )
    assert resumed is not None
    assert resumed.status is TaskStatus.RUNNING
    assert not pending
    events = await service.list_events(task.task_id)
    assert [event.event_type for event in events][-2:] == [
        "approval.decided",
        "run.resumed",
    ]


@pytest.mark.asyncio
async def test_parallel_approval_requests_from_separate_services(
    tmp_path: Path,
) -> None:
    """Concurrent producers retry optimistic Task Ledger conflicts."""
    database_path = tmp_path / "ledger.db"
    first_service = await _service(database_path)
    task = await first_service.create_task(
        objective="Request permissions concurrently",
        agent_id="default",
    )
    await first_service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Guard", objective="Request permission"),),
    )
    await first_service.start_task(task.task_id, runner_id="runner.local")
    second_service = await _service(database_path)
    actor = ActorRef(type=ActorType.AGENT, id="default")

    first, second = await asyncio.gather(
        first_service.request_approval(
            task.task_id,
            action="tool.first",
            risk=RiskLevel.LOW,
            requester=actor,
        ),
        second_service.request_approval(
            task.task_id,
            action="driver.second",
            risk=RiskLevel.MEDIUM,
            requester=actor,
        ),
    )

    pending = await first_service.list_approvals(
        task.task_id,
        status=ApprovalStatus.PENDING,
    )
    assert {record[0].approval_id for record in pending} == {
        first.approval_id,
        second.approval_id,
    }
    events = await first_service.list_events(task.task_id)
    sequences = [event.sequence for event in events]
    assert sequences == list(range(1, len(events) + 1))


@pytest.mark.asyncio
async def test_rejected_blocking_approval_cancels_other_requests(
    tmp_path: Path,
) -> None:
    service = await _service(tmp_path / "ledger.db")
    actor = ActorRef(type=ActorType.AGENT, id="default")
    task = await service.create_task(
        objective="Reject one blocking action",
        agent_id="default",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Guard", objective="Request permission"),),
    )
    await service.start_task(task.task_id, runner_id="runner.local")
    blocking = await service.request_approval(
        task.task_id,
        action="proposal.execute",
        risk=RiskLevel.HIGH,
        requester=actor,
    )
    remaining = await service.request_approval(
        task.task_id,
        action="tool.second",
        risk=RiskLevel.MEDIUM,
        requester=actor,
        continuation=ApprovalContinuation.RESUME_ON_DECISION,
    )

    await service.decide_approval(
        task.task_id,
        blocking.approval_id,
        decision=ApprovalDecisionValue.DENIED,
        actor=ActorRef(type=ActorType.USER, id="local-user"),
        reason="Reject the blocking action",
    )

    failed = await service.get_task(task.task_id)
    cancelled_record = await service.get_approval(remaining.approval_id)
    assert failed is not None
    assert failed.status is TaskStatus.FAILED
    assert cancelled_record is not None
    assert cancelled_record[1] is not None
    assert cancelled_record[1].decision is ApprovalDecisionValue.CANCELLED


@pytest.mark.asyncio
async def test_approval_retry_replays_same_decision(tmp_path: Path) -> None:
    service = await _service(tmp_path / "ledger.db")
    actor = ActorRef(type=ActorType.USER, id="local-user")
    task = await service.create_task(
        objective="Approve exactly once",
        agent_id="default",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Risk", objective="Perform action"),),
    )
    await service.start_task(task.task_id, runner_id="runner.local")
    approval = await service.request_approval(
        task.task_id,
        action="shell.execute",
        risk=RiskLevel.HIGH,
        requester=actor,
    )
    arguments = {
        "decision": ApprovalDecisionValue.APPROVED,
        "actor": actor,
        "reason": "Approved once",
        "idempotency_key": "approval-1",
    }

    first = await service.decide_approval(
        task.task_id,
        approval.approval_id,
        **arguments,
    )
    retry = await service.decide_approval(
        task.task_id,
        approval.approval_id,
        **arguments,
    )

    assert first == retry
    events = await service.list_events(task.task_id)
    assert [event.event_type for event in events].count(
        "approval.decided",
    ) == 1
