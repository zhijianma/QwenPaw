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
async def test_missing_live_runtime_recovers_before_rejecting_decision(
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
        lambda _order: None,
    )

    with pytest.raises(TaskRuntimeLostError):
        await application.decide(
            _command(task.task_id, approval.approval_id),
        )

    restored = await service.get_task(task.task_id)
    assert restored is not None
    assert restored.status is TaskStatus.FAILED


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
async def test_list_rejects_unknown_task(tmp_path: Path) -> None:
    service = await _service(tmp_path)
    application = TaskApprovalApplicationService(
        service,
        _RuntimeBridge(),
        lambda _order: None,
    )

    with pytest.raises(TaskNotFoundError):
        await application.list(uuid4())
