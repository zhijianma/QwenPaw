# -*- coding: utf-8 -*-
"""Built-in Workspace Drivers published through the OS catalog."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from ..kernel.invocation import DEFAULT_DRIVER_PROVIDER_ID, InvocationScope
from ..kernel.models import (
    CapabilityBundle,
    CapabilityContribution,
    CapabilityProviderKind,
    RestartPolicy,
    DriverToolDefinition,
    PromptFragment,
)
from ..kernel.ports import DriverHost, DriverSession

SYSTEM_DRIVER_PROVIDER_ID = "qwenpaw.system.drivers"


class _WorkspaceDriverHost(DriverHost, Protocol):
    """Private compatibility Host for the built-in Workspace adapter."""

    async def load(
        self,
    ) -> tuple[Sequence[DriverToolDefinition], Sequence[PromptFragment]]:
        """Return policy-owned tools and identified prompt fragments."""


@dataclass(frozen=True)
class WorkspaceDriverSession:
    """Hold request-scoped tools while the Workspace owns Driver services."""

    tools: tuple[DriverToolDefinition, ...]
    fragments: tuple[PromptFragment, ...]
    provider_id: str = DEFAULT_DRIVER_PROVIDER_ID

    def list_tools(self) -> Sequence[DriverToolDefinition]:
        """Return the tools resolved for this invocation."""
        return self.tools

    def prompt_fragments(self) -> Sequence[PromptFragment]:
        """Return identified Driver guidance for this invocation."""
        return self.fragments

    async def close(self) -> None:
        """Leave long-lived Driver services under Workspace ownership."""


class WorkspaceDriverProvider:
    """Bind configured Workspace Drivers to one invocation."""

    provider_id = DEFAULT_DRIVER_PROVIDER_ID

    async def health_check(self) -> bool:
        """Report that the stateless compatibility provider is available."""
        return True

    async def open(
        self,
        scope: InvocationScope,
        host: _WorkspaceDriverHost,
    ) -> DriverSession:
        """Resolve current Driver tools and hints once per invocation."""
        del scope
        tools, fragments = await host.load()
        return WorkspaceDriverSession(tuple(tools), tuple(fragments))


SYSTEM_DRIVER_CAPABILITY_BUNDLE = CapabilityBundle(
    provider_id=SYSTEM_DRIVER_PROVIDER_ID,
    provider_kind=CapabilityProviderKind.SYSTEM,
    version="1.0.0",
    restart_policy=RestartPolicy.HOT,
    contributions=(
        CapabilityContribution(
            contribution_id="workspace-driver",
            slot="driver.provider",
            entrypoint=(
                "qwenpaw.capabilities.system_drivers:WorkspaceDriverProvider"
            ),
        ),
    ),
)


def system_driver_contribution_factory(
    declaration: CapabilityContribution,
) -> object:
    """Construct one built-in Driver capability implementation."""
    if declaration.contribution_id == "workspace-driver":
        return WorkspaceDriverProvider()
    raise LookupError(declaration.contribution_id)


__all__ = [
    "SYSTEM_DRIVER_CAPABILITY_BUNDLE",
    "SYSTEM_DRIVER_PROVIDER_ID",
    "WorkspaceDriverProvider",
    "WorkspaceDriverSession",
    "system_driver_contribution_factory",
]
