# -*- coding: utf-8 -*-
"""Compatibility bridge from legacy approvals to InteractionService."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from ...kernel import (
    ActorRef,
    ActorType,
    InteractionKind,
    InteractionMode,
    InteractionOption,
    InteractionRequest,
    InteractionResolution,
    InteractionResponse,
    InteractionStatus,
)
from ...interactions import InteractionConflictError
from ...security.tool_guard.approval import ApprovalDecision, ApprovalScope
from ...tasks.redaction import redact_payload
from .service import ApprovalActor, ApprovalService, PendingApproval

logger = logging.getLogger(__name__)


def _optional_uuid(value: Any) -> UUID | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _redacted_json(payload: dict[str, Any]) -> dict[str, Any]:
    """Return JSON-compatible, recursively redacted interaction metadata."""
    compatible = json.loads(json.dumps(payload, default=str))
    return redact_payload(compatible)


def _actor_ref(
    decision: ApprovalDecision,
    actor: ApprovalActor | None,
) -> ActorRef:
    if decision is ApprovalDecision.TIMEOUT:
        return ActorRef(type=ActorType.SYSTEM, id="approval-timeout")
    actor_id = actor.user_id if actor and actor.user_id else "local-user"
    return ActorRef(type=ActorType.USER, id=actor_id)


def _option_id(
    decision: ApprovalDecision,
    scope: ApprovalScope | None,
) -> str:
    if decision is ApprovalDecision.DENIED:
        return "deny"
    if scope is ApprovalScope.SIMILAR:
        return "approve_similar"
    return "approve_exact"


def _matches_terminal_resolution(
    resolution: InteractionResolution | None,
    decision: ApprovalDecision,
    scope: ApprovalScope | None,
) -> bool:
    """Return whether a concurrent terminal already records the decision."""
    if resolution is None:
        return False
    if decision is ApprovalDecision.TIMEOUT:
        return resolution.status is InteractionStatus.EXPIRED
    if resolution.status is InteractionStatus.CANCELLED:
        return decision is ApprovalDecision.DENIED
    if (
        resolution.status is not InteractionStatus.RESOLVED
        or resolution.response is None
    ):
        return False
    return _option_id(decision, scope) in set(
        resolution.response.selected_option_ids,
    )


@dataclass(slots=True)
class LegacyApprovalInteractionBridge:
    """Synchronize one legacy Future with one durable interaction."""

    interaction_service: Any
    approval_service: ApprovalService
    pending: PendingApproval
    request: InteractionRequest
    downstream_hook: Any = None

    async def resolve(
        self,
        decision: ApprovalDecision,
        scope: ApprovalScope | None,
        actor: ApprovalActor | None,
    ) -> None:
        """Commit policy-specific state before the delivery projection."""
        if self.downstream_hook is not None:
            await self.downstream_hook(decision, scope, actor)
        await self.resolve_interaction(decision, scope, actor)

    async def resolve_interaction(
        self,
        decision: ApprovalDecision,
        scope: ApprovalScope | None,
        actor: ApprovalActor | None,
    ) -> None:
        """Commit only the durable projection after legacy already closed."""
        if decision is ApprovalDecision.TIMEOUT:
            await self.interaction_service.expire(
                self.request.interaction_id,
                detail="legacy approval timed out",
            )
            return
        try:
            await self.interaction_service.resolve(
                InteractionResponse(
                    interaction_id=self.request.interaction_id,
                    idempotency_key=(
                        f"legacy:{decision.value}:"
                        f"{scope.value if scope else 'exact'}"
                    ),
                    expected_revision=self.request.revision,
                    actor=_actor_ref(decision, actor),
                    selected_option_ids=(_option_id(decision, scope),),
                ),
            )
        except InteractionConflictError:
            resolution = await self.interaction_service.get_resolution(
                self.request.interaction_id,
            )
            if _matches_terminal_resolution(resolution, decision, scope):
                return
            raise

    async def on_terminal(
        self,
        resolution: InteractionResolution,
    ) -> None:
        """Wake the legacy waiter when the unified plane resolves first."""
        if resolution.status is InteractionStatus.RESOLVED:
            selected = set(
                resolution.response.selected_option_ids
                if resolution.response is not None
                else (),
            )
            decision = (
                ApprovalDecision.APPROVED
                if selected & {"approve_exact", "approve_similar"}
                else ApprovalDecision.DENIED
            )
            scope = (
                ApprovalScope.SIMILAR
                if "approve_similar" in selected
                else ApprovalScope.EXACT
                if decision is ApprovalDecision.APPROVED
                else None
            )
            terminal_status = decision.value
        elif resolution.status is InteractionStatus.EXPIRED:
            decision = ApprovalDecision.TIMEOUT
            scope = None
            terminal_status = "timeout"
        else:
            decision = ApprovalDecision.DENIED
            scope = None
            terminal_status = "cancelled"
        await self.approval_service.resolve_from_interaction(
            self.pending.request_id,
            decision,
            terminal_status=terminal_status,
            scope=scope,
            downstream_hook=self.downstream_hook,
        )


async def attach_pending_to_interaction(
    request_context: dict[str, Any],
    pending: PendingApproval,
    approval_service: ApprovalService,
    *,
    source: str,
    input_data: dict[str, Any],
) -> bool:
    """Attach when OS identities exist; preserve legacy-only callers."""
    service = request_context.get("_interaction_service")
    conversation_id = request_context.get("os_conversation_id")
    invocation_id = _optional_uuid(
        request_context.get("os_invocation_id"),
    )
    interaction_id = _optional_uuid(pending.request_id)
    if service is None or not conversation_id or invocation_id is None:
        return True
    if interaction_id is None:
        return False
    request = InteractionRequest(
        interaction_id=interaction_id,
        kind=InteractionKind.APPROVAL,
        mode=InteractionMode.BLOCKING,
        agent_id=pending.agent_id,
        conversation_id=str(conversation_id),
        invocation_id=invocation_id,
        source_id=interaction_id,
        title=f"Approve {pending.tool_name}",
        prompt=pending.result_summary or "Approve protected execution?",
        options=(
            InteractionOption(
                option_id="approve_exact",
                label="Approve once",
            ),
            InteractionOption(
                option_id="approve_similar",
                label="Approve similar",
            ),
            InteractionOption(option_id="deny", label="Deny"),
        ),
        metadata={
            "source": source,
            "severity": pending.severity,
            "findings_count": pending.findings_count,
            "arguments": _redacted_json(input_data),
        },
    )
    try:
        await service.open(request)
    except Exception:  # pylint: disable=broad-except
        logger.warning(
            "Approval interaction persistence failed: request_id=%s",
            pending.request_id[:8],
            exc_info=True,
        )
        return False
    bridge = LegacyApprovalInteractionBridge(
        interaction_service=service,
        approval_service=approval_service,
        pending=pending,
        request=request,
        downstream_hook=pending.resolution_hook,
    )
    pending.resolution_hook = bridge.resolve
    pending.interaction_id = request.interaction_id
    await service.add_terminal_hook(
        request.interaction_id,
        bridge.on_terminal,
    )
    if pending.future.done():
        await bridge.resolve_interaction(
            pending.future.result(),
            pending.scope,
            None,
        )
    return True


__all__ = ["attach_pending_to_interaction"]
