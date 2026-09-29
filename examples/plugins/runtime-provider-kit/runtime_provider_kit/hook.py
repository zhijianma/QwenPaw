# -*- coding: utf-8 -*-
"""Minimal lifecycle Hook Provider using only the public plugin SDK."""

from pathlib import Path

from qwenpaw.plugins.sdk import (
    HookDefinition,
    HookHost,
    HookOutcome,
    HookSession,
    InvocationScope,
    LifecyclePhase,
)


class ProjectHookSession:
    """Inject project context through one invocation-scoped host."""

    provider_id = "runtime-provider-kit.project-hooks"

    def __init__(self, project_name: str, host: HookHost) -> None:
        self._project_name = project_name
        self._host = host

    def list_hooks(self) -> tuple[HookDefinition, ...]:
        """Return a deterministic provider-owned hook catalog."""
        return (
            HookDefinition(
                hook_id=f"{self.provider_id}.project-context",
                provider_id=self.provider_id,
                phase=LifecyclePhase.PRE_AGENT_BUILD,
                priority=120,
            ),
        )

    async def run_hook(self, hook_id: str) -> HookOutcome:
        """Inject bounded context for the declared hook."""
        expected_id = f"{self.provider_id}.project-context"
        if hook_id != expected_id:
            raise LookupError(hook_id)
        self._host.inject_context(
            f"The active project is {self._project_name}.",
            priority=120,
            source=hook_id,
        )
        return HookOutcome()

    async def close(self) -> None:
        """Release no resources for this stateless session."""


class ProjectHookProvider:
    """Open one project context hook per invocation."""

    provider_id = ProjectHookSession.provider_id

    async def health_check(self) -> bool:
        """Report that this stateless provider can be published."""
        return True

    async def open(
        self,
        scope: InvocationScope,
        host: HookHost,
    ) -> HookSession:
        """Capture project identity and the invocation-scoped host."""
        return ProjectHookSession(
            Path(scope.workspace_dir).name or "workspace",
            host,
        )


def create_provider() -> ProjectHookProvider:
    """Create the manifest contribution implementation."""
    return ProjectHookProvider()
