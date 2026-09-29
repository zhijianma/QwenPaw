# -*- coding: utf-8 -*-
"""Transport-neutral approval commands for durable Tasks."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from pydantic import ValidationError

from ..kernel.models import (
    ActorRef,
    ApprovalDecision,
    ApprovalDecisionValue,
    ApprovalRequest,
    ApprovalStatus,
    Proposal,
    TaskOrder,
)
from ..kernel.proposals import approved_task_order
from .service import TaskNotFoundError, TaskService


class RuntimeApprovalBridge(Protocol):
    """Live-process side of a durable approval decision."""

    async def is_live(self, approval_id: UUID) -> bool:
        """Return whether this process still owns the waiting runtime."""

    async def resolve(
        self,
        approval_id: UUID,
        decision: ApprovalDecisionValue,
        scope: str,
    ) -> None:
        """Wake the live runtime after the durable commit succeeds."""


class TaskRuntimeLostError(RuntimeError):
    """Raised when a durable approval outlives its runtime waiter."""


class ProposalNotAvailableError(RuntimeError):
    """Raised when proposal execution lacks valid proposal metadata."""


@dataclass(frozen=True, slots=True)
class DecideTaskApprovalCommand:
    """Transport-neutral input for one approval decision."""

    task_id: UUID
    approval_id: UUID
    decision: ApprovalDecisionValue
    actor: ActorRef
    reason: str
    scope: str = "exact"
    idempotency_key: str | None = None


ApprovalRecord = tuple[ApprovalRequest, ApprovalDecision | None]
ProposalScheduler = Callable[[TaskOrder], None]


class TaskApprovalApplicationService:
    """Coordinate durable decisions with live runtime continuation."""

    def __init__(
        self,
        service: TaskService,
        runtime_bridge: RuntimeApprovalBridge,
        proposal_scheduler: ProposalScheduler,
    ) -> None:
        self._service = service
        self._runtime_bridge = runtime_bridge
        self._proposal_scheduler = proposal_scheduler

    async def list(
        self,
        task_id: UUID,
        *,
        run_id: UUID | None = None,
        status: ApprovalStatus | None = None,
    ) -> tuple[ApprovalRecord, ...]:
        """Return authoritative approval records for one Task."""
        task = await self._service.get_task(task_id)
        if task is None:
            raise TaskNotFoundError(str(task_id))
        return tuple(
            await self._service.list_approvals(
                task_id,
                run_id=run_id,
                status=status,
            ),
        )

    async def decide(
        self,
        command: DecideTaskApprovalCommand,
    ) -> ApprovalDecision:
        """Commit one decision, wake runtimes, and dispatch proposals."""
        pending_before = await self._service.list_approvals(
            command.task_id,
            status=ApprovalStatus.PENDING,
        )
        approval_record = await self._service.get_approval(
            command.approval_id,
        )
        await self._assert_runtime_available(command, approval_record)
        proposal = await self._approved_proposal(command, approval_record)
        decision = await self._service.decide_approval(
            command.task_id,
            command.approval_id,
            decision=command.decision,
            actor=command.actor,
            reason=command.reason,
            scope=command.scope,
            idempotency_key=command.idempotency_key,
        )
        await self._runtime_bridge.resolve(
            command.approval_id,
            command.decision,
            command.scope,
        )
        await self._resolve_cancelled_siblings(
            command.approval_id,
            pending_before,
        )
        if proposal is not None and approval_record is not None:
            self._proposal_scheduler(
                approved_task_order(
                    proposal,
                    approval_record[0],
                    decision,
                    task_id=command.task_id,
                ),
            )
        return decision

    async def _assert_runtime_available(
        self,
        command: DecideTaskApprovalCommand,
        approval_record: ApprovalRecord | None,
    ) -> None:
        if approval_record is None or approval_record[1] is not None:
            return
        if approval_record[0].action == "proposal.execute":
            return
        if await self._runtime_bridge.is_live(command.approval_id):
            return
        await self._service.recover_orphaned_task(command.task_id)
        raise TaskRuntimeLostError(str(command.task_id))

    async def _approved_proposal(
        self,
        command: DecideTaskApprovalCommand,
        approval_record: ApprovalRecord | None,
    ) -> Proposal | None:
        if command.decision is not ApprovalDecisionValue.APPROVED:
            return None
        if approval_record is None or approval_record[1] is not None:
            return None
        if approval_record[0].action != "proposal.execute":
            return None
        task = await self._service.get_task(command.task_id)
        if task is None:
            raise TaskNotFoundError(str(command.task_id))
        proposal_data = task.metadata.get("proposal")
        if not isinstance(proposal_data, dict):
            raise ProposalNotAvailableError(str(command.task_id))
        try:
            return Proposal.model_validate(proposal_data)
        except ValidationError as exc:
            raise ProposalNotAvailableError(str(command.task_id)) from exc

    async def _resolve_cancelled_siblings(
        self,
        approval_id: UUID,
        pending_before: Sequence[ApprovalRecord],
    ) -> None:
        for pending_request, _ in pending_before:
            if pending_request.approval_id == approval_id:
                continue
            resolved = await self._service.get_approval(
                pending_request.approval_id,
            )
            if (
                resolved is not None
                and resolved[1] is not None
                and resolved[1].decision is ApprovalDecisionValue.CANCELLED
            ):
                await self._runtime_bridge.resolve(
                    pending_request.approval_id,
                    ApprovalDecisionValue.CANCELLED,
                    "exact",
                )


__all__ = [
    "DecideTaskApprovalCommand",
    "ProposalNotAvailableError",
    "RuntimeApprovalBridge",
    "TaskApprovalApplicationService",
    "TaskRuntimeLostError",
]
