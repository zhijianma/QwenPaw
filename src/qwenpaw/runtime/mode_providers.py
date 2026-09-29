# -*- coding: utf-8 -*-
"""Runtime adapters for provider-neutral Agent Mode contracts."""

from __future__ import annotations

import inspect
import logging
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderAgentModeHost:
    """Expose only validated configuration to a plugin Mode Provider."""

    provider_config: dict[str, Any] | None = None

    def config_snapshot(self) -> dict[str, Any]:
        """Return a detached invocation configuration snapshot."""
        return deepcopy(self.provider_config or {})


@dataclass(frozen=True)
class WorkspaceAgentModeHost:
    """Expose one immutable snapshot of legacy Workspace modes."""

    context: Any
    modes: tuple[Any, ...]
    provider_config: dict[str, Any] | None = None

    @classmethod
    def capture(
        cls,
        context: Any,
        provider_config: dict[str, Any] | None = None,
    ) -> "WorkspaceAgentModeHost":
        """Capture mode identities before plugin generations may change."""
        plugins = getattr(getattr(context, "workspace", None), "plugins", None)
        return cls(
            context=context,
            modes=tuple(getattr(plugins, "modes", ())),
            provider_config=provider_config,
        )

    def config_snapshot(self) -> dict[str, Any]:
        """Return a detached compatibility configuration snapshot."""
        return deepcopy(self.provider_config or {})

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
    "close_agent_mode_session",
]
