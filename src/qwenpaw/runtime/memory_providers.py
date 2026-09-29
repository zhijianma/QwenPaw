# -*- coding: utf-8 -*-
"""Runtime adapters for provider-neutral memory capability contracts."""

from __future__ import annotations

import inspect
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..kernel.memory import MemoryStateScope, MemoryStateUnavailableError
from .memory_state import SQLiteMemoryStateStore


@dataclass(frozen=True, kw_only=True)
class ProviderMemoryHost:
    """Minimal config and state Host exposed to third-party providers."""

    provider_id: str = "qwenpaw.system.memory.workspace-memory"
    agent_id: str = "default"
    conversation_id: str | None = None
    workspace_dir: str | Path = "."
    provider_config: dict[str, Any] | None = None

    def config_snapshot(self) -> dict[str, Any]:
        """Return a detached invocation configuration snapshot."""
        return deepcopy(self.provider_config or {})

    def state(self, scope: MemoryStateScope) -> SQLiteMemoryStateStore:
        """Return one provider-owned agent or Conversation namespace."""
        owner_id = self.agent_id
        if scope is MemoryStateScope.CONVERSATION:
            if not self.conversation_id:
                raise MemoryStateUnavailableError(
                    "conversation memory requires a stable ChatSpec.id",
                )
            owner_id = self.conversation_id
        return SQLiteMemoryStateStore(
            Path(self.workspace_dir)
            / ".qwenpaw"
            / "runtime"
            / "memory-state.sqlite3",
            provider_id=self.provider_id,
            scope=scope,
            owner_id=owner_id,
        )


@dataclass(frozen=True)
class WorkspaceMemoryHost(ProviderMemoryHost):
    """Private compatibility Host for the built-in Workspace provider."""

    _compatibility_backend: Any = None

    def compatibility_backend(self) -> object | None:
        """Return the legacy backend only to the built-in adapter."""
        return self._compatibility_backend


async def close_memory_session(session: object | None) -> None:
    """Close a provider session supporting sync or async cleanup."""
    if session is None:
        return
    close = getattr(session, "close", None)
    if not callable(close):
        return
    result = close()
    if inspect.isawaitable(result):
        await result


__all__ = [
    "ProviderMemoryHost",
    "WorkspaceMemoryHost",
    "close_memory_session",
]
