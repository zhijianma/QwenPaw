# -*- coding: utf-8 -*-
"""Tests for the run-scoped durable approval broker."""

from pathlib import Path
from uuid import uuid4

import pytest

from qwenpaw.kernel.models import (
    ActorRef,
    ActorType,
    ApprovalContinuation,
    ApprovalDecisionValue,
    ApprovalSource,
    PlanStep,
    RiskLevel,
)
from qwenpaw.tasks.approval_broker import TaskApprovalBroker
from qwenpaw.tasks.ledger import SQLiteExecutionLedger
from qwenpaw.tasks.service import RunNotActiveError, TaskService


async def _running_task(tmp_path: Path):
    service = TaskService(
        store=SQLiteExecutionLedger(tmp_path / "tasks.db"),
        registry_generation=2,
    )
    task = await service.create_task(
        objective="Exercise the approval broker",
        agent_id="default",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Run", objective=task.objective),),
    )
    _, run = await service.start_task(
        task.task_id,
        runner_id="runner.example",
    )
    return service, task, run


@pytest.mark.asyncio
async def test_broker_requests_and_decides_for_its_bound_run(
    tmp_path: Path,
) -> None:
    service, task, run = await _running_task(tmp_path)
    broker = TaskApprovalBroker(service, task.task_id, run.run_id)
    approval_id = uuid4()

    request = await broker.request(
        approval_id=approval_id,
        action="tool.execute",
        risk=RiskLevel.HIGH,
        requester=ActorRef(type=ActorType.AGENT, id="default"),
        source=ApprovalSource.TOOL,
        policy="strict",
        continuation=ApprovalContinuation.RESUME_ON_DECISION,
        redacted_arguments={"tool_name": "Read"},
    )
    decision = await broker.decide(
        approval_id,
        decision=ApprovalDecisionValue.APPROVED,
        actor=ActorRef(type=ActorType.USER, id="local-user"),
        reason="Approved in the Workbench",
    )

    assert request.run_id == run.run_id
    assert decision.decision is ApprovalDecisionValue.APPROVED
    assert await broker.get(approval_id) == (request, decision)


@pytest.mark.asyncio
async def test_broker_rejects_requests_after_its_run_is_replaced(
    tmp_path: Path,
) -> None:
    service, task, run = await _running_task(tmp_path)
    broker = TaskApprovalBroker(service, task.task_id, run.run_id)
    await service.cancel_task(task.task_id)

    with pytest.raises(RunNotActiveError, match="no longer active"):
        await broker.request(
            approval_id=uuid4(),
            action="tool.execute",
            risk=RiskLevel.MEDIUM,
            requester=ActorRef(type=ActorType.AGENT, id="default"),
            source=ApprovalSource.TOOL,
            policy="strict",
            continuation=ApprovalContinuation.RESUME_ON_DECISION,
            redacted_arguments={},
        )
