# -*- coding: utf-8 -*-
"""Built-in Workspace commands published through the OS catalog."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..kernel.invocation import (
    DEFAULT_COMMAND_PROVIDER_ID,
    InvocationScope,
)
from ..kernel.models import (
    CapabilityBundle,
    CapabilityContribution,
    CapabilityProviderKind,
    CommandDefinition,
    CommandRequest,
    CommandResult,
    RestartPolicy,
)
from ..kernel.ports import CommandHost, CommandSession

SYSTEM_COMMAND_PROVIDER_ID = "qwenpaw.system.commands"


@dataclass(frozen=True)
class WorkspaceCommandSession:
    """Adapt one fixed Workspace command catalog to the stable port."""

    host: CommandHost
    provider_id: str = DEFAULT_COMMAND_PROVIDER_ID
    allows_dynamic_fallback: bool = True

    def list_commands(self) -> Sequence[CommandDefinition]:
        """Return the catalog captured when this invocation opened."""
        return self.host.list_commands()

    async def dispatch(self, request: CommandRequest) -> CommandResult:
        """Execute one catalog command through the compatibility host."""
        return await self.host.dispatch(request)

    async def fallback(self, request: CommandRequest) -> CommandResult:
        """Resolve the system-owned dynamic Skill fallback."""
        return await self.host.fallback(request)

    async def close(self) -> None:
        """Leave Workspace-owned command handlers under host ownership."""


class WorkspaceCommandProvider:
    """Bind the current Workspace command catalog to one invocation."""

    provider_id = DEFAULT_COMMAND_PROVIDER_ID

    async def health_check(self) -> bool:
        """Report that the stateless compatibility provider is available."""
        return True

    async def open(
        self,
        scope: InvocationScope,
        host: CommandHost,
    ) -> CommandSession:
        """Open a session over the host's already-fixed registry snapshot."""
        del scope
        return WorkspaceCommandSession(host)


SYSTEM_COMMAND_CAPABILITY_BUNDLE = CapabilityBundle(
    provider_id=SYSTEM_COMMAND_PROVIDER_ID,
    provider_kind=CapabilityProviderKind.SYSTEM,
    version="1.0.0",
    restart_policy=RestartPolicy.HOT,
    contributions=(
        CapabilityContribution(
            contribution_id="workspace-commands",
            slot="command.provider",
            entrypoint=(
                "qwenpaw.capabilities.system_commands:"
                "WorkspaceCommandProvider"
            ),
        ),
    ),
)


def system_command_contribution_factory(
    declaration: CapabilityContribution,
) -> object:
    """Construct one built-in command capability implementation."""
    if declaration.contribution_id == "workspace-commands":
        return WorkspaceCommandProvider()
    raise LookupError(declaration.contribution_id)


__all__ = [
    "SYSTEM_COMMAND_CAPABILITY_BUNDLE",
    "SYSTEM_COMMAND_PROVIDER_ID",
    "WorkspaceCommandProvider",
    "WorkspaceCommandSession",
    "system_command_contribution_factory",
]
