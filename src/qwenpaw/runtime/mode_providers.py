# -*- coding: utf-8 -*-
"""Runtime adapters for provider-neutral Agent Mode contracts."""

from __future__ import annotations

import inspect
import logging
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from ..kernel import AgentModeState, InvocationScope
from ..kernel.models import JsonObject
from ..kernel.ports import AgentModeStateStore


@dataclass(frozen=True)
class _BoundAgentModeState:
    """Bind an untrusted provider to one namespaced Chat state owner."""

    provider_id: str
    scope: InvocationScope
    store: AgentModeStateStore

    def _conversation_id(self) -> str:
        if self.scope.conversation_id is None:
            raise RuntimeError(
                "agent mode state requires a stable ChatSpec.id",
            )
        return self.scope.conversation_id

    def _registry_epoch_id(self) -> UUID:
        if self.scope.registry_epoch_id is None:
            raise RuntimeError(
                "agent mode state requires a pinned registry epoch",
            )
        return self.scope.registry_epoch_id

    async def read(self, state_key: str) -> AgentModeState | None:
        return await self.store.read(
            provider_id=self.provider_id,
            agent_id=self.scope.agent_id,
            conversation_id=self._conversation_id(),
            state_key=state_key,
        )

    async def write(
        self,
        value: JsonObject,
        *,
        expected_revision: int,
        state_key: str,
        state_schema_version: int,
    ) -> AgentModeState:
        state = AgentModeState(
            provider_id=self.provider_id,
            agent_id=self.scope.agent_id,
            conversation_id=self._conversation_id(),
            state_key=state_key,
            value=value,
            state_schema_version=state_schema_version,
            revision=expected_revision,
            writer_registry_epoch_id=self._registry_epoch_id(),
            writer_generation=self.scope.registry_generation,
        )
        return await self.store.write(
            state,
            expected_revision=expected_revision,
        )

    async def clear(
        self,
        *,
        expected_revision: int,
        state_key: str,
    ) -> AgentModeState:
        current = await self.read(state_key)
        state_schema_version = (
            current.state_schema_version if current is not None else 1
        )
        return await self.write(
            {},
            expected_revision=expected_revision,
            state_key=state_key,
            state_schema_version=state_schema_version,
        )


def bind_agent_mode_state(
    provider_id: str,
    scope: InvocationScope,
) -> _BoundAgentModeState:
    """Create one Host-only state binding for a pinned Invocation."""
    from .mode_state import lite_agent_mode_state_store

    return _BoundAgentModeState(
        provider_id=provider_id,
        scope=scope,
        store=lite_agent_mode_state_store(scope.workspace_dir),
    )


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderAgentModeHost:
    """Expose only validated configuration to a plugin Mode Provider."""

    provider_config: dict[str, Any] | None = None
    _state: _BoundAgentModeState | None = None

    def config_snapshot(self) -> dict[str, Any]:
        """Return a detached invocation configuration snapshot."""
        return deepcopy(self.provider_config or {})

    async def read_state(
        self,
        state_key: str = "default",
    ) -> AgentModeState | None:
        """Read only state owned by this provider and ChatSpec."""
        if self._state is None:
            raise RuntimeError("agent mode state Host is not bound")
        return await self._state.read(state_key)

    async def write_state(
        self,
        value: JsonObject,
        *,
        expected_revision: int,
        state_key: str = "default",
        state_schema_version: int = 1,
    ) -> AgentModeState:
        """CAS-write only state owned by this provider and ChatSpec."""
        if self._state is None:
            raise RuntimeError("agent mode state Host is not bound")
        return await self._state.write(
            value,
            expected_revision=expected_revision,
            state_key=state_key,
            state_schema_version=state_schema_version,
        )

    async def clear_state(
        self,
        *,
        expected_revision: int,
        state_key: str = "default",
    ) -> AgentModeState:
        """CAS-clear provider state without deleting its revision fence."""
        if self._state is None:
            raise RuntimeError("agent mode state Host is not bound")
        return await self._state.clear(
            expected_revision=expected_revision,
            state_key=state_key,
        )


@dataclass(frozen=True)
class WorkspaceAgentModeHost:
    """Expose one immutable snapshot of legacy Workspace modes."""

    context: Any
    modes: tuple[Any, ...]
    provider_config: dict[str, Any] | None = None
    _state: _BoundAgentModeState | None = None

    @classmethod
    def capture(
        cls,
        context: Any,
        provider_config: dict[str, Any] | None = None,
        state: _BoundAgentModeState | None = None,
    ) -> "WorkspaceAgentModeHost":
        """Capture mode identities before plugin generations may change."""
        plugins = getattr(getattr(context, "workspace", None), "plugins", None)
        return cls(
            context=context,
            modes=tuple(getattr(plugins, "modes", ())),
            provider_config=provider_config,
            _state=state,
        )

    def config_snapshot(self) -> dict[str, Any]:
        """Return a detached compatibility configuration snapshot."""
        return deepcopy(self.provider_config or {})

    async def read_state(
        self,
        state_key: str = "default",
    ) -> AgentModeState | None:
        """Read state owned by the built-in Mode Provider namespace."""
        if self._state is None:
            raise RuntimeError("agent mode state Host is not bound")
        return await self._state.read(state_key)

    async def write_state(
        self,
        value: JsonObject,
        *,
        expected_revision: int,
        state_key: str = "default",
        state_schema_version: int = 1,
    ) -> AgentModeState:
        """CAS-write built-in Mode Provider state through the same Host."""
        if self._state is None:
            raise RuntimeError("agent mode state Host is not bound")
        return await self._state.write(
            value,
            expected_revision=expected_revision,
            state_key=state_key,
            state_schema_version=state_schema_version,
        )

    async def clear_state(
        self,
        *,
        expected_revision: int,
        state_key: str = "default",
    ) -> AgentModeState:
        """CAS-clear built-in state without deleting its revision fence."""
        if self._state is None:
            raise RuntimeError("agent mode state Host is not bound")
        return await self._state.clear(
            expected_revision=expected_revision,
            state_key=state_key,
        )

    def active_mode_names(self) -> tuple[str, ...]:
        """Return active mode names from the captured snapshot."""
        return tuple(
            str(mode.name)
            for mode in self.modes
            if mode.is_active(self.context)
        )

    async def start_turn(self) -> None:
        """Prepare captured modes while isolating one broken mode."""
        for mode in self.modes:
            try:
                result = mode.on_turn_start(self.context)
                if inspect.isawaitable(result):
                    await result
            except Exception:  # pylint: disable=broad-except
                logger.warning(
                    "mode '%s' turn start raised",
                    getattr(mode, "name", "?"),
                    exc_info=True,
                )

    async def reset_conversation(self) -> None:
        """Reset captured modes while isolating one broken mode."""
        for mode in self.modes:
            try:
                result = mode.on_conversation_reset(self.context)
                if inspect.isawaitable(result):
                    await result
            except Exception:  # pylint: disable=broad-except
                logger.warning(
                    "mode '%s' reset raised",
                    getattr(mode, "name", "?"),
                    exc_info=True,
                )


async def close_agent_mode_session(session: object | None) -> None:
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
    "ProviderAgentModeHost",
    "WorkspaceAgentModeHost",
    "bind_agent_mode_state",
    "close_agent_mode_session",
]
