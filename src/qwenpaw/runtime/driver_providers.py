# -*- coding: utf-8 -*-
"""Runtime adapters for provider-neutral Driver capability contracts."""

from __future__ import annotations

import inspect
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping

from ..kernel.driver import (
    DriverApprovalRejectedError,
)
from ..kernel.models import (
    DriverApprovalRequest,
    DriverToolDefinition,
    PromptFragment,
)
from .provider_credentials import (
    WorkspaceCredentialHandle,
    provider_credential_handle,
)

_MAX_DRIVER_TOOLS = 128
_MAX_DRIVER_PROMPT_FRAGMENTS = 16
_MAX_DRIVER_PROMPT_BYTES = 32 * 1024


class ProviderDriverHost:
    """Expose only public Driver services to a plugin invocation."""

    __slots__ = (
        "__approval_context",
        "__credentials",
        "__provider_config",
        "__provider_id",
    )

    def __init__(
        self,
        *,
        provider_id: str,
        provider_config: dict[str, Any] | None = None,
        credentials: Mapping[str, WorkspaceCredentialHandle] | None = None,
        approval_context: dict[str, Any] | None = None,
    ) -> None:
        self.__provider_id = provider_id
        self.__provider_config = deepcopy(provider_config or {})
        self.__credentials = dict(credentials or {})
        self.__approval_context = dict(approval_context or {})

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


@dataclass(frozen=True)
class WorkspaceDriverHost:
    """Bind the existing DriverManager adapter to one invocation."""

    manager: Any
    request_context: dict[str, Any]
    provider_id: str
    provider_config: dict[str, Any] | None = None
    credential_refs: dict[str, str] | None = None

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

    subject = str(
        request_context.get("user_id")
        or request_context.get("agent_id")
        or "unknown",
    )
    context = DriverInvocationContext(
        subject=subject,
        driver_name=provider_id,
        protocol="plugin",
        operation=request.operation,
        target=PolicyTarget(kind="tool", name=request.tool_name),
        request_context=dict(request_context),
        extras={
            "capability_id": request.capability_id,
            **dict(request.redacted_arguments),
        },
    )
    try:
        await QwenPawDriverApprovalGate().request_approval(context)
    except (ApprovalRequiredError, DriverPermissionDeniedError) as error:
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


def _validate_driver_definitions(
    definitions: tuple[DriverToolDefinition, ...],
    provider_id: str,
) -> None:
    """Validate typed tools and their provider ownership."""
    if len(definitions) > _MAX_DRIVER_TOOLS:
        raise ValueError(
            f"driver provider '{provider_id}' returned too many tools",
        )
    capability_ids: set[str] = set()
    tool_names: set[str] = set()
    for definition in definitions:
        if not isinstance(definition, DriverToolDefinition):
            raise TypeError(
                f"driver provider '{provider_id}' returned an untyped tool",
            )
        if definition.provider_id != provider_id:
            raise ValueError(
                f"driver tool '{definition.name}' has foreign ownership",
            )
        if definition.capability_id in capability_ids:
            raise ValueError("driver capability IDs must be unique")
        if definition.name in tool_names:
            raise ValueError("driver tool names must be unique")
        capability_ids.add(definition.capability_id)
        tool_names.add(definition.name)


def _validate_driver_fragments(
    fragments: tuple[PromptFragment, ...],
    provider_id: str,
) -> None:
    """Validate bounded prompts and their provider ownership."""
    if len(fragments) > _MAX_DRIVER_PROMPT_FRAGMENTS:
        raise ValueError(
            f"driver provider '{provider_id}' returned too many prompts",
        )
    fragment_ids: set[str] = set()
    prompt_bytes = 0
    for fragment in fragments:
        if not isinstance(fragment, PromptFragment):
            raise TypeError(
                f"driver provider '{provider_id}' returned an untyped prompt",
            )
        if not fragment.fragment_id.startswith(f"{provider_id}."):
            raise ValueError(
                f"driver prompt '{fragment.fragment_id}' has foreign "
                "ownership",
            )
        if fragment.fragment_id in fragment_ids:
            raise ValueError("driver prompt IDs must be unique")
        fragment_ids.add(fragment.fragment_id)
        prompt_bytes += len(fragment.content.encode("utf-8"))
    if prompt_bytes > _MAX_DRIVER_PROMPT_BYTES:
        raise ValueError(
            f"driver provider '{provider_id}' prompt content is too large",
        )


def validate_driver_session(
    session: object,
    provider_id: str,
) -> tuple[tuple[DriverToolDefinition, ...], tuple[PromptFragment, ...]]:
    """Validate one pinned Driver session before Toolkit publication."""
    if getattr(session, "provider_id", None) != provider_id:
        raise TypeError(
            f"driver session for '{provider_id}' has a mismatched identity",
        )
    definitions = tuple(session.list_tools())
    fragments = tuple(session.prompt_fragments())
    _validate_driver_definitions(definitions, provider_id)
    _validate_driver_fragments(fragments, provider_id)
    return definitions, tuple(
        sorted(fragments, key=lambda item: (item.priority, item.fragment_id)),
    )


__all__ = [
    "ProviderDriverHost",
    "WorkspaceDriverHost",
    "close_driver_session",
    "validate_driver_session",
]
