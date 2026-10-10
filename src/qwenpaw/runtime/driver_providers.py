# -*- coding: utf-8 -*-
"""Runtime adapters for provider-neutral Driver capability contracts."""

from __future__ import annotations

import inspect
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping

from ..kernel.driver import (
    DriverApprovalRejectedError,
    validate_driver_session,
)
from ..kernel.models import (
    DriverApprovalRequest,
    DriverToolDefinition,
    PromptFragment,
)
from ..kernel.ports import OutcomeHost
from .provider_credentials import (
    WorkspaceCredentialHandle,
    provider_credential_handle,
)


class ProviderDriverHost:
    """Expose only public Driver services to a plugin invocation."""

    __slots__ = (
        "__approval_context",
        "__credentials",
        "__provider_config",
        "__provider_id",
        "__outcomes",
    )

    def __init__(
        self,
        *,
        provider_id: str,
        provider_config: dict[str, Any] | None = None,
        credentials: Mapping[str, WorkspaceCredentialHandle] | None = None,
        approval_context: dict[str, Any] | None = None,
        outcomes: OutcomeHost | None = None,
    ) -> None:
        self.__provider_id = provider_id
        self.__provider_config = deepcopy(provider_config or {})
        self.__credentials = dict(credentials or {})
        self.__approval_context = dict(approval_context or {})
        self.__outcomes = outcomes

    def config_snapshot(self) -> dict[str, Any]:
        """Return a detached invocation configuration snapshot."""
        return deepcopy(self.__provider_config)

    def credential(
        self,
        alias: str,
    ) -> WorkspaceCredentialHandle | None:
        """Resolve only a credential alias bound before provider open."""
        return self.__credentials.get(alias)

    async def require_approval(
        self,
        request: DriverApprovalRequest,
    ) -> None:
        """Route approval through the unified application bridge."""
        await _require_driver_approval(
            self.__provider_id,
            self.__approval_context,
            request,
        )

    def outcome_host(self) -> OutcomeHost | None:
        """Return only a Host-admitted invocation outcome service."""
        return self.__outcomes


@dataclass(frozen=True)
class WorkspaceDriverHost:
    """Bind the existing DriverManager adapter to one invocation."""

    manager: Any
    request_context: dict[str, Any]
    provider_id: str
    provider_config: dict[str, Any] | None = None
    credential_refs: dict[str, str] | None = None
    outcomes: OutcomeHost | None = None

    def config_snapshot(self) -> dict[str, Any]:
        """Return a detached invocation configuration snapshot."""
        return deepcopy(self.provider_config or {})

    def credential(
        self,
        alias: str,
    ) -> WorkspaceCredentialHandle | None:
        """Resolve only an alias explicitly bound to this provider."""
        return provider_credential_handle(
            self.manager,
            self.credential_refs or {},
            self.provider_id,
            alias,
        )

    def outcome_host(self) -> OutcomeHost | None:
        """Return the same admitted service exposed to plugin providers."""
        return self.outcomes

    async def load(
        self,
    ) -> tuple[list[DriverToolDefinition], list[PromptFragment]]:
        """Resolve public tool definitions from active Drivers."""
        from ..drivers.adapters.agentscope_tool import (
            build_driver_definitions,
        )

        return await build_driver_definitions(
            self.manager,
            self.request_context,
        )

    async def require_approval(
        self,
        request: DriverApprovalRequest,
    ) -> None:
        """Route compatibility approval through the unified bridge."""
        await _require_driver_approval(
            self.provider_id,
            self.request_context,
            request,
        )


async def _require_driver_approval(
    provider_id: str,
    request_context: dict[str, Any],
    request: DriverApprovalRequest,
) -> None:
    """Validate ownership and invoke the one application approval path."""
    if request.provider_id != provider_id:
        raise DriverApprovalRejectedError(
            request.capability_id,
            "approval request has foreign provider ownership",
        )
    from ..app.approvals.driver_gate import QwenPawDriverApprovalGate
    from ..drivers.errors import (
        ApprovalRequiredError,
        DriverPermissionDeniedError,
    )
    from ..drivers.policy import DriverInvocationContext
    from ..drivers.policy_types import PolicyTarget

    approval_context = dict(request_context)
    try:
        from ..tool_calls._ctxvars import get_call_context

        active_call = get_call_context()
    except Exception:  # noqa: BLE001
        active_call = None
    if active_call is not None:
        approval_context["tool_call_id"] = active_call.tool_call_id
        action_id = active_call.governance_metadata.get("action_id")
        if action_id:
            approval_context["os_action_id"] = str(action_id)
    subject = str(
        approval_context.get("user_id")
        or approval_context.get("agent_id")
        or "unknown",
    )
    context = DriverInvocationContext(
        subject=subject,
        driver_name=provider_id,
        protocol="plugin",
        operation=request.operation,
        target=PolicyTarget(kind="tool", name=request.tool_name),
        request_context=approval_context,
        extras={
            "capability_id": request.capability_id,
            **(
                {"action_id": approval_context["os_action_id"]}
                if approval_context.get("os_action_id")
                else {}
            ),
            **dict(request.redacted_arguments),
        },
    )
    from ..kernel import ActionAdmissionEvidence
    from .actions import (
        ActionAdmissionPersistenceError,
        ActionConflictError,
        record_active_action_admission,
    )

    try:
        await record_active_action_admission(
            approval_context,
            authority=provider_id,
            decision="ask",
            evidence=ActionAdmissionEvidence.ACTION_INTENT,
        )
        await QwenPawDriverApprovalGate().request_approval(context)
    except (
        ActionAdmissionPersistenceError,
        ActionConflictError,
        ApprovalRequiredError,
        DriverPermissionDeniedError,
    ) as error:
        raise DriverApprovalRejectedError(
            request.capability_id,
            getattr(error, "reason", str(error)),
        ) from error


async def close_driver_session(session: object | None) -> None:
    """Close a Driver session supporting sync or async cleanup."""
    if session is None:
        return
    close = getattr(session, "close", None)
    if not callable(close):
        return
    result = close()
    if inspect.isawaitable(result):
        await result


__all__ = [
    "ProviderDriverHost",
    "WorkspaceDriverHost",
    "close_driver_session",
    "validate_driver_session",
]
