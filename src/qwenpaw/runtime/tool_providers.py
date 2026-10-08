# -*- coding: utf-8 -*-
"""Runtime adapters for provider-neutral tool capability contracts."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ..kernel.models import ToolSelection
from ..kernel.ports import OutcomeHost, RuntimeInteractionProducer
from .provider_credentials import (
    WorkspaceCredentialHandle,
    provider_credential_handle,
)


def _interaction_broker(
    request_context: dict[str, Any],
) -> RuntimeInteractionProducer | None:
    broker = request_context.get("_interaction_broker")
    if isinstance(broker, RuntimeInteractionProducer):
        return broker
    return None


@dataclass(frozen=True)
class ProviderToolHost:
    """Minimal public Host supplied to third-party Tool Providers."""

    _provider_id: str
    _provider_config: dict[str, Any]
    _credential_manager: Any
    _credential_refs: dict[str, str]
    _broker: RuntimeInteractionProducer | None
    _outcomes: OutcomeHost | None = None

    def config_snapshot(self) -> dict[str, Any]:
        """Return detached, schema-validated provider configuration."""
        return deepcopy(self._provider_config)

    def credential(self, alias: str) -> WorkspaceCredentialHandle | None:
        """Return only a credential alias bound to this provider."""
        return provider_credential_handle(
            self._credential_manager,
            self._credential_refs,
            self._provider_id,
            alias,
        )

    def interaction_broker(self) -> RuntimeInteractionProducer | None:
        """Return the invocation broker without request-context access."""
        return self._broker

    def outcome_host(self) -> OutcomeHost | None:
        """Return only a Host-admitted invocation outcome service."""
        return self._outcomes


@dataclass(frozen=True)
class WorkspaceToolHost:
    """Internal compatibility Host for the built-in Workspace provider."""

    local_workspace: Any
    agent_config: Any
    request_context: dict[str, Any]
    governor: Any
    provider_id: str = "qwenpaw.system.workspace-tools"
    provider_config: dict[str, Any] | None = None
    credential_manager: Any = None
    credential_refs: dict[str, str] | None = None
    outcomes: OutcomeHost | None = None

    def config_snapshot(self) -> dict[str, Any]:
        """Return detached built-in provider configuration."""
        return deepcopy(self.provider_config or {})

    def credential(self, alias: str) -> WorkspaceCredentialHandle | None:
        """Return one built-in alias without exposing the credential store."""
        return provider_credential_handle(
            self.credential_manager,
            self.credential_refs or {},
            self.provider_id,
            alias,
        )

    async def list_workspace_tools(
        self,
        selection: ToolSelection,
    ) -> Sequence[Callable[..., object]]:
        """Return tools after existing config and governance filtering."""
        if self.local_workspace is None:
            return ()
        self.local_workspace.set_governor(self.governor)
        return await self.local_workspace.list_tools(
            agent_config=self.agent_config,
            request_context=self.request_context,
            active_modes=set(selection.active_modes),
            active_skills=set(selection.active_skills),
            enabled_features=set(selection.enabled_features),
        )

    def interaction_broker(self) -> RuntimeInteractionProducer | None:
        """Expose the broker without leaking request-context keys."""
        return _interaction_broker(self.request_context)

    def outcome_host(self) -> OutcomeHost | None:
        """Return the same admitted service exposed to plugin providers."""
        return self.outcomes


def tool_selection_from_request(
    *,
    active_modes: Sequence[str],
    active_skills: Sequence[str],
    enabled_features: Sequence[str],
    request_context: dict[str, Any] | None,
) -> ToolSelection:
    """Build the stable selection contract from current Chat state."""
    raw_whitelist = (request_context or {}).get("subagent_allowed_tools")
    whitelist = (
        tuple(item for item in raw_whitelist if isinstance(item, str))
        if isinstance(raw_whitelist, list)
        else None
    )
    return ToolSelection(
        active_modes=tuple(sorted(set(active_modes))),
        active_skills=tuple(sorted(set(active_skills))),
        enabled_features=tuple(sorted(set(enabled_features))),
        subagent_allowed_tools=whitelist,
    )


__all__ = [
    "ProviderToolHost",
    "WorkspaceToolHost",
    "tool_selection_from_request",
]
