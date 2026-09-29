# -*- coding: utf-8 -*-
"""Validated compatibility directives produced by Task strategies."""

from __future__ import annotations

from typing import Any

from ..kernel.models import RuntimeStrategyDirective

_SYSTEM_STRATEGY_PARAMETERS = {
    "qwenpaw.system.tasks.default-strategy": {"mode": "default"},
    "qwenpaw.system.tasks.coding-strategy": {
        "mode": "coding",
        "activation": "request",
    },
    "qwenpaw.system.tasks.goal-strategy": {
        "mode": "goal",
        "activation": "command",
        "command": "goal",
    },
    "qwenpaw.system.tasks.mission-strategy": {
        "mode": "mission",
        "activation": "command",
        "command": "mission",
    },
}


def console_strategy_parameters(
    directive: RuntimeStrategyDirective | None,
) -> dict[str, str] | None:
    """Return an exact built-in Console directive or no bridge."""
    if directive is None:
        return None
    expected = _SYSTEM_STRATEGY_PARAMETERS.get(directive.strategy_id)
    if expected is None:
        return None
    if directive.parameters != expected:
        raise ValueError(
            f"invalid parameters for strategy '{directive.strategy_id}'",
        )
    return dict(expected)


def console_strategy_request_context(
    directive: RuntimeStrategyDirective | None,
) -> dict[str, object]:
    """Serialize one validated system directive for the Chat adapter."""
    parameters = console_strategy_parameters(directive)
    if directive is None or parameters is None:
        return {}
    return {
        "runtime_strategy": {
            "id": directive.strategy_id,
            "parameters": parameters,
        },
    }


def requested_runtime_mode(request_context: Any) -> str | None:
    """Return a bounded request-level mode from a strategy directive."""
    if not isinstance(request_context, dict):
        return None
    raw_strategy = request_context.get("runtime_strategy")
    if not isinstance(raw_strategy, dict):
        return None
    strategy_id = raw_strategy.get("id")
    parameters = raw_strategy.get("parameters")
    if not isinstance(strategy_id, str) or not isinstance(parameters, dict):
        return None
    try:
        directive = RuntimeStrategyDirective(
            strategy_id=strategy_id,
            parameters=parameters,
        )
        validated = console_strategy_parameters(directive)
    except (TypeError, ValueError):
        return None
    if validated is None or validated.get("activation") != "request":
        return None
    return validated.get("mode")


__all__ = [
    "console_strategy_parameters",
    "console_strategy_request_context",
    "requested_runtime_mode",
]
