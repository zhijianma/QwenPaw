# -*- coding: utf-8 -*-
"""Shared Proposal ingress for proactive and contributed sensors."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

from ..kernel.models import (
    ActorRef,
    ActorType,
    ApprovalContinuation,
    ApprovalDisplay,
    ApprovalSource,
    PlanStep,
    Proposal,
    Task,
    TaskSource,
    TaskStatus,
)
from .approval_broker import TaskApprovalBroker
from .service import TaskService


async def persist_sensor_proposals(
    service: TaskService,
    *,
    agent_id: str,
    sensor_id: str,
    proposals: Sequence[Proposal],
    registry_generation: int | None = None,
) -> tuple[Task, ...]:
    """Persist proposals behind the common strict approval boundary."""
    tasks = []
    requester = ActorRef(type=ActorType.SENSOR, id=sensor_id)
    for proposal in proposals:
        task = await service.create_task(
            objective=proposal.objective,
            agent_id=agent_id,
            source=TaskSource.SENSOR,
            acceptance_criteria=(
                proposal.execution_contract.acceptance
                if proposal.execution_contract is not None
                else ()
            ),
            execution_contract=proposal.execution_contract,
            metadata={
                "proposal": proposal.model_dump(mode="json"),
                "sensor_id": sensor_id,
                **(
                    {"sensor_registry_generation": registry_generation}
                    if registry_generation is not None
                    else {}
                ),
            },
            idempotency_key=(f"sensor:{sensor_id}:{proposal.proposal_id}"),
        )
        current = await service.get_task(task.task_id)
        if current is not None:
            task = current
        if task.status is TaskStatus.CREATED:
            task, _ = await service.plan_task(
                task.task_id,
                steps=(
                    PlanStep(
                        title="Approved sensor action",
                        objective=proposal.objective,
                    ),
                ),
            )
            task, run = await service.start_task(
                task.task_id,
                runner_id="runner.local-agent",
            )
            await TaskApprovalBroker(
                service=service,
                task_id=task.task_id,
                run_id=run.run_id,
            ).request(
                approval_id=uuid4(),
                action="proposal.execute",
                source=ApprovalSource.PROPOSAL,
                risk=proposal.risk,
                requester=requester,
                policy="proposal_policy",
                continuation=ApprovalContinuation.RESUME_ON_DECISION,
                redacted_arguments={
                    "proposal_id": str(proposal.proposal_id),
                },
                display=ApprovalDisplay(
                    title="Approve proposed task",
                    summary=proposal.rationale_summary,
                    target=proposal.objective,
                    provider=sensor_id,
                ),
            )
            stored = await service.get_task(task.task_id)
            if stored is not None:
                task = stored
        tasks.append(task)
    return tuple(tasks)
