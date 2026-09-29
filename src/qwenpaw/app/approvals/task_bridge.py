# -*- coding: utf-8 -*-
"""Adapter between legacy runtime approvals and the durable Task ledger."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import AwareDatetime

from ...kernel.models import (
    ActorRef,
    ActorType,
    ApprovalBroker,
    ApprovalContinuation,
    ApprovalDecisionValue,
    ApprovalDisplay,
    ApprovalSource,
    RiskLevel,
)
from ...security.tool_guard.approval import (
    ApprovalDecision as RuntimeApprovalDecision,
)
from ...security.tool_guard.approval import ApprovalScope
from ...tasks.ledger import SQLiteExecutionLedger
from ...tasks.service import ApprovalAlreadyResolvedError, TaskService
from .service import ApprovalActor


def _risk_level(value: str) -> RiskLevel:
    normalized = value.strip().lower()
    if normalized in {"critical", "high", "medium"}:
        return RiskLevel(normalized)
    return RiskLevel.LOW


def _optional_uuid(value: Any) -> UUID | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _decision_value(
    decision: RuntimeApprovalDecision,
) -> ApprovalDecisionValue:
    if decision is RuntimeApprovalDecision.APPROVED:
        return ApprovalDecisionValue.APPROVED
    if decision is RuntimeApprovalDecision.TIMEOUT:
        return ApprovalDecisionValue.EXPIRED
    return ApprovalDecisionValue.DENIED


class LiveRuntimeApprovalBridge:
    """Wake legacy in-process approval waiters after durable commits."""

    async def is_live(self, approval_id: UUID) -> bool:
        """Return whether the current process still owns the waiter."""
        from . import get_approval_service

        pending = await get_approval_service().get_request(str(approval_id))
        return pending is not None and pending.status in {
            "pending",
            "resolving",
        }

    async def resolve(
        self,
        approval_id: UUID,
        decision: ApprovalDecisionValue,
        scope: str,
    ) -> None:
        """Translate and deliver one durable decision to the waiter."""
        from . import get_approval_service

        runtime_service = get_approval_service()
        pending = await runtime_service.get_request(str(approval_id))
        if pending is None:
            return
        if decision is ApprovalDecisionValue.APPROVED:
            runtime_decision = RuntimeApprovalDecision.APPROVED
        elif decision is ApprovalDecisionValue.EXPIRED:
            runtime_decision = RuntimeApprovalDecision.TIMEOUT
        else:
            runtime_decision = RuntimeApprovalDecision.DENIED
        await runtime_service.resolve_request(
            str(approval_id),
            runtime_decision,
            scope=ApprovalScope(scope),
        )


def _actor_ref(
    decision: RuntimeApprovalDecision,
    actor: ApprovalActor | None,
) -> ActorRef:
    if decision is RuntimeApprovalDecision.TIMEOUT:
        return ActorRef(type=ActorType.SYSTEM, id="approval-timeout")
    actor_id = actor.user_id if actor and actor.user_id else "local-user"
    return ActorRef(type=ActorType.USER, id=actor_id)


@dataclass(frozen=True)
class DurableTaskApprovalBridge:
    """Persist runtime approval lifecycle changes into one Task ledger."""

    broker: ApprovalBroker
    approval_id: UUID

    async def request(
        self,
        *,
        agent_id: str,
        tool_name: str,
        severity: str,
        input_data: dict[str, Any],
        source: ApprovalSource = ApprovalSource.TOOL,
        action: str = "tool.execute",
        policy: str = "tool_guard",
        display: ApprovalDisplay | None = None,
        expires_at: AwareDatetime | None = None,
        invocation_id: UUID | None = None,
        correlation_id: UUID | None = None,
    ) -> None:
        """Create the durable request before exposing the runtime prompt."""
        await self.broker.request(
            approval_id=self.approval_id,
            action=action,
            source=source,
            risk=_risk_level(severity),
            requester=ActorRef(
                type=ActorType.AGENT,
                id=agent_id or "unknown",
            ),
            policy=policy,
            continuation=ApprovalContinuation.RESUME_ON_DECISION,
            redacted_arguments={
                "tool_name": tool_name,
                "input": input_data,
            },
            display=display
            or ApprovalDisplay(
                title=f"Approve {tool_name}",
                summary=("The task is waiting to execute a protected tool."),
                target=tool_name,
                provider="tool_guard",
            ),
            expires_at=expires_at,
            invocation_id=invocation_id,
            correlation_id=correlation_id,
        )

    async def resolve(
        self,
        decision: RuntimeApprovalDecision,
        scope: ApprovalScope | None,
        actor: ApprovalActor | None,
    ) -> None:
        """Persist a runtime decision exactly once before waking the tool."""
        durable_decision = _decision_value(decision)
        existing = await self.broker.get(self.approval_id)
        if existing is None:
            raise KeyError(str(self.approval_id))
        if existing[1] is not None:
            if existing[1].decision is durable_decision:
                return
            raise ApprovalAlreadyResolvedError(str(self.approval_id))
        await self.broker.decide(
            self.approval_id,
            decision=durable_decision,
            actor=_actor_ref(decision, actor),
            reason=f"Tool Guard resolved the request as {decision.value}",
            scope=scope.value if scope else "exact",
            idempotency_key=(
                f"tool-guard:{self.approval_id}:{durable_decision.value}"
            ),
        )


async def _legacy_approval_broker(
    request_context: dict[str, Any],
) -> ApprovalBroker | None:
    """Rebuild a broker for request contexts created before typed services."""
    task_id = request_context.get("task_id")
    ledger_workspace = request_context.get("task_ledger_workspace_dir")
    if not isinstance(task_id, str) or not isinstance(ledger_workspace, str):
        return None
    try:
        parsed_task_id = UUID(task_id)
    except ValueError:
        return None
    ledger_root = Path(ledger_workspace)
    store = SQLiteExecutionLedger(
        ledger_root / ".qwenpaw" / "lite" / "tasks.db",
    )
    from ...tasks.approval_broker import TaskApprovalBroker

    service = TaskService(store=store, registry_generation=1)
    task = await service.get_task(parsed_task_id)
    if task is None or task.active_run_id is None:
        return None
    return TaskApprovalBroker(
        service=service,
        task_id=parsed_task_id,
        run_id=task.active_run_id,
    )


async def task_approval_bridge_from_context(
    request_context: dict[str, Any],
    request_id: str,
) -> DurableTaskApprovalBridge | None:
    """Build a bridge only for explicitly durable Task executions."""
    if request_context.get("durable_task") is not True:
        return None
    try:
        approval_id = UUID(request_id)
    except ValueError:
        return None
    broker = request_context.get("_task_approval_broker")
    if not isinstance(broker, ApprovalBroker):
        broker = await _legacy_approval_broker(request_context)
    if broker is None:
        return None
    return DurableTaskApprovalBridge(
        broker=broker,
        approval_id=approval_id,
    )


async def attach_pending_to_durable_task(
    request_context: dict[str, Any],
    pending: Any,
    approval_service: Any,
    *,
    agent_id: str,
    tool_name: str,
    severity: str,
    input_data: dict[str, Any],
    source: ApprovalSource,
    action: str,
    policy: str,
    display: ApprovalDisplay,
) -> bool:
    """Persist a legacy pending request before its runtime waits on it."""
    bridge = await task_approval_bridge_from_context(
        request_context,
        pending.request_id,
    )
    if bridge is None:
        return True
    invocation_id = _optional_uuid(request_context.get("os_invocation_id"))
    correlation_id = (
        _optional_uuid(
            request_context.get("os_correlation_id"),
        )
        or invocation_id
    )
    try:
        expires_at = datetime.fromtimestamp(
            pending.created_at + pending.timeout_seconds,
            tz=timezone.utc,
        )
        await bridge.request(
            agent_id=agent_id,
            tool_name=tool_name,
            severity=severity,
            input_data=input_data,
            source=source,
            action=action,
            policy=policy,
            display=display,
            expires_at=expires_at,
            invocation_id=invocation_id,
            correlation_id=correlation_id,
        )
        pending.resolution_hook = bridge.resolve
        return True
    except Exception:
        await approval_service.resolve_request(
            pending.request_id,
            RuntimeApprovalDecision.DENIED,
        )
        return False
