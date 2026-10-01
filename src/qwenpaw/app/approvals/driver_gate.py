# -*- coding: utf-8 -*-
"""QwenPaw application approval gate for Driver policy."""

from __future__ import annotations

from uuid import UUID

from ...constant import TOOL_GUARD_APPROVAL_TIMEOUT_SECONDS
from ...drivers.errors import (
    ApprovalRequiredError,
    DriverPermissionDeniedError,
)
from ...drivers.policy import DriverInvocationContext
from ...kernel.models import ApprovalDisplay, ApprovalSource
from ...security.tool_guard.approval import ApprovalDecision

from .models import ApprovalRequestSummary
from .interaction_bridge import attach_pending_to_interaction
from .task_bridge import attach_pending_to_durable_task
from .timeouts import approval_timeout_seconds


class QwenPawDriverApprovalGate:
    """Bridge Driver policy approval requests into QwenPaw approval service."""

    async def request_approval(
        self,
        context: DriverInvocationContext,
    ) -> None:
        # Reuse QwenPaw's approval Future flow: this coroutine pauses until
        # the console or command approval endpoint resolves the pending
        # request.
        ctx = context.request_context
        timeout_seconds = approval_timeout_seconds(
            ctx,
            default=TOOL_GUARD_APPROVAL_TIMEOUT_SECONDS,
        )
        session_id = str(ctx.get("session_id") or "")
        driver_label = f"driver:{context.protocol}:{context.driver_name}"
        driver_ref = f"{context.protocol}:{context.driver_name}"
        target_name = str(context.target.name or "")
        has_tool_target = context.target.kind == "tool" and bool(
            target_name,
        )
        display_tool_name = target_name if has_tool_target else driver_label
        display_tool_source = driver_ref
        if has_tool_target:
            result_summary = (
                f"Tool '{display_tool_name}' from '{display_tool_source}' "
                f"requires approval for {context.operation}."
            )
        else:
            result_summary = (
                f"Driver '{driver_ref}' requires approval for "
                f"{context.operation}."
            )
        if not session_id:
            raise ApprovalRequiredError(
                "Driver approval required but request_context.session_id "
                f"is missing: {context.subject} -> {context.driver_name}",
            )

        from . import get_approval_service
        from ...config.context import get_f1_reasoning

        svc = get_approval_service()
        tool_call_id = str(ctx.get("tool_call_id") or "")
        if tool_call_id:
            await svc.cancel_stale_pending_for_tool_call(
                session_id,
                tool_call_id,
            )

        pending = await svc.create_pending_summary(
            session_id=session_id,
            root_session_id=str(ctx.get("root_session_id") or session_id),
            owner_agent_id=str(
                ctx.get("root_agent_id") or ctx.get("agent_id") or "",
            ),
            user_id=str(ctx.get("user_id") or ""),
            channel=str(ctx.get("channel") or ""),
            agent_id=str(ctx.get("agent_id") or "unknown"),
            summary=ApprovalRequestSummary(
                source_type="driver_policy",
                name=driver_label,
                severity="medium",
                findings_count=1,
                result_summary=result_summary,
            ),
            timeout_seconds=timeout_seconds,
            extra={
                "display": {
                    "tool_name": display_tool_name,
                    "tool_source": display_tool_source,
                },
                "driver": {
                    "name": context.driver_name,
                    "protocol": context.protocol,
                    "operation": context.operation,
                    "subject": context.subject,
                    "extras": context.extras,
                },
                "tool_call": {
                    "id": tool_call_id,
                    "name": driver_label,
                    "input": context.extras,
                },
                "channel_meta": ctx.get("channel_meta"),
                "_channel_instance": ctx.get("_channel_instance"),
                "reasoning": get_f1_reasoning(session_id),
                **(
                    {
                        "_spawn_subagent": True,
                    }
                    if ctx.get("_spawn_subagent")
                    else {}
                ),
            },
        )
        try:
            from ...runtime.actions import link_active_action_approval

            await link_active_action_approval(
                ctx,
                UUID(pending.request_id),
                ApprovalSource.DRIVER,
            )
        except Exception as exc:
            await svc.resolve_request(
                pending.request_id,
                ApprovalDecision.DENIED,
            )
            raise DriverPermissionDeniedError(
                context.driver_name,
                context.subject,
                context.operation,
                reason="Driver approval could not be linked to its action.",
            ) from exc
        bridge_ready = await attach_pending_to_durable_task(
            ctx,
            pending,
            svc,
            agent_id=str(ctx.get("agent_id") or "unknown"),
            tool_name=display_tool_name,
            severity="medium",
            input_data=dict(context.extras),
            source=ApprovalSource.DRIVER,
            action=f"driver.{context.operation}",
            policy="driver_policy",
            display=ApprovalDisplay(
                title=f"Approve {display_tool_name}",
                summary=result_summary,
                target=display_tool_name,
                provider=display_tool_source,
            ),
        )
        if not bridge_ready:
            raise DriverPermissionDeniedError(
                context.driver_name,
                context.subject,
                context.operation,
                reason="Driver approval could not be persisted.",
            )
        interaction_ready = await attach_pending_to_interaction(
            ctx,
            pending,
            svc,
            source="driver_policy",
            input_data=dict(context.extras),
        )
        if not interaction_ready:
            await svc.resolve_request(
                pending.request_id,
                ApprovalDecision.DENIED,
            )
            raise DriverPermissionDeniedError(
                context.driver_name,
                context.subject,
                context.operation,
                reason="Driver approval could not be persisted.",
            )
        decision = await svc.wait_for_approval(
            pending,
            timeout_seconds,
        )
        if decision == ApprovalDecision.APPROVED:
            return
        raise DriverPermissionDeniedError(
            context.driver_name,
            context.subject,
            context.operation,
            reason=f"User approval decision was {decision.value}.",
        )
