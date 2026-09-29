# -*- coding: utf-8 -*-
"""Built-in Workspace lifecycle hooks published through the OS catalog."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..kernel.invocation import DEFAULT_HOOK_PROVIDER_ID, InvocationScope
from ..kernel.models import (
    CapabilityBundle,
    CapabilityContribution,
    CapabilityProviderKind,
    HookDefinition,
    HookOutcome,
    RestartPolicy,
)
from ..kernel.ports import HookHost, HookSession

SYSTEM_HOOK_PROVIDER_ID = "qwenpaw.system.hooks"


@dataclass(frozen=True)
class WorkspaceHookSession:
    """Adapt one fixed Workspace hook snapshot to the stable port."""

    host: HookHost
    provider_id: str = DEFAULT_HOOK_PROVIDER_ID

    def list_hooks(self) -> Sequence[HookDefinition]:
        """Return the catalog captured when this invocation opened."""
        return self.host.list_hooks()

    async def run_hook(self, hook_id: str) -> HookOutcome:
        """Execute one compatibility hook through the host."""
        return await self.host.run_hook(hook_id)

    async def close(self) -> None:
        """Leave Workspace-owned hook objects under host ownership."""


class WorkspaceHookProvider:
    """Bind the current Workspace hooks to one invocation."""

    provider_id = DEFAULT_HOOK_PROVIDER_ID

    async def health_check(self) -> bool:
        """Report that the stateless compatibility provider is available."""
        return True

    async def open(
        self,
        scope: InvocationScope,
        host: HookHost,
    ) -> HookSession:
        """Open a session over the host's fixed hook snapshot."""
        del scope
        return WorkspaceHookSession(host)


SYSTEM_HOOK_CAPABILITY_BUNDLE = CapabilityBundle(
    provider_id=SYSTEM_HOOK_PROVIDER_ID,
    provider_kind=CapabilityProviderKind.SYSTEM,
    version="1.0.0",
    restart_policy=RestartPolicy.HOT,
    contributions=(
        CapabilityContribution(
            contribution_id="workspace-hooks",
            slot="hook.provider",
            entrypoint=(
                "qwenpaw.capabilities.system_hooks:WorkspaceHookProvider"
            ),
        ),
    ),
)


def system_hook_contribution_factory(
    declaration: CapabilityContribution,
) -> object:
    """Construct one built-in lifecycle hook capability implementation."""
    if declaration.contribution_id == "workspace-hooks":
        return WorkspaceHookProvider()
    raise LookupError(declaration.contribution_id)


__all__ = [
    "SYSTEM_HOOK_CAPABILITY_BUNDLE",
    "SYSTEM_HOOK_PROVIDER_ID",
    "WorkspaceHookProvider",
    "WorkspaceHookSession",
    "system_hook_contribution_factory",
]
