# -*- coding: utf-8 -*-
"""Minimal Tool Provider implemented only with the stable SDK."""

from collections.abc import Sequence

from qwenpaw.plugins.sdk import (
    ActionIdempotencyMode,
    InteractionOption,
    InvocationScope,
    RuntimeInteractionProducer,
    ToolDefinition,
    ToolHost,
    ToolSelection,
)


def _description_tool(
    description: str,
    credential_configured: bool,
) -> ToolDefinition:
    """Build one invocation tool from public Host values."""

    async def describe_qwenpaw_invocation() -> str:
        """Return a short description of the active QwenPaw invocation."""
        credential_status = (
            "credential configured"
            if credential_configured
            else "no credential configured"
        )
        return f"{description} ({credential_status})"

    return ToolDefinition(
        function=describe_qwenpaw_invocation,
        name="describe_qwenpaw_invocation",
        tool_type="internal",
        idempotency_mode=ActionIdempotencyMode.HOST_GUARDED,
    )


def _suggestion_tool(
    broker: RuntimeInteractionProducer,
) -> ToolDefinition:
    """Build a plugin tool using the same interaction API as built-ins."""

    async def suggest_workspace_review() -> str:
        """Suggest an optional workspace review without blocking the run."""
        await broker.suggest(
            title="Plugin suggestion",
            prompt="Consider reviewing the active workspace configuration.",
            options=(
                InteractionOption(
                    option_id="review_workspace",
                    label="Review workspace",
                ),
            ),
            metadata={"source": "chat-tool-provider.workspace-info"},
        )
        return "Plugin suggestion delivered without blocking the run."

    return ToolDefinition(
        function=suggest_workspace_review,
        name="suggest_workspace_review",
        tool_type="internal",
    )


class WorkspaceInfoProvider:
    """Contribute one governed, task-independent Chat tool."""

    provider_id = "chat-tool-provider.workspace-info"

    async def health_check(self) -> bool:
        """Allow activation only when the provider is ready."""
        return True

    async def list_tools(
        self,
        scope: InvocationScope,
        selection: ToolSelection,
        host: ToolHost,
    ) -> Sequence[ToolDefinition]:
        """Return tools available to this immutable invocation."""
        del scope, selection
        config = host.config_snapshot()
        description = str(
            config.get(
                "description",
                "This tool is running through a pinned QwenPaw OS generation.",
            ),
        )
        definitions = [
            _description_tool(
                description,
                host.credential("service") is not None,
            ),
        ]
        broker = host.interaction_broker()
        if broker is not None:
            definitions.append(_suggestion_tool(broker))
        return tuple(definitions)


def create_provider() -> WorkspaceInfoProvider:
    """Create the stateless provider staged during plugin activation."""
    return WorkspaceInfoProvider()
