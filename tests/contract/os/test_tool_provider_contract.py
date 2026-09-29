# -*- coding: utf-8 -*-
"""Shared behavioral contract for system and plugin Tool Providers."""

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from qwenpaw.capabilities.system_tools import WorkspaceToolProvider
from qwenpaw.governance.tool_registry import DEFAULT_REGISTRY
from qwenpaw.kernel.invocation import InvocationScope
from qwenpaw.plugins.architecture import PluginManifest
from qwenpaw.plugins.generations import GenerationRegistry
from qwenpaw.plugins.sdk import (
    ToolDefinition,
    ToolHost,
    ToolProvider,
    ToolSelection,
)
from qwenpaw.runtime.assembly import RuntimeAssemblyFactory
from qwenpaw.runtime.builder import AgentBuilder


async def _system_contract_tool() -> str:
    return "system"


class _SystemHost:
    def __init__(self) -> None:
        self.selection: ToolSelection | None = None

    def config_snapshot(self) -> dict[str, Any]:
        return {}

    def credential(self, alias: str) -> None:
        del alias

    def interaction_broker(self) -> None:
        """Expose no broker in the direct contract fixture."""

    async def list_workspace_tools(
        self,
        selection: ToolSelection,
    ) -> tuple[Any, ...]:
        self.selection = selection
        return (_system_contract_tool,)


class _PluginHost:
    def config_snapshot(self) -> dict[str, Any]:
        return {"description": "Contract plugin"}

    def credential(self, alias: str) -> None:
        del alias

    def interaction_broker(self) -> None:
        """Expose no broker in the direct contract fixture."""


class _LocalWorkspace:
    def __init__(self) -> None:
        self.selection: dict[str, Any] = {}
        self.governor: object | None = None

    def set_governor(self, governor: object) -> None:
        self.governor = governor

    async def list_tools(self, **kwargs: Any) -> tuple[Any, ...]:
        self.selection = kwargs
        return (_system_contract_tool,)


def _scope(workspace_dir: Path) -> InvocationScope:
    return InvocationScope(
        agent_id="default",
        conversation_id="chat-contract",
        session_id="transport-contract",
        root_agent_id="default",
        root_session_id="transport-contract",
        workspace_dir=str(workspace_dir),
        registry_generation=19,
    )


@pytest.mark.asyncio
async def test_system_and_plugin_tools_share_behavioral_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_root = (
        Path(__file__).parents[3]
        / "examples"
        / "plugins"
        / "chat-tool-provider"
    )
    monkeypatch.syspath_prepend(str(plugin_root))
    provider_module = importlib.import_module("chat_tool_provider.provider")
    system_host = _SystemHost()
    providers_and_hosts = (
        (WorkspaceToolProvider(), system_host),
        (provider_module.create_provider(), _PluginHost()),
    )
    selection = ToolSelection(
        active_modes=("coding",),
        active_skills=("repo-reader",),
        enabled_features=("browser",),
    )

    for provider, host in providers_and_hosts:
        assert isinstance(provider, ToolProvider)
        assert isinstance(host, ToolHost)
        assert await provider.health_check() is True
        tools = tuple(
            await provider.list_tools(
                _scope(tmp_path),
                selection,
                host,
            ),
        )
        assert tools
        assert all(
            isinstance(tool, ToolDefinition) or callable(tool)
            for tool in tools
        )

    assert system_host.selection == selection


@pytest.mark.asyncio
async def test_plugin_tool_flows_through_pinned_governed_builder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_root = (
        Path(__file__).parents[3]
        / "examples"
        / "plugins"
        / "chat-tool-provider"
    )
    monkeypatch.syspath_prepend(str(plugin_root))
    provider_module = importlib.import_module("chat_tool_provider.provider")
    manifest = PluginManifest.from_dict(
        json.loads((plugin_root / "plugin.json").read_text(encoding="utf-8")),
    )
    registry = GenerationRegistry()
    await registry.activate(
        manifest,
        lambda _declaration: provider_module.create_provider(),
    )
    assembly = await RuntimeAssemblyFactory(registry).open(
        agent_id="default",
        conversation_id="chat-contract",
        session_id="transport-contract",
        root_agent_id="default",
        root_session_id="transport-contract",
        workspace_dir=tmp_path,
    )
    provider_id = "chat-tool-provider.workspace-info"
    local_workspace = _LocalWorkspace()
    context = SimpleNamespace(
        invocation_scope=assembly.scope,
        extras={"runtime_assembly": assembly},
        workspace=SimpleNamespace(
            config=SimpleNamespace(
                capability_configs={
                    provider_id: {"description": "Pinned plugin"},
                },
                capability_credential_refs={},
            ),
            driver_manager=None,
        ),
    )
    builder = AgentBuilder()

    try:
        # pylint: disable=protected-access
        providers = builder._resolve_tool_providers(context)
        assert providers is not None
        tools = await builder._collect_provider_tools(
            tool_providers=providers,
            ctx=context,
            local_workspace=local_workspace,
            agent_config=SimpleNamespace(),
            request_context={
                "agent_id": "default",
                "os_invocation_id": str(assembly.scope.invocation_id),
            },
            governor=object(),
            active_modes=("coding",),
            active_skills=("repo-reader",),
            enabled_features=("browser",),
        )
        # pylint: enable=protected-access

        by_name = {tool.name: tool for tool in tools}
        assert "_system_contract_tool" in by_name
        assert "describe_qwenpaw_invocation" in by_name
        assert all(
            callable(getattr(tool, "check_permissions", None))
            for tool in tools
        )
        assert (
            DEFAULT_REGISTRY.get_owner(
                "describe_qwenpaw_invocation",
            )
            == provider_id
        )
        assert local_workspace.selection["active_modes"] == {"coding"}
        assert assembly.scope.registry_generation == registry.generation
    finally:
        DEFAULT_REGISTRY.unregister_owner(provider_id)
        await assembly.close()
