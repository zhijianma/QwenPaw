# -*- coding: utf-8 -*-
"""Minimal Command Provider using only the public plugin SDK."""

from pathlib import Path

from qwenpaw.plugins.sdk import (
    CommandDefinition,
    CommandDisposition,
    CommandHost,
    CommandMessage,
    CommandRequest,
    CommandResult,
    CommandSession,
    InvocationScope,
)


class ProjectCommandSession:
    """Static commands bound to one immutable invocation scope."""

    provider_id = "runtime-provider-kit.project-commands"
    allows_dynamic_fallback = False

    def __init__(self, project_name: str) -> None:
        self._project_name = project_name

    def list_commands(self) -> tuple[CommandDefinition, ...]:
        """Return a provider-owned command catalog."""
        return (
            CommandDefinition(
                command_id=f"{self.provider_id}.project-name",
                provider_id=self.provider_id,
                name="project-name",
                aliases=("project",),
                help_text="Show the project captured by this invocation.",
            ),
        )

    async def dispatch(self, request: CommandRequest) -> CommandResult:
        """Return a framework-independent command response."""
        suffix = f" ({request.arguments})" if request.arguments else ""
        return CommandResult(
            disposition=CommandDisposition.RESPOND,
            message=CommandMessage(
                text=f"Project: {self._project_name}{suffix}",
            ),
        )

    async def fallback(self, request: CommandRequest) -> CommandResult:
        """Decline uncatalogued commands; plugins cannot own fallback."""
        del request
        return CommandResult(disposition=CommandDisposition.NOT_HANDLED)

    async def close(self) -> None:
        """Release no resources for this stateless session."""


class ProjectCommandProvider:
    """Open one project command session per invocation."""

    provider_id = "runtime-provider-kit.project-commands"

    async def health_check(self) -> bool:
        """Report that this stateless provider can be published."""
        return True

    async def open(
        self,
        scope: InvocationScope,
        host: CommandHost,
    ) -> CommandSession:
        """Capture project identity without retaining the private Host."""
        del host
        return ProjectCommandSession(
            Path(scope.workspace_dir).name or "workspace",
        )


def create_provider() -> ProjectCommandProvider:
    """Create the manifest contribution implementation."""
    return ProjectCommandProvider()
