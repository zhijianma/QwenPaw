# -*- coding: utf-8 -*-
"""Shared behavioral contract for system and plugin Agent Mode Providers."""

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import qwenpaw.plugins.sdk as plugin_sdk

from qwenpaw.capabilities.system_modes import WorkspaceAgentModeProvider
from qwenpaw.kernel.invocation import CapabilitySelection, InvocationScope
from qwenpaw.plugins.architecture import PluginManifest
from qwenpaw.plugins.generations import GenerationRegistry
from qwenpaw.plugins.sdk import (
    AgentModeHost,
    AgentModeProvider,
    AgentModeSession,
)
from qwenpaw.runtime.assembly import RuntimeAssemblyFactory
from qwenpaw.runtime.mode_providers import (
    ProviderAgentModeHost,
    WorkspaceAgentModeHost,
    bind_agent_mode_state,
)
from qwenpaw.runtime.runtime import Runtime


class _SystemMode:
    name = "system-contract"

    def __init__(self) -> None:
        self.turn_starts = 0
        self.conversation_resets = 0

    def is_active(self, context: object) -> bool:
        del context
        return True

    async def on_turn_start(self, context: object) -> None:
        del context
        self.turn_starts += 1

    async def on_conversation_reset(self, context: object) -> None:
        del context
        self.conversation_resets += 1


def _scope(workspace_dir: Path) -> InvocationScope:
    return InvocationScope(
        agent_id="agent-contract",
        conversation_id="chat-contract",
        session_id="transport-contract",
        root_agent_id="agent-contract",
        root_session_id="transport-contract",
        workspace_dir=str(workspace_dir),
        registry_epoch_id=uuid4(),
        registry_generation=37,
    )


def _plugin_root() -> Path:
    return (
        Path(__file__).parents[3]
        / "examples"
        / "plugins"
        / "runtime-provider-kit"
    )


def test_mode_state_store_is_not_exported_to_plugins() -> None:
    assert hasattr(plugin_sdk, "AgentModeState")
    assert not hasattr(plugin_sdk, "AgentModeStateStore")


@pytest.mark.asyncio
async def test_system_and_plugin_modes_share_behavioral_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_root = _plugin_root()
    monkeypatch.syspath_prepend(str(plugin_root))
    mode_module = importlib.import_module("runtime_provider_kit.mode")
    system_mode = _SystemMode()
    system_context = SimpleNamespace(
        workspace=SimpleNamespace(
            plugins=SimpleNamespace(modes=[system_mode]),
        ),
    )
    scope = _scope(tmp_path)
    plugin_host = ProviderAgentModeHost(
        {"mode_name": "plugin-contract"},
        bind_agent_mode_state(
            "runtime-provider-kit.review-mode",
            scope,
        ),
    )
    providers_and_hosts = (
        (
            WorkspaceAgentModeProvider(),
            WorkspaceAgentModeHost.capture(system_context),
            "system-contract",
        ),
        (mode_module.create_provider(), plugin_host, "plugin-contract"),
    )

    for provider, host, expected_name in providers_and_hosts:
        assert isinstance(provider, AgentModeProvider)
        assert isinstance(host, AgentModeHost)
        assert await provider.health_check() is True
        session = await provider.open(scope, host)
        assert isinstance(session, AgentModeSession)
        assert tuple(session.active_mode_names()) == (expected_name,)
        await session.start_turn()
        await session.reset_conversation()
        await session.close()

    assert system_mode.turn_starts == 1
    assert system_mode.conversation_resets == 1
    assert not hasattr(plugin_host, "context")
    assert not hasattr(plugin_host, "modes")
    assert not hasattr(plugin_host, "active_mode_names")
    assert not hasattr(plugin_host, "start_turn")
    assert not hasattr(plugin_host, "reset_conversation")
    assert not hasattr(plugin_host, "state")


@pytest.mark.asyncio
async def test_plugin_mode_flows_through_runtime_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_root = _plugin_root()
    monkeypatch.syspath_prepend(str(plugin_root))
    mode_module = importlib.import_module("runtime_provider_kit.mode")
    manifest_data = json.loads(
        (plugin_root / "plugin.json").read_text(encoding="utf-8"),
    )
    manifest_data["contributions"] = [
        item
        for item in manifest_data["contributions"]
        if item["slot"] == "agent.mode.provider"
    ]
    registry = GenerationRegistry()
    await registry.activate(
        PluginManifest.from_dict(manifest_data),
        lambda _declaration: mode_module.create_provider(),
    )
    provider_id = "runtime-provider-kit.review-mode"
    assembly = await RuntimeAssemblyFactory(registry).open(
        agent_id="agent-contract",
        conversation_id="chat-contract",
        session_id="transport-contract",
        root_agent_id="agent-contract",
        root_session_id="transport-contract",
        workspace_dir=tmp_path,
        selection=CapabilitySelection(agent_mode_provider_id=provider_id),
    )
    workspace = SimpleNamespace(
        config=SimpleNamespace(
            capability_configs={provider_id: {"mode_name": "review-safe"}},
        ),
    )
    runtime = Runtime(workspace=workspace, app_services=None)
    context = SimpleNamespace(workspace=workspace, extras={})
    session = None

    try:
        # pylint: disable=protected-access
        session = await runtime._open_agent_mode_session(context, assembly)
        # pylint: enable=protected-access
        context.extras["agent_mode_session"] = session
        assert tuple(session.active_mode_names()) == ("review-safe",)
        await runtime._start_modes(context)  # pylint: disable=protected-access
        assert session.turn_starts == 1
        await session.reset_conversation()
        assert session.conversation_resets == 1
        persisted = await session.host.read_state("lifecycle")
        assert persisted is not None
        assert persisted.value == {}
        assert persisted.revision == 2
        assert assembly.scope.registry_generation == registry.generation
    finally:
        if session is not None:
            await session.close()
        await assembly.close()


@pytest.mark.asyncio
async def test_plugin_mode_config_fails_before_provider_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_root = _plugin_root()
    monkeypatch.syspath_prepend(str(plugin_root))
    mode_module = importlib.import_module("runtime_provider_kit.mode")
    manifest_data = json.loads(
        (plugin_root / "plugin.json").read_text(encoding="utf-8"),
    )
    manifest_data["contributions"] = [
        item
        for item in manifest_data["contributions"]
        if item["slot"] == "agent.mode.provider"
    ]
    provider = mode_module.create_provider()
    opened = False
    original_open = provider.open

    async def capture_open(scope: object, host: object):
        nonlocal opened
        opened = True
        return await original_open(scope, host)

    provider.open = capture_open
    registry = GenerationRegistry()
    await registry.activate(
        PluginManifest.from_dict(manifest_data),
        lambda _declaration: provider,
    )
    provider_id = "runtime-provider-kit.review-mode"
    assembly = await RuntimeAssemblyFactory(registry).open(
        agent_id="agent-contract",
        conversation_id="chat-contract",
        session_id="transport-contract",
        root_agent_id="agent-contract",
        root_session_id="transport-contract",
        workspace_dir=tmp_path,
        selection=CapabilitySelection(agent_mode_provider_id=provider_id),
    )
    workspace = SimpleNamespace(
        config=SimpleNamespace(
            capability_configs={provider_id: {"mode_name": ""}},
        ),
    )

    try:
        # pylint: disable=protected-access
        with pytest.raises(ValueError, match="does not match config_schema"):
            await Runtime(
                workspace=workspace,
                app_services=None,
            )._open_agent_mode_session(
                SimpleNamespace(workspace=workspace, extras={}),
                assembly,
            )
        # pylint: enable=protected-access
        assert opened is False
    finally:
        await assembly.close()
