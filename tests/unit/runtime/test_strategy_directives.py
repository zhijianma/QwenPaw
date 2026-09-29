# -*- coding: utf-8 -*-
"""Tests for request-scoped Task strategy compatibility directives."""

# pylint: disable=protected-access

from types import SimpleNamespace

from qwenpaw.config.config import AgentProfileConfig
from qwenpaw.modes.coding import CodingMode
from qwenpaw.runtime.builder import AgentBuilder
from qwenpaw.runtime.strategy_directives import requested_runtime_mode


def _request_context(mode: str = "coding") -> dict:
    return {
        "runtime_strategy": {
            "id": f"qwenpaw.system.tasks.{mode}-strategy",
            "parameters": {
                "mode": mode,
                "activation": "request",
            },
        },
    }


def test_coding_strategy_enables_only_the_request_config() -> None:
    original = AgentProfileConfig(id="default", name="Default")

    updated = AgentBuilder._apply_runtime_strategy(
        original,
        _request_context(),
    )

    assert original.coding_mode.enabled is False
    assert updated.coding_mode.enabled is True
    assert updated is not original


def test_coding_mode_is_active_before_agent_build() -> None:
    context = SimpleNamespace(
        request=SimpleNamespace(request_context=_request_context()),
        agent_config=None,
    )

    assert CodingMode().is_active(context) is True


def test_unknown_or_command_directive_cannot_force_request_mode() -> None:
    assert requested_runtime_mode(_request_context("goal")) is None
    assert requested_runtime_mode({"runtime_strategy": {}}) is None


def test_plugin_or_extra_parameters_cannot_activate_system_mode() -> None:
    plugin = _request_context()
    plugin["runtime_strategy"]["id"] = "plugin.coding-strategy"
    injected = _request_context()
    injected["runtime_strategy"]["parameters"]["hidden"] = True

    assert requested_runtime_mode(plugin) is None
    assert requested_runtime_mode(injected) is None
