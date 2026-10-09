# -*- coding: utf-8 -*-
"""Shared behavioral contract for system and plugin Prompt Providers."""

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from qwenpaw.capabilities.system_prompts import WorkspacePromptProvider
from qwenpaw.kernel.invocation import InvocationScope
from qwenpaw.plugins.architecture import PluginManifest
from qwenpaw.plugins.generations import GenerationRegistry
from qwenpaw.plugins.sdk import PromptFragment, PromptProvider
from qwenpaw.runtime.assembly import RuntimeAssemblyFactory
from qwenpaw.runtime.builder import AgentBuilder


class _PromptHost:
    async def build_workspace_prompt(self) -> str:
        return "System workspace guidance"


def _scope(workspace_dir: Path) -> InvocationScope:
    return InvocationScope(
        agent_id="default",
        chat_id="chat-contract",
        session_id="transport-contract",
        root_agent_id="default",
        root_session_id="transport-contract",
        workspace_dir=str(workspace_dir),
        registry_generation=7,
    )


@pytest.mark.asyncio
async def test_system_and_plugin_prompt_providers_share_behavioral_contract(
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
    prompt_module = importlib.import_module("runtime_provider_kit.prompt")
    scope = _scope(tmp_path / "contract-project")
    host = _PromptHost()
    providers = (
        WorkspacePromptProvider(),
        prompt_module.create_provider(),
    )

    for provider in providers:
        assert isinstance(provider, PromptProvider)
        assert await provider.health_check() is True
        first = tuple(await provider.list_fragments(scope, host))
        replay = tuple(await provider.list_fragments(scope, host))
        assert first == replay
        assert first
        assert all(isinstance(item, PromptFragment) for item in first)
        assert all(item.content.strip() for item in first)
        assert all(
            item.fragment_id.startswith(f"{provider.provider_id}.")
            for item in first
        )

    assert scope.chat_id == "chat-contract"
    assert (
        "System workspace guidance"
        in (await providers[0].list_fragments(scope, host))[0].content
    )
    assert (
        "contract-project"
        in (await providers[1].list_fragments(scope, host))[0].content
    )


@pytest.mark.asyncio
async def test_plugin_prompt_flows_through_pinned_runtime_assembly(
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
    prompt_module = importlib.import_module("runtime_provider_kit.prompt")
    manifest_data = json.loads(
        (plugin_root / "plugin.json").read_text(encoding="utf-8"),
    )
    manifest_data["contributions"] = [
        item
        for item in manifest_data["contributions"]
        if item["slot"] == "prompt.provider"
    ]
    registry = GenerationRegistry()
    await registry.activate(
        PluginManifest.from_dict(manifest_data),
        lambda _declaration: prompt_module.create_provider(),
    )
    workspace_dir = tmp_path / "contract-project"
    workspace_dir.mkdir()
    assembly = await RuntimeAssemblyFactory(registry).open(
        agent_id="default",
        chat_id="chat-contract",
        session_id="transport-contract",
        root_agent_id="default",
        root_session_id="transport-contract",
        workspace_dir=workspace_dir,
    )
    builder = AgentBuilder()
    monkeypatch.setattr(
        builder,
        "build_prompt",
        lambda _context, _config: "System workspace guidance",
    )
    context = SimpleNamespace(
        invocation_scope=assembly.scope,
        extras={"runtime_assembly": assembly},
    )

    # pylint: disable=protected-access
    prompt = await builder._build_prompt_from_providers(
        context,
        SimpleNamespace(),
    )
    # pylint: enable=protected-access

    assert assembly.scope.registry_generation == registry.generation
    assert prompt == (
        "System workspace guidance\n\n"
        "Keep changes scoped to the contract-project project."
    )
    await assembly.close()
