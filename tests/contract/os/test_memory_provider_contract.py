# -*- coding: utf-8 -*-
"""Shared behavioral contract for system and plugin Memory Providers."""

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from qwenpaw.capabilities.system_memory import WorkspaceMemoryProvider
from qwenpaw.governance.tool_registry import DEFAULT_REGISTRY
from qwenpaw.kernel.invocation import CapabilitySelection, InvocationScope
from qwenpaw.kernel.memory import MemoryStateScope
from qwenpaw.plugins.architecture import PluginManifest
from qwenpaw.plugins.generations import GenerationRegistry
from qwenpaw.plugins.sdk import MemoryHost, MemoryProvider, MemorySession
from qwenpaw.runtime.assembly import RuntimeAssemblyFactory
from qwenpaw.runtime.builder import AgentBuilder
from qwenpaw.runtime.memory_providers import (
    ProviderMemoryHost,
    WorkspaceMemoryHost,
)
from qwenpaw.runtime.prompt_contributors import MemoryGuidanceContributor


class _Backend:
    def get_memory_prompt(self) -> str:
        return "System memory guidance."

    def list_memory_tools(self) -> tuple[Any, ...]:
        return ()


def _scope(workspace_dir: Path) -> InvocationScope:
    return InvocationScope(
        agent_id="default",
        conversation_id="chat-contract",
        session_id="transport-contract",
        root_agent_id="default",
        root_session_id="transport-contract",
        workspace_dir=str(workspace_dir),
        registry_generation=23,
    )


@pytest.mark.asyncio
async def test_system_and_plugin_memory_share_behavioral_contract(
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
    memory_module = importlib.import_module("runtime_provider_kit.memory")
    scope = _scope(tmp_path / "contract-project")
    providers_and_hosts = (
        (
            WorkspaceMemoryProvider(),
            WorkspaceMemoryHost(
                _Backend(),
                provider_id="qwenpaw.system.memory.workspace-memory",
                agent_id=scope.agent_id,
                conversation_id=scope.conversation_id,
                workspace_dir=scope.workspace_dir,
            ),
        ),
        (
            memory_module.create_provider(),
            ProviderMemoryHost(
                provider_id="runtime-provider-kit.project-memory",
                agent_id=scope.agent_id,
                conversation_id=scope.conversation_id,
                workspace_dir=scope.workspace_dir,
                provider_config={"prompt_prefix": "Remember"},
            ),
        ),
    )

    for provider, host in providers_and_hosts:
        assert isinstance(provider, MemoryProvider)
        assert isinstance(host, MemoryHost)
        assert await provider.health_check() is True
        session = await provider.open(scope, host)
        assert isinstance(session, MemorySession)
        assert isinstance(session.get_prompt(), str)
        assert tuple(session.list_tools()) == tuple(session.list_tools())
        await session.close()

    plugin_host = providers_and_hosts[1][1]
    assert not hasattr(plugin_host, "compatibility_backend")
    agent_store = plugin_host.state(MemoryStateScope.AGENT)
    conversation_store = plugin_host.state(MemoryStateScope.CONVERSATION)
    agent_snapshot = await agent_store.write(
        "contract",
        "agent",
        expected_revision=0,
    )
    conversation_snapshot = await conversation_store.write(
        "contract",
        "conversation",
        expected_revision=0,
    )
    assert agent_snapshot.owner_id == scope.agent_id
    assert conversation_snapshot.owner_id == scope.conversation_id


@pytest.mark.asyncio
async def test_plugin_memory_flows_through_builder_prompt_and_tool_guard(
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
    memory_module = importlib.import_module("runtime_provider_kit.memory")
    manifest_data = json.loads(
        (plugin_root / "plugin.json").read_text(encoding="utf-8"),
    )
    manifest_data["contributions"] = [
        item
        for item in manifest_data["contributions"]
        if item["slot"] == "memory.provider"
    ]
    registry = GenerationRegistry()
    await registry.activate(
        PluginManifest.from_dict(manifest_data),
        lambda _declaration: memory_module.create_provider(),
    )
    assembly = await RuntimeAssemblyFactory(registry).open(
        agent_id="agent-contract",
        conversation_id="chat-contract",
        session_id="transport-contract",
        root_agent_id="agent-contract",
        root_session_id="transport-contract",
        workspace_dir=tmp_path / "contract-project",
        selection=CapabilitySelection(
            memory_provider_id="runtime-provider-kit.project-memory",
        ),
    )
    provider_id = "runtime-provider-kit.project-memory"
    context = SimpleNamespace(
        extras={"runtime_assembly": assembly},
        invocation_scope=assembly.scope,
        workspace=SimpleNamespace(
            memory_manager=object(),
            local_workspace=None,
            config=SimpleNamespace(
                capability_configs={
                    provider_id: {"prompt_prefix": "Pinned memory for"},
                },
            ),
        ),
    )
    builder = AgentBuilder()

    try:
        # pylint: disable=protected-access
        session = await builder._open_memory_session(context)
        # pylint: enable=protected-access
        context.extras["memory_session"] = session
        assert session.get_prompt() == "Pinned memory for contract-project."
        assert MemoryGuidanceContributor().contribute_sync(context) == (
            "Pinned memory for contract-project."
        )

        memory_tool = session.list_tools()[0]
        assert await memory_tool.function("renamed-project") == (
            "renamed-project"
        )
        assert session.get_prompt() == "Pinned memory for renamed-project."

        toolkit = await builder.build_toolkit(
            SimpleNamespace(),
            agent_id="agent-contract",
            request_context={
                "agent_id": "agent-contract",
                "os_invocation_id": str(assembly.scope.invocation_id),
            },
            tool_providers=(),
            memory_tools=session.list_tools(),
            memory_provider_id=provider_id,
            governor=object(),
            ctx=context,
            workspace_dir=assembly.scope.workspace_dir,
        )
        guarded = await toolkit.get_tool("remember_project_label")
        assert guarded is not None
        assert callable(getattr(guarded, "check_permissions", None))
        assert DEFAULT_REGISTRY.get_owner("remember_project_label") == (
            provider_id
        )
        assert assembly.scope.registry_generation == registry.generation
    finally:
        DEFAULT_REGISTRY.unregister_owner(provider_id)
        session_value = context.extras.get("memory_session")
        if session_value is not None:
            await session_value.close()
        await assembly.close()
