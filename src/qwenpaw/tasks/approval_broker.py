# -*- coding: utf-8 -*-
"""Run-scoped durable approval broker for system and plugin capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from pydantic import AwareDatetime

from ..kernel.models import (
    ActorRef,
    ApprovalContinuation,
    ApprovalDecision,
    ApprovalDecisionValue,
    ApprovalDisplay,
    ApprovalRequest,
    ApprovalSource,
    JsonObject,
    RiskLevel,
    RunStatus,
    TaskStatus,
)
from .service import RunNotActiveError, TaskService


@dataclass(frozen=True, slots=True)
class TaskApprovalBroker:
    """Bind approval mutations to one authoritative task run."""

    service: TaskService
    task_id: UUID
    run_id: UUID

    async def _assert_active_run(self) -> None:
        task = await self.service.get_task(self.task_id)
        runs = await self.service.list_runs(self.task_id)
        run = next(
            (item for item in runs if item.run_id == self.run_id),
            None,
        )
        if (
            task is None
            or task.active_run_id != self.run_id
            or task.status
            not in {TaskStatus.RUNNING, TaskStatus.WAITING_APPROVAL}
            or run is None
            or run.status
            not in {RunStatus.RUNNING, RunStatus.WAITING_APPROVAL}
        ):
            raise RunNotActiveError(
                f"approval broker run is no longer active: {self.run_id}",
            )

    async def request(
        self,
        *,
        approval_id: UUID,
        action: str,
        risk: RiskLevel,
        requester: ActorRef,
        source: ApprovalSource,
        policy: str,
        continuation: ApprovalContinuation,
        redacted_arguments: JsonObject,
        display: ApprovalDisplay | None = None,
        expires_at: AwareDatetime | None = None,
        invocation_id: UUID | None = None,
        correlation_id: UUID | None = None,
    ) -> ApprovalRequest:
        """Persist one request only while the bound run remains active."""
        await self._assert_active_run()
        return await self.service.request_approval(
            self.task_id,
            approval_id=approval_id,
            action=action,
            risk=risk,
            requester=requester,
            source=source,
            policy=policy,
            continuation=continuation,
            redacted_arguments=redacted_arguments,
            display=display,
            expires_at=expires_at,
            invocation_id=invocation_id,
            correlation_id=correlation_id,
        )

    async def get(
        self,
        approval_id: UUID,
    ) -> tuple[ApprovalRequest, ApprovalDecision | None] | None:
        """Return one approval only when it belongs to the bound run."""
        record = await self.service.get_approval(approval_id)
        if record is None or record[0].run_id != self.run_id:
            return None
        return record

    async def decide(
        self,
        approval_id: UUID,
        *,
        decision: ApprovalDecisionValue,
        actor: ActorRef,
        reason: str,
        scope: str = "exact",
        idempotency_key: str | None = None,
    ) -> ApprovalDecision:
        """Resolve an approval that belongs to the bound run."""
        record = await self.get(approval_id)
        if record is None:
            raise KeyError(str(approval_id))
        return await self.service.decide_approval(
            self.task_id,
            approval_id,
            decision=decision,
            actor=actor,
            reason=reason,
            scope=scope,
            idempotency_key=idempotency_key,
        )
