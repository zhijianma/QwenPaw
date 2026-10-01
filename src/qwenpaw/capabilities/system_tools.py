# -*- coding: utf-8 -*-
"""Built-in workspace tools published through the OS capability catalog."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from ..kernel.invocation import (
    DEFAULT_TOOL_PROVIDER_ID,
    InvocationScope,
)
from ..kernel.models import (
    CapabilityBundle,
    CapabilityContribution,
    CapabilityProviderKind,
    RestartPolicy,
    ToolDefinition,
    ToolSelection,
)
from ..kernel.interactions import (
    InteractionOption,
)
from ..kernel.ports import RuntimeInteractionProducer, ToolHost

SYSTEM_TOOL_PROVIDER_ID = "qwenpaw.system"


def _ask_user_tool(
    broker: RuntimeInteractionProducer,
) -> ToolDefinition:
    """Create the built-in tool against one invocation-bound broker."""

    async def ask_user(
        question: str,
        choices: list[str] | None = None,
    ) -> str:
        """Ask for input and continue through a durable future turn."""
        prompt = question.strip()
        if not prompt:
            return "User input request was rejected: question is empty."
        normalized_choices = tuple(
            choice.strip() for choice in (choices or [])[:12] if choice.strip()
        )
        labels = {
            f"choice_{index}": choice
            for index, choice in enumerate(normalized_choices, start=1)
        }
        request = await broker.defer_user_input(
            title="Input requested",
            prompt=prompt,
            options=tuple(
                InteractionOption(option_id=option_id, label=label)
                for option_id, label in labels.items()
            ),
            metadata={"source": "qwenpaw.system.ask_user"},
        )
        return (
            "User input requested. End this turn now; QwenPaw will "
            "continue in a new turn after the user responds. "
            f"Interaction: {request.interaction_id}."
        )

    return ToolDefinition(
        function=ask_user,
        name="ask_user",
        tool_type="internal",
    )


def _suggest_user_action_tool(
    broker: RuntimeInteractionProducer,
) -> ToolDefinition:
    """Create a non-blocking suggestion tool against the same broker."""

    async def suggest_user_action(
        suggestion: str,
        actions: list[str] | None = None,
    ) -> str:
        """Suggest an optional next action without pausing execution."""
        prompt = suggestion.strip()
        if not prompt:
            return "Suggestion was rejected: suggestion is empty."
        normalized_actions = tuple(
            action.strip() for action in (actions or [])[:12] if action.strip()
        )
        await broker.suggest(
            title="Suggested next step",
            prompt=prompt,
            options=tuple(
                InteractionOption(
                    option_id=f"action_{index}",
                    label=action,
                )
                for index, action in enumerate(
                    normalized_actions,
                    start=1,
                )
            ),
            metadata={"source": "qwenpaw.system.suggest_user_action"},
        )
        return "Suggestion delivered without pausing the conversation."

    return ToolDefinition(
        function=suggest_user_action,
        name="suggest_user_action",
        tool_type="internal",
    )


def _tool_name(tool: object) -> str:
    """Return the model-facing name from either supported tool shape."""
    if isinstance(tool, ToolDefinition):
        return tool.name
    return str(getattr(tool, "__name__", ""))


class WorkspaceToolProvider:
    """Expose the governed per-workspace tool registry through one slot."""

    provider_id = DEFAULT_TOOL_PROVIDER_ID

    async def health_check(self) -> bool:
        """Report that the stateless provider adapter is available."""
        return True

    async def list_tools(
        self,
        scope: InvocationScope,
        selection: ToolSelection,
        host: ToolHost,
    ) -> Sequence[ToolDefinition | Callable[..., object]]:
        """Delegate selection and governance to the invocation host."""
        del scope
        list_workspace_tools = getattr(host, "list_workspace_tools", None)
        if not callable(list_workspace_tools):
            raise TypeError("system tools require their compatibility host")
        workspace_tools = await list_workspace_tools(selection)
        broker = host.interaction_broker()
        if broker is None:
            return workspace_tools
        names = {_tool_name(tool) for tool in workspace_tools}
        additions: list[ToolDefinition] = []
        if "ask_user" not in names:
            additions.append(_ask_user_tool(broker))
        if "suggest_user_action" not in names:
            additions.append(_suggest_user_action_tool(broker))
        return (*workspace_tools, *additions)


SYSTEM_TOOL_CAPABILITY_BUNDLE = CapabilityBundle(
    provider_id=SYSTEM_TOOL_PROVIDER_ID,
    provider_kind=CapabilityProviderKind.SYSTEM,
    version="1.0.0",
    restart_policy=RestartPolicy.HOT,
    contributions=(
        CapabilityContribution(
            contribution_id="workspace-tools",
            slot="tool.provider",
            entrypoint=(
                "qwenpaw.capabilities.system_tools:WorkspaceToolProvider"
            ),
        ),
    ),
)


def system_tool_contribution_factory(
    declaration: CapabilityContribution,
) -> object:
    """Construct one built-in tool capability implementation."""
    if declaration.contribution_id == "workspace-tools":
        return WorkspaceToolProvider()
    raise LookupError(declaration.contribution_id)


__all__ = [
    "SYSTEM_TOOL_CAPABILITY_BUNDLE",
    "SYSTEM_TOOL_PROVIDER_ID",
    "WorkspaceToolProvider",
    "system_tool_contribution_factory",
]
