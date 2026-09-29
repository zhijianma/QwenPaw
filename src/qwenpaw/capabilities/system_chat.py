# -*- coding: utf-8 -*-
"""Built-in Chat runtime capabilities published through the OS catalog."""

from __future__ import annotations

from typing import Any

from ..kernel.invocation import DEFAULT_AGENT_FACTORY_ID
from ..kernel.models import (
    CapabilityBundle,
    CapabilityContribution,
    CapabilityProviderKind,
    RestartPolicy,
)

SYSTEM_CHAT_PROVIDER_ID = "qwenpaw.system.chat"


class LegacyAgentFactory:
    """Adapt the current AgentBuilder to the new invocation boundary."""

    factory_id = DEFAULT_AGENT_FACTORY_ID

    async def health_check(self) -> bool:
        """Report that the compatibility factory is available."""
        return True

    async def build(self, context: Any, app_services: Any) -> Any:
        """Build the existing Agent through the stable capability slot."""
        from ..runtime import runtime as runtime_module

        builder = runtime_module.AgentBuilder(app_services=app_services)
        return await builder.build(context)


SYSTEM_CHAT_CAPABILITY_BUNDLE = CapabilityBundle(
    provider_id=SYSTEM_CHAT_PROVIDER_ID,
    provider_kind=CapabilityProviderKind.SYSTEM,
    version="1.0.0",
    restart_policy=RestartPolicy.HOT,
    contributions=(
        CapabilityContribution(
            contribution_id="agent-factory",
            slot="agent.factory",
            entrypoint=("qwenpaw.capabilities.system_chat:LegacyAgentFactory"),
        ),
    ),
)


def system_chat_contribution_factory(
    declaration: CapabilityContribution,
) -> object:
    """Construct one built-in Chat capability implementation."""
    if declaration.contribution_id == "agent-factory":
        return LegacyAgentFactory()
    raise LookupError(declaration.contribution_id)


__all__ = [
    "SYSTEM_CHAT_CAPABILITY_BUNDLE",
    "SYSTEM_CHAT_PROVIDER_ID",
    "LegacyAgentFactory",
    "system_chat_contribution_factory",
]
