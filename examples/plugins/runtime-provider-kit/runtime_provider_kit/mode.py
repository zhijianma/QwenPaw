# -*- coding: utf-8 -*-
"""Minimal invocation-scoped Agent Mode using the public SDK."""

from qwenpaw.plugins.sdk import (
    AgentModeHost,
    InvocationScope,
)

PROVIDER_ID = "runtime-provider-kit.review-mode"


class ExampleModeSession:
    """Own one plugin Mode lifecycle without accessing Workspace modes."""

    def __init__(self, mode_name: str, host: AgentModeHost) -> None:
        self.mode_name = mode_name
        self.host = host
        self.turn_starts = 0
        self.conversation_resets = 0
        self.closed = False

    def active_mode_names(self) -> tuple[str, ...]:
        """Return names used by the shared Prompt and Tool pipeline."""
        return (self.mode_name,)

    async def start_turn(self) -> None:
        """Prepare only state owned by this plugin session."""
        await self._increment("turn_starts")

    async def reset_conversation(self) -> None:
        """Clear only conversation state owned by this plugin session."""
        await self._increment("conversation_resets")

    async def _increment(self, field: str) -> None:
        """Persist lifecycle state without opening Host storage directly."""
        current = await self.host.read_state("lifecycle")
        value = dict(current.value) if current is not None else {}
        value[field] = int(value.get(field, 0)) + 1
        persisted = await self.host.write_state(
            value,
            expected_revision=(current.revision if current else 0),
            state_key="lifecycle",
        )
        self.turn_starts = int(persisted.value.get("turn_starts", 0))
        self.conversation_resets = int(
            persisted.value.get("conversation_resets", 0),
        )

    async def close(self) -> None:
        """Release only invocation-scoped plugin resources."""
        self.closed = True


class ExampleModeProvider:
    """Open a Mode session from validated non-secret configuration."""

    provider_id = PROVIDER_ID

    async def health_check(self) -> bool:
        """Report whether this contribution can be published."""
        return True

    async def open(
        self,
        scope: InvocationScope,
        host: AgentModeHost,
    ) -> ExampleModeSession:
        """Bind one configured Mode name to the pinned invocation."""
        del scope
        config = host.config_snapshot()
        return ExampleModeSession(
            str(config.get("mode_name", "review")),
            host,
        )


def create_provider() -> ExampleModeProvider:
    """Create the manifest contribution implementation."""
    return ExampleModeProvider()
