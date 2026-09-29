# -*- coding: utf-8 -*-
"""Built-in Workspace stop gates published through the OS catalog."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..kernel.invocation import (
    DEFAULT_STOP_GATE_PROVIDER_ID,
    InvocationScope,
)
from ..kernel.models import (
    CapabilityBundle,
    CapabilityContribution,
    CapabilityProviderKind,
    RestartPolicy,
    StopGateDecision,
    StopGateDefinition,
    StopGateInput,
)
from ..kernel.ports import StopGateHost, StopGateSession

SYSTEM_STOP_GATE_PROVIDER_ID = "qwenpaw.system.loop-gates"


@dataclass(frozen=True)
class WorkspaceStopGateSession:
    """Adapt one fixed Workspace stop-handler snapshot to the stable port."""

    host: StopGateHost
    provider_id: str = DEFAULT_STOP_GATE_PROVIDER_ID

    def list_gates(self) -> Sequence[StopGateDefinition]:
        """Return the catalog captured when this invocation opened."""
        return self.host.list_gates()

    def is_active(self, gate_id: str) -> bool:
        """Delegate legacy mode-scope activity checks."""
        return self.host.is_active(gate_id)

    async def evaluate(
        self,
        gate_id: str,
        gate_input: StopGateInput,
    ) -> StopGateDecision:
        """Evaluate one compatibility stop-handler registration."""
        return await self.host.evaluate(gate_id, gate_input)

    async def start_turn(self) -> None:
        """Let Agent Modes retain ownership of legacy gate resets."""

    async def reset_conversation(self) -> None:
        """Let Agent Modes retain ownership of legacy session resets."""

    async def close(self) -> None:
        """Leave Workspace-owned gates under host ownership."""


class WorkspaceStopGateProvider:
    """Bind the current Workspace stop handlers to one invocation."""

    provider_id = DEFAULT_STOP_GATE_PROVIDER_ID

    async def health_check(self) -> bool:
        """Report that the stateless compatibility provider is available."""
        return True

    async def open(
        self,
        scope: InvocationScope,
        host: StopGateHost,
    ) -> StopGateSession:
        """Open a session over the host's fixed registration snapshot."""
        del scope
        return WorkspaceStopGateSession(host)


SYSTEM_STOP_GATE_CAPABILITY_BUNDLE = CapabilityBundle(
    provider_id=SYSTEM_STOP_GATE_PROVIDER_ID,
    provider_kind=CapabilityProviderKind.SYSTEM,
    version="1.0.0",
    restart_policy=RestartPolicy.HOT,
    contributions=(
        CapabilityContribution(
            contribution_id="workspace-gates",
            slot="loop.gate.provider",
            entrypoint=(
                "qwenpaw.capabilities.system_stop_gates:"
                "WorkspaceStopGateProvider"
            ),
        ),
    ),
)


def system_stop_gate_contribution_factory(
    declaration: CapabilityContribution,
) -> object:
    """Construct one built-in stop-gate capability implementation."""
    if declaration.contribution_id == "workspace-gates":
        return WorkspaceStopGateProvider()
    raise LookupError(declaration.contribution_id)


__all__ = [
    "SYSTEM_STOP_GATE_CAPABILITY_BUNDLE",
    "SYSTEM_STOP_GATE_PROVIDER_ID",
    "WorkspaceStopGateProvider",
    "WorkspaceStopGateSession",
    "system_stop_gate_contribution_factory",
]
