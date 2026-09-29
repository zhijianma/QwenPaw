# -*- coding: utf-8 -*-
"""Minimal policy-owning Driver Provider using the public SDK."""

from qwenpaw.plugins.sdk import (
    DriverApprovalRequest,
    DriverCredentialHandle,
    DriverHost,
    DriverToolDefinition,
    InvocationScope,
    JsonObject,
    PromptFragment,
)

PROVIDER_ID = "runtime-provider-kit.example-driver"


class ExampleDriverSession:
    """Immutable Driver capability snapshot for one invocation."""

    provider_id = PROVIDER_ID

    def __init__(
        self,
        host: DriverHost,
        echo_prefix: str,
        credential: DriverCredentialHandle | None,
    ) -> None:
        self._host = host
        self._echo_prefix = echo_prefix
        self._credential = credential

    async def _approved_echo(self, payload: JsonObject) -> object:
        """Wait for the host's durable approval before execution."""
        await self._host.require_approval(
            DriverApprovalRequest(
                provider_id=self.provider_id,
                capability_id=("driver://example/local/tools/echo#invoke"),
                tool_name="driver_echo",
                redacted_arguments={"text": payload.get("text", "")},
            ),
        )
        return {
            "echo": f"{self._echo_prefix}{payload.get('text', '')}",
            "credential_configured": self._credential is not None,
        }

    def list_tools(self) -> tuple[DriverToolDefinition, ...]:
        """Return typed definitions without importing AgentScope."""
        return (
            DriverToolDefinition(
                provider_id=self.provider_id,
                capability_id="driver://example/local/tools/echo#invoke",
                name="driver_echo",
                description="Echo text through the example Driver.",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                invoke=self._approved_echo,
            ),
        )

    def prompt_fragments(self) -> tuple[PromptFragment, ...]:
        """Return provider-owned guidance with deterministic ordering."""
        return (
            PromptFragment(
                fragment_id=f"{self.provider_id}.policy",
                content=(
                    "The example Driver evaluates policy before invoking "
                    "driver_echo."
                ),
                priority=100,
            ),
        )

    async def close(self) -> None:
        """Release only invocation-owned Driver resources."""
        return None


class ExampleDriverProvider:
    """Open one typed Driver session from the pinned generation."""

    provider_id = PROVIDER_ID

    async def health_check(self) -> bool:
        """Report whether this contribution can be published."""
        return True

    async def open(
        self,
        scope: InvocationScope,
        host: DriverHost,
    ) -> ExampleDriverSession:
        """Create a session without accessing application globals."""
        del scope
        config = host.config_snapshot()
        return ExampleDriverSession(
            host,
            str(config.get("echo_prefix", "")),
            host.credential("service"),
        )


def create_provider() -> ExampleDriverProvider:
    """Create the manifest contribution implementation."""
    return ExampleDriverProvider()
