# -*- coding: utf-8 -*-
"""Tests for the transport-neutral Task approval use case."""

from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from qwenpaw.kernel.models import (
    ActorRef,
    ActorType,
    ApprovalDecisionValue,
    PlanStep,
    Proposal,
    RiskLevel,
    TaskStatus,
)
from qwenpaw.tasks.approval_application import (
    DecideTaskApprovalCommand,
    TaskApprovalApplicationService,
    TaskRuntimeLostError,
)
from qwenpaw.tasks.ledger import SQLiteExecutionLedger
from qwenpaw.tasks.service import TaskNotFoundError, TaskService


class _RuntimeBridge:
    """Recording live-runtime approval adapter."""

    def __init__(self, *, live: bool = True) -> None:
        self.live = live
        self.resolutions: list[tuple[UUID, ApprovalDecisionValue, str]] = []

    async def is_live(self, approval_id: UUID) -> bool:
        del approval_id
        return self.live

    async def resolve(
        self,
        approval_id: UUID,
        decision: ApprovalDecisionValue,
        scope: str,
    ) -> None:
        self.resolutions.append((approval_id, decision, scope))


class _ContinuationScheduler:
    """Exercise the same orphan fencing used by the production service."""

    def __init__(self, service: TaskService) -> None:
        self.service = service
        self.resumes: list[tuple[UUID, str | None]] = []

    async def resume(
        self,
        task_id: UUID,
        *,
        idempotency_key: str | None = None,
    ) -> object:
        self.resumes.append((task_id, idempotency_key))
        return await self.service.resume_task(
            task_id,
            idempotency_key=idempotency_key,
        )


class _FailingContinuationScheduler:
    async def resume(
        self,
        task_id: UUID,
        *,
        idempotency_key: str | None = None,
    ) -> object:
        del task_id, idempotency_key
        raise RuntimeError("runner unavailable")


async def _service(tmp_path: Path) -> TaskService:
    store = SQLiteExecutionLedger(tmp_path / "ledger.db")
    await store.initialize()
    return TaskService(store=store, registry_generation=1)


async def _running_task(service: TaskService, **metadata):
    task = await service.create_task(
        objective="Review the release",
        agent_id="default",
        metadata=metadata,
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Review", objective=task.objective),),
    )
    await service.start_task(task.task_id, runner_id="runner.tests")
    return task


def _command(task_id: UUID, approval_id: UUID):
    return DecideTaskApprovalCommand(
        task_id=task_id,
        approval_id=approval_id,
        decision=ApprovalDecisionValue.APPROVED,
        actor=ActorRef(type=ActorType.USER, id="local-user"),
        reason="Approved",
    )


@pytest.mark.asyncio
async def test_approved_proposal_schedules_only_after_durable_decision(
    tmp_path: Path,
) -> None:
    service = await _service(tmp_path)
    proposal = Proposal(
        source="sensor.tests",
        objective="Review the release",
        rationale_summary="The release is ready",
        risk=RiskLevel.MEDIUM,
    )
    task = await _running_task(
        service,
        proposal=proposal.model_dump(mode="json"),
    )
    approval = await service.request_approval(
        task.task_id,
        action="proposal.execute",
        risk=RiskLevel.MEDIUM,
        requester=ActorRef(type=ActorType.SENSOR, id="sensor.tests"),
        redacted_arguments={
            "proposal_id": str(proposal.proposal_id),
        },
    )
    bridge = _RuntimeBridge(live=False)
    scheduled = []
    application = TaskApprovalApplicationService(
        service,
        bridge,
        _ContinuationScheduler(service),
        scheduled.append,
    )

    decision = await application.decide(
        _command(task.task_id, approval.approval_id),
    )

    record = await service.get_approval(approval.approval_id)
    assert record is not None
    assert record[1] == decision
    assert scheduled[0].approval_ids == (approval.approval_id,)
    assert bridge.resolutions == [
        (
            approval.approval_id,
            ApprovalDecisionValue.APPROVED,
            "exact",
        ),
    ]


@pytest.mark.asyncio
async def test_missing_runtime_resumes_from_approval_checkpoint(
    tmp_path: Path,
) -> None:
    service = await _service(tmp_path)
    task = await _running_task(service)
    approval = await service.request_approval(
        task.task_id,
        action="tool.execute",
        risk=RiskLevel.HIGH,
        requester=ActorRef(type=ActorType.AGENT, id="agent.tests"),
    )
    continuation = _ContinuationScheduler(service)
    application = TaskApprovalApplicationService(
        service,
        _RuntimeBridge(live=False),
        continuation,
        lambda _order: None,
    )

    decision = await application.decide(
        _command(task.task_id, approval.approval_id),
    )

    restored = await service.get_task(task.task_id)
    record = await service.get_approval(approval.approval_id)
    runs = await service.list_runs(task.task_id)
    assert restored is not None
    assert restored.status is TaskStatus.RUNNING
    assert record is not None
    assert record[1] == decision
    assert [run.attempt for run in runs] == [1, 2]
    assert runs[1].checkpoint_id == approval.checkpoint_id
    assert continuation.resumes == [
        (
            task.task_id,
            f"approval-continuation:{approval.approval_id}",
        ),
    ]


@pytest.mark.asyncio
async def test_detached_parallel_approvals_resume_after_last_decision(
    tmp_path: Path,
) -> None:
    service = await _service(tmp_path)
    task = await _running_task(service)
    first = await service.request_approval(
        task.task_id,
        action="tool.first",
        risk=RiskLevel.HIGH,
        requester=ActorRef(type=ActorType.AGENT, id="agent.tests"),
    )
    second = await service.request_approval(
        task.task_id,
        action="tool.second",
        risk=RiskLevel.HIGH,
        requester=ActorRef(type=ActorType.AGENT, id="agent.tests"),
    )
    continuation = _ContinuationScheduler(service)
    application = TaskApprovalApplicationService(
        service,
        _RuntimeBridge(live=False),
        continuation,
        lambda _order: None,
    )

    await application.decide(_command(task.task_id, first.approval_id))
    waiting = await service.get_task(task.task_id)
    assert waiting is not None
    assert waiting.status is TaskStatus.WAITING_APPROVAL
    assert not continuation.resumes

    await application.decide(_command(task.task_id, second.approval_id))
    resumed = await service.get_task(task.task_id)
    assert resumed is not None
    assert resumed.status is TaskStatus.RUNNING
    assert len(await service.list_runs(task.task_id)) == 2
    assert continuation.resumes == [
        (
            task.task_id,
            f"approval-continuation:{second.approval_id}",
        ),
    ]


@pytest.mark.asyncio
async def test_detached_continuation_replay_does_not_fence_new_run(
    tmp_path: Path,
) -> None:
    service = await _service(tmp_path)
    task = await _running_task(service)
    approval = await service.request_approval(
        task.task_id,
        action="tool.execute",
        risk=RiskLevel.HIGH,
        requester=ActorRef(type=ActorType.AGENT, id="agent.tests"),
    )
    continuation = _ContinuationScheduler(service)
    bridge = _RuntimeBridge(live=False)
    application = TaskApprovalApplicationService(
        service,
        bridge,
        continuation,
        lambda _order: None,
    )
    command = replace(
        _command(task.task_id, approval.approval_id),
        idempotency_key="same-decision",
    )

    first = await application.decide(command)
    replay = await application.reconcile(
        replace(command, idempotency_key="interaction-replay"),
    )

    current = await service.get_task(task.task_id)
    assert replay == first
    assert current is not None
    assert current.status is TaskStatus.RUNNING
    assert len(await service.list_runs(task.task_id)) == 2
    assert len(continuation.resumes) == 1
    assert not bridge.resolutions


@pytest.mark.asyncio
async def test_detached_continuation_failure_preserves_decision_and_checkpoint(
    tmp_path: Path,
) -> None:
    service = await _service(tmp_path)
    task = await _running_task(service)
    approval = await service.request_approval(
        task.task_id,
        action="tool.execute",
        risk=RiskLevel.HIGH,
        requester=ActorRef(type=ActorType.AGENT, id="agent.tests"),
    )
    application = TaskApprovalApplicationService(
        service,
        _RuntimeBridge(live=False),
        _FailingContinuationScheduler(),
        lambda _order: None,
    )

    with pytest.raises(TaskRuntimeLostError):
        await application.decide(
            _command(task.task_id, approval.approval_id),
        )

    current = await service.get_task(task.task_id)
    record = await service.get_approval(approval.approval_id)
    checkpoint = await service.projection_snapshot(task.task_id)
    assert current is not None
    assert current.status is TaskStatus.FAILED
    assert record is not None
    assert record[1] is not None
    assert checkpoint.checkpoint is not None
    assert checkpoint.checkpoint.checkpoint_id == approval.checkpoint_id


@pytest.mark.asyncio
async def test_terminal_decision_wakes_cancelled_parallel_approvals(
    tmp_path: Path,
) -> None:
    service = await _service(tmp_path)
    task = await _running_task(service)
    first = await service.request_approval(
        task.task_id,
        action="tool.first",
        risk=RiskLevel.HIGH,
        requester=ActorRef(type=ActorType.AGENT, id="agent.tests"),
    )
    second = await service.request_approval(
        task.task_id,
        action="tool.second",
        risk=RiskLevel.HIGH,
        requester=ActorRef(type=ActorType.AGENT, id="agent.tests"),
    )
    bridge = _RuntimeBridge()
    application = TaskApprovalApplicationService(
        service,
        bridge,
        _ContinuationScheduler(service),
        lambda _order: None,
    )
    command = replace(
        _command(task.task_id, first.approval_id),
        decision=ApprovalDecisionValue.DENIED,
    )

    await application.decide(command)

    assert bridge.resolutions == [
        (first.approval_id, ApprovalDecisionValue.DENIED, "exact"),
        (second.approval_id, ApprovalDecisionValue.CANCELLED, "exact"),
    ]


@pytest.mark.asyncio
async def test_interaction_managed_decision_does_not_redeliver_primary(
    tmp_path: Path,
) -> None:
    service = await _service(tmp_path)
    task = await _running_task(service)
    approval = await service.request_approval(
        task.task_id,
        action="tool.execute",
        risk=RiskLevel.HIGH,
        requester=ActorRef(type=ActorType.AGENT, id="agent.tests"),
    )
    bridge = _RuntimeBridge(live=True)
    application = TaskApprovalApplicationService(
        service,
        bridge,
        _ContinuationScheduler(service),
        lambda _order: None,
    )
    command = replace(
        _command(task.task_id, approval.approval_id),
        delivery_managed_by_interaction=True,
    )

    decision = await application.decide(command)

    record = await service.get_approval(approval.approval_id)
    assert record is not None
    assert record[1] == decision
    assert not bridge.resolutions


@pytest.mark.asyncio
async def test_list_rejects_unknown_task(tmp_path: Path) -> None:
    service = await _service(tmp_path)
    application = TaskApprovalApplicationService(
        service,
        _RuntimeBridge(),
        _ContinuationScheduler(service),
        lambda _order: None,
    )

    with pytest.raises(TaskNotFoundError):
        await application.list(uuid4())
