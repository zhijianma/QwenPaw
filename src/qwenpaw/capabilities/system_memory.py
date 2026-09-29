# -*- coding: utf-8 -*-
"""Built-in Workspace memory published through the OS capability catalog."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ..kernel.invocation import (
    DEFAULT_MEMORY_PROVIDER_ID,
    InvocationScope,
)
from ..kernel.models import (
    CapabilityBundle,
    CapabilityContribution,
    CapabilityProviderKind,
    RestartPolicy,
    ToolDefinition,
)
from ..kernel.ports import MemoryHost, MemorySession

SYSTEM_MEMORY_PROVIDER_ID = "qwenpaw.system.memory"


@dataclass(frozen=True)
class WorkspaceMemorySession:
    """Adapt the current BaseMemoryManager to the stable session port."""

    backend: Any

    def get_prompt(self) -> str:
        """Return the current backend's memory guidance."""
        if self.backend is None:
            return ""
        prompt = self.backend.get_memory_prompt()
        return str(prompt or "")

    def list_tools(
        self,
    ) -> Sequence[ToolDefinition | Callable[..., object]]:
        """Return tools exposed by the current backend."""
        if self.backend is None:
            return ()
        return tuple(self.backend.list_memory_tools())

    def build_middlewares(self) -> list[Any]:
        """Preserve the current AgentScope middleware integration."""
        if self.backend is None:
            return []
        factory = getattr(self.backend, "build_middlewares", None)
        return list(factory()) if callable(factory) else []

    async def close(self) -> None:
        """Leave the long-lived Workspace backend under service ownership."""


class WorkspaceMemoryProvider:
    """Bind the configured Workspace backend to one invocation."""

    provider_id = DEFAULT_MEMORY_PROVIDER_ID

    async def health_check(self) -> bool:
        """Report that the stateless provider adapter is available."""
        return True

    async def open(
        self,
        scope: InvocationScope,
        host: MemoryHost,
    ) -> MemorySession:
        """Open one compatibility session without owning backend lifetime."""
        del scope
        compatibility_backend = getattr(host, "compatibility_backend", None)
        if not callable(compatibility_backend):
            raise TypeError("system memory requires its compatibility host")
        return WorkspaceMemorySession(compatibility_backend())


SYSTEM_MEMORY_CAPABILITY_BUNDLE = CapabilityBundle(
    provider_id=SYSTEM_MEMORY_PROVIDER_ID,
    provider_kind=CapabilityProviderKind.SYSTEM,
    version="1.0.0",
    restart_policy=RestartPolicy.HOT,
    contributions=(
        CapabilityContribution(
            contribution_id="workspace-memory",
            slot="memory.provider",
            entrypoint=(
                "qwenpaw.capabilities.system_memory:WorkspaceMemoryProvider"
            ),
        ),
    ),
)


def system_memory_contribution_factory(
    declaration: CapabilityContribution,
) -> object:
    """Construct one built-in memory capability implementation."""
    if declaration.contribution_id == "workspace-memory":
        return WorkspaceMemoryProvider()
    raise LookupError(declaration.contribution_id)


__all__ = [
    "SYSTEM_MEMORY_CAPABILITY_BUNDLE",
    "SYSTEM_MEMORY_PROVIDER_ID",
    "WorkspaceMemorySession",
    "WorkspaceMemoryProvider",
    "system_memory_contribution_factory",
]
