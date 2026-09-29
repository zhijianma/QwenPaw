# -*- coding: utf-8 -*-
"""Built-in Agent Modes published through the OS capability catalog."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..kernel.invocation import (
    DEFAULT_AGENT_MODE_PROVIDER_ID,
    InvocationScope,
)
from ..kernel.models import (
    CapabilityBundle,
    CapabilityContribution,
    CapabilityProviderKind,
    RestartPolicy,
)
from ..kernel.ports import AgentModeHost, AgentModeSession

SYSTEM_MODE_PROVIDER_ID = "qwenpaw.system.modes"


class _WorkspaceAgentModeHost(AgentModeHost, Protocol):
    """Private lifecycle Host for the built-in compatibility provider."""

    def active_mode_names(self) -> tuple[str, ...]:
        """Return active mode names from the fixed Workspace snapshot."""

    async def start_turn(self) -> None:
        """Prepare the fixed Workspace mode snapshot for this turn."""

    async def reset_conversation(self) -> None:
        """Reset state owned by the fixed Workspace mode snapshot."""


@dataclass(frozen=True)
class WorkspaceAgentModeSession:
    """Adapt one fixed Workspace Agent Mode snapshot to the stable port."""

    host: _WorkspaceAgentModeHost

    def active_mode_names(self) -> tuple[str, ...]:
        """Return active names in deterministic order."""
        return tuple(sorted(set(self.host.active_mode_names())))

    async def start_turn(self) -> None:
        """Prepare every mode captured for this invocation."""
        await self.host.start_turn()

    async def reset_conversation(self) -> None:
        """Reset every mode captured for this invocation."""
        await self.host.reset_conversation()

    async def close(self) -> None:
        """Leave Workspace-owned mode instances under host ownership."""


class WorkspaceAgentModeProvider:
    """Bind the current Workspace Agent Modes to one invocation."""

    provider_id = DEFAULT_AGENT_MODE_PROVIDER_ID

    async def health_check(self) -> bool:
        """Report that the stateless compatibility provider is available."""
        return True

    async def open(
        self,
        scope: InvocationScope,
        host: _WorkspaceAgentModeHost,
    ) -> AgentModeSession:
        """Open one session backed by the host's fixed mode snapshot."""
        del scope
        return WorkspaceAgentModeSession(host)


SYSTEM_MODE_CAPABILITY_BUNDLE = CapabilityBundle(
    provider_id=SYSTEM_MODE_PROVIDER_ID,
    provider_kind=CapabilityProviderKind.SYSTEM,
    version="1.0.0",
    restart_policy=RestartPolicy.HOT,
    contributions=(
        CapabilityContribution(
            contribution_id="workspace-modes",
            slot="agent.mode.provider",
            entrypoint=(
                "qwenpaw.capabilities.system_modes:"
                "WorkspaceAgentModeProvider"
            ),
        ),
    ),
)


def system_mode_contribution_factory(
    declaration: CapabilityContribution,
) -> object:
    """Construct one built-in Agent Mode capability implementation."""
    if declaration.contribution_id == "workspace-modes":
        return WorkspaceAgentModeProvider()
    raise LookupError(declaration.contribution_id)


__all__ = [
    "SYSTEM_MODE_CAPABILITY_BUNDLE",
    "SYSTEM_MODE_PROVIDER_ID",
    "WorkspaceAgentModeProvider",
    "WorkspaceAgentModeSession",
    "system_mode_contribution_factory",
]
