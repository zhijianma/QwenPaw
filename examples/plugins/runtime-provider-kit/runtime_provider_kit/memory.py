# -*- coding: utf-8 -*-
"""Minimal invocation-scoped Memory Provider using the public SDK."""

from pathlib import Path

from qwenpaw.plugins.sdk import (
    InvocationScope,
    MemoryHost,
    MemoryStateScope,
    MemoryStateStore,
    ToolDefinition,
)


class ProjectMemorySession:
    """Memory state owned by one pinned invocation."""

    def __init__(
        self,
        project_name: str,
        prompt_prefix: str,
        state: MemoryStateStore,
    ) -> None:
        self._project_name = project_name
        self._prompt_prefix = prompt_prefix
        self._state = state

    def get_prompt(self) -> str:
        """Return bounded guidance without mutating Workspace memory."""
        return f"{self._prompt_prefix} {self._project_name}.".strip()

    async def remember_project_label(self, label: str) -> str:
        """Persist one agent-owned label with optimistic concurrency."""
        current = await self._state.read("project-label")
        snapshot = await self._state.write(
            "project-label",
            label,
            expected_revision=current.revision if current else 0,
        )
        self._project_name = str(snapshot.value)
        return self._project_name

    def list_tools(self) -> tuple[ToolDefinition, ...]:
        """Return one typed tool backed by public Memory state."""
        return (
            ToolDefinition(
                function=self.remember_project_label,
                name="remember_project_label",
                tool_type="internal",
            ),
        )

    async def close(self) -> None:
        """Release only resources owned by this session."""
        return None


class ProjectMemoryProvider:
    """Open one memory session for each immutable invocation."""

    provider_id = "runtime-provider-kit.project-memory"

    async def health_check(self) -> bool:
        """Report whether the provider can be published."""
        return True

    async def open(
        self,
        scope: InvocationScope,
        host: MemoryHost,
    ) -> ProjectMemorySession:
        """Create an isolated session from public invocation data."""
        config = host.config_snapshot()
        state = host.state(MemoryStateScope.AGENT)
        project_name = Path(scope.workspace_dir).name
        saved = await state.read("project-label")
        if saved is not None and isinstance(saved.value, str):
            project_name = saved.value
        return ProjectMemorySession(
            project_name or "workspace",
            str(
                config.get("prompt_prefix", "Keep terminology consistent for"),
            ),
            state,
        )


def create_provider() -> ProjectMemoryProvider:
    """Create the manifest contribution implementation."""
    return ProjectMemoryProvider()
