# -*- coding: utf-8 -*-
"""Provider-neutral Action Plane bridge for third-party Harnesses."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import UUID

from ..kernel import (
    ActionStatus,
    ApprovalSource,
    EnvironmentRef,
    EnvironmentResolution,
    InvocationScope,
    RiskLevel,
    ToolEffect,
)
from ..runtime.actions import RuntimeActionRecorder
from ..tool_calls._context import ToolCallContext
from .events import HarnessEvent

HARNESS_ACTION_TRACKER_KEY = "_harness_action_tracker"


def _effect(
    provider_type: str,
    tool_name: str,
) -> tuple[ToolEffect, RiskLevel, bool]:
    normalized = f"{provider_type} {tool_name}".lower()
    if any(value in normalized for value in ("command", "shell", "bash")):
        return ToolEffect.PROCESS, RiskLevel.HIGH, False
    if any(value in normalized for value in ("filechange", "write", "edit")):
        return ToolEffect.LOCAL_WRITE, RiskLevel.MEDIUM, True
    if any(
        value in normalized
        for value in ("websearch", "imageview", "read", "search")
    ):
        return ToolEffect.NONE, RiskLevel.LOW, True
    return ToolEffect.EXTERNAL_WRITE, RiskLevel.HIGH, False


def _terminal_status(event: HarnessEvent) -> tuple[ActionStatus, str]:
    data = event.data
    status = str(data.get("status") or "").lower()
    if data.get("permission_denied") is True or status == "denied":
        return ActionStatus.DENIED, "permission_denied"
    if status in {"cancelled", "canceled", "interrupted"}:
        return ActionStatus.CANCELLED, status
    exit_code = data.get("exit_code")
    if (
        data.get("is_error") is True
        or status in {"error", "failed", "failure"}
        or (
            isinstance(exit_code, int)
            and not isinstance(exit_code, bool)
            and exit_code != 0
        )
    ):
        return ActionStatus.FAILED, status or "provider_error"
    return ActionStatus.SUCCEEDED, ""


class HarnessActionTracker:
    """Track remote tool events under one immutable invocation scope."""

    def __init__(
        self,
        *,
        backend: str,
        scope: InvocationScope,
        recorder: RuntimeActionRecorder,
    ) -> None:
        self._backend = backend
        self._scope = scope
        self._recorder = recorder
        self._environment_ref: EnvironmentRef | None = None
        self._contexts: dict[str, ToolCallContext] = {}

    def bind_environment(self, resolution: EnvironmentResolution) -> None:
        """Bind the environment evidence persisted before provider dispatch."""
        self._environment_ref = EnvironmentRef(
            resolution_id=resolution.resolution_id,
            contract_id=resolution.contract_id,
            contract_version=resolution.contract_version,
            resolver_id=resolution.resolver_id,
        )

    @property
    def backend(self) -> str:
        """Return the provider identity bound to this tracker."""
        return self._backend

    def _context(
        self,
        *,
        item_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolCallContext:
        context = self._contexts.get(item_id)
        if context is not None:
            return context
        context = ToolCallContext(
            tool_call_id=item_id,
            tool_name=tool_name,
            session_id=self._scope.session_id,
            agent_id=self._scope.agent_id,
            root_session_id=self._scope.root_session_id,
            root_agent_id=self._scope.root_agent_id,
            started_at=time.monotonic(),
            offload_deadline=None,
            cancel_event=asyncio.Event(),
            extra={"tool_input": arguments},
        )
        self._contexts[item_id] = context
        return context

    async def begin(
        self,
        *,
        item_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None,
        provider_type: str = "",
        policy_decision: str = "provider_reported",
        approval_id: UUID | None = None,
    ) -> ToolCallContext | None:
        """Record an approval-time intent or first provider start event."""
        if not item_id or self._environment_ref is None:
            return None
        resolved_name = tool_name or provider_type or "remote_tool"
        context = self._context(
            item_id=item_id,
            tool_name=resolved_name,
            arguments=arguments or {},
        )
        effect, risk, reversible = _effect(provider_type, resolved_name)
        await self._recorder.begin_harness_remote(
            context,
            capability_id=f"qwenpaw.system.harness.{self._backend}",
            effect=effect,
            risk=risk,
            reversible=reversible,
            policy_decision=policy_decision,
            environment_ref=self._environment_ref,
        )
        if approval_id is not None:
            await self._recorder.link_approval(
                context,
                approval_id,
                ApprovalSource.HARNESS,
            )
        return context

    async def begin_event(self, event: HarnessEvent) -> None:
        """Record one normalized TOOL_STARTED event."""
        await self.begin(
            item_id=event.item_id,
            tool_name=event.tool_name,
            arguments=(
                dict(event.data.get("arguments") or {})
                if isinstance(event.data.get("arguments"), dict)
                else {}
            ),
            provider_type=str(event.data.get("provider_type") or ""),
        )

    async def complete_event(self, event: HarnessEvent) -> None:
        """Complete one normalized TOOL_COMPLETED event."""
        context = self._contexts.get(event.item_id)
        if context is None:
            context = await self.begin(
                item_id=event.item_id,
                tool_name=event.tool_name,
                arguments=(
                    dict(event.data.get("arguments") or {})
                    if isinstance(event.data.get("arguments"), dict)
                    else {}
                ),
                provider_type=str(event.data.get("provider_type") or ""),
            )
        if context is None:
            return
        status, error_code = _terminal_status(event)
        await self._recorder.complete_harness_remote(
            context,
            status=status,
            error_code=error_code,
        )
        self._contexts.pop(event.item_id, None)

    async def finalize_pending(
        self,
        status: ActionStatus,
        error_code: str,
    ) -> None:
        """Terminalize actions whose provider never emitted completion."""
        pending = tuple(self._contexts.items())
        self._contexts.clear()
        for _item_id, context in pending:
            await self._recorder.complete_harness_remote(
                context,
                status=status,
                error_code=error_code,
            )


async def begin_harness_approval_action(
    request_context: dict[str, Any],
    *,
    backend: str,
    item_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    provider_type: str,
    approval_id: UUID | str,
) -> None:
    """Record and link a Harness action before an approval wait resumes it."""
    tracker = request_context.get(HARNESS_ACTION_TRACKER_KEY)
    if not isinstance(tracker, HarnessActionTracker):
        return
    if tracker.backend != backend:
        return
    await tracker.begin(
        item_id=item_id,
        tool_name=tool_name,
        arguments=arguments,
        provider_type=provider_type,
        policy_decision="approval_required",
        approval_id=(
            approval_id
            if isinstance(approval_id, UUID)
            else UUID(str(approval_id))
        ),
    )


__all__ = [
    "HARNESS_ACTION_TRACKER_KEY",
    "HarnessActionTracker",
    "begin_harness_approval_action",
]
