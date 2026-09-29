# -*- coding: utf-8 -*-
"""Shared behavioral contract for system and plugin Stop Gate Providers."""

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from qwenpaw.capabilities.system_stop_gates import WorkspaceStopGateProvider
from qwenpaw.kernel.invocation import (
    DEFAULT_STOP_GATE_PROVIDER_ID,
    InvocationScope,
)
from qwenpaw.loop.gates.runner import apply_stop_result, check_pending_gates
from qwenpaw.plugins.architecture import PluginManifest
from qwenpaw.plugins.generations import GenerationRegistry
from qwenpaw.plugins.sdk import (
    StopGateAction,
    StopGateDecision,
    StopGateDefinition,
    StopGateInput,
    StopGateMessage,
    StopGateProvider,
    StopGateSession,
)
from qwenpaw.runtime.assembly import RuntimeAssemblyFactory
from qwenpaw.runtime.runtime import Runtime
from qwenpaw.runtime.stop_gate_providers import (
    StopGateRouterSession,
    decision_to_legacy_result,
)


class _SystemHost:
    def list_gates(self) -> tuple[StopGateDefinition, ...]:
        return (
            StopGateDefinition(
                gate_id=(f"{DEFAULT_STOP_GATE_PROVIDER_ID}.system-observer"),
                provider_id=DEFAULT_STOP_GATE_PROVIDER_ID,
                priority=100,
            ),
        )

    def is_active(self, gate_id: str) -> bool:
        del gate_id
        return True

    async def evaluate(
        self,
        gate_id: str,
        gate_input: StopGateInput,
    ) -> StopGateDecision:
        del gate_id, gate_input
        return StopGateDecision(action=StopGateAction.BYPASS)


def _scope(workspace_dir: Path) -> InvocationScope:
    return InvocationScope(
        agent_id="default",
        conversation_id="chat-contract",
        session_id="transport-contract",
        root_agent_id="default",
        root_session_id="transport-contract",
        workspace_dir=str(workspace_dir),
        registry_generation=17,
    )


@pytest.mark.asyncio
async def test_system_and_plugin_stop_gates_share_behavioral_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_root = (
        Path(__file__).parents[3]
        / "examples"
        / "plugins"
        / "runtime-provider-kit"
    )
    monkeypatch.syspath_prepend(str(plugin_root))
    gate_module = importlib.import_module("runtime_provider_kit.stop_gate")
    providers_and_hosts = (
        (WorkspaceStopGateProvider(), _SystemHost()),
        (gate_module.create_provider(), SimpleNamespace()),
    )
    sessions = []

    for provider, host in providers_and_hosts:
        assert isinstance(provider, StopGateProvider)
        assert await provider.health_check() is True
        session = await provider.open(_scope(tmp_path), host)
        assert isinstance(session, StopGateSession)
        assert session.provider_id == provider.provider_id
        first = tuple(session.list_gates())
        assert first == tuple(session.list_gates())
        assert first
        assert all(item.provider_id == provider.provider_id for item in first)
        assert all(
            item.gate_id.startswith(f"{provider.provider_id}.")
            for item in first
        )
        sessions.append(session)

    router = StopGateRouterSession(sessions)
    await router.start_turn()
    decision = await router.evaluate(
        StopGateInput(
            iteration=1,
            has_tool_calls=False,
            final_message=StopGateMessage(text="draft"),
        ),
    )
    completed = await router.evaluate(
        StopGateInput(
            iteration=2,
            has_tool_calls=False,
            final_message=StopGateMessage(text="reviewed"),
        ),
    )

    assert decision.action == StopGateAction.INTERRUPT_AND_CONTINUE
    assert decision.inject_on_tool_call is True
    assert completed.action == StopGateAction.TERMINATE
    assert completed.final_message is not None
    assert completed.final_message.text == "reviewed"
    await router.reset_conversation()
    reset_decision = await router.evaluate(
        StopGateInput(iteration=1, has_tool_calls=True),
    )
    assert reset_decision.action == StopGateAction.INTERRUPT_AND_CONTINUE
    await router.close()


@pytest.mark.asyncio
async def test_plugin_gate_uses_runtime_and_tool_completion_safe_point(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_root = (
        Path(__file__).parents[3]
        / "examples"
        / "plugins"
        / "runtime-provider-kit"
    )
    monkeypatch.syspath_prepend(str(plugin_root))
    gate_module = importlib.import_module("runtime_provider_kit.stop_gate")
    manifest_data = json.loads(
        (plugin_root / "plugin.json").read_text(encoding="utf-8"),
    )
    manifest_data["contributions"] = [
        item
        for item in manifest_data["contributions"]
        if item["slot"] == "loop.gate.provider"
    ]
    registry = GenerationRegistry()
    await registry.activate(
        PluginManifest.from_dict(manifest_data),
        lambda _declaration: gate_module.create_provider(),
    )
    assembly = await RuntimeAssemblyFactory(registry).open(
        agent_id="default",
        conversation_id="chat-contract",
        session_id="transport-contract",
        root_agent_id="default",
        root_session_id="transport-contract",
        workspace_dir=tmp_path,
    )
    context = SimpleNamespace(
        workspace=SimpleNamespace(
            plugins=SimpleNamespace(stop_handlers=()),
        ),
        agent=SimpleNamespace(),
    )
    runtime = Runtime(workspace=SimpleNamespace(), app_services=None)

    # pylint: disable=protected-access
    router = await runtime._open_stop_gate_session(context, assembly)
    # pylint: enable=protected-access
    await router.start_turn()
    decision = await router.evaluate(
        StopGateInput(iteration=1, has_tool_calls=True),
    )
    fake_agent = SimpleNamespace(
        _gate_pending_continue=None,
        _gate_pending_stop=None,
        state=SimpleNamespace(context=[]),
    )

    apply_stop_result(
        fake_agent,
        decision_to_legacy_result(decision),
        is_tool_call=True,
    )
    assert fake_agent.state.context == []
    assert getattr(fake_agent, "_gate_pending_continue", None) is not None
    assert check_pending_gates(fake_agent) is None
    assert fake_agent.state.context[0].content[0].text == (
        "Review the project answer once more."
    )
    assert assembly.scope.registry_generation == registry.generation
    await router.close()
    await assembly.close()
