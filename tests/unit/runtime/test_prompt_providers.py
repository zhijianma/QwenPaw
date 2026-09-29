# -*- coding: utf-8 -*-
"""Tests for task-independent Prompt Provider assembly."""

from types import SimpleNamespace

import pytest

from qwenpaw.capabilities import GenerationRegistry
from qwenpaw.capabilities.system_prompts import WorkspacePromptProvider
from qwenpaw.kernel.invocation import CapabilitySelection, InvocationScope
from qwenpaw.kernel.models import (
    CapabilityBundle,
    CapabilityContribution,
    CapabilityProviderKind,
    PromptFragment,
)
from qwenpaw.runtime.assembly import RuntimeAssemblyFactory
from qwenpaw.runtime.builder import AgentBuilder


def _scope(*provider_ids: str) -> InvocationScope:
    return InvocationScope(
        agent_id="default",
        session_id="chat",
        root_agent_id="default",
        root_session_id="chat",
        workspace_dir="/tmp/qwenpaw-workspace",
        registry_generation=6,
        selection=CapabilitySelection(prompt_provider_ids=provider_ids),
    )


class _PromptHost:
    async def build_workspace_prompt(self) -> str:
        return "workspace prompt"


@pytest.mark.asyncio
async def test_workspace_prompt_provider_adapts_existing_prompt_manager() -> (
    None
):
    fragments = await WorkspacePromptProvider().list_fragments(
        _scope("qwenpaw.system.prompts.workspace-prompt"),
        _PromptHost(),
    )

    assert len(fragments) == 1
    assert fragments[0].content == "workspace prompt"
    assert fragments[0].priority == 0


class _PromptProvider:
    def __init__(self, provider_id: str, content: str, priority: int) -> None:
        self.provider_id = provider_id
        self.content = content
        self.priority = priority

    async def list_fragments(
        self,
        scope: InvocationScope,
        host: object,
    ) -> tuple[PromptFragment, ...]:
        del scope, host
        return (
            PromptFragment(
                fragment_id=f"{self.provider_id}.main",
                content=self.content,
                priority=self.priority,
            ),
        )


class _Assembly:
    def __init__(self, providers: dict[str, object]) -> None:
        self.providers = providers

    def require(self, capability_id: str, slot: str) -> object:
        assert slot == "prompt.provider"
        return self.providers[capability_id]


@pytest.mark.asyncio
async def test_builder_orders_pinned_prompt_fragments() -> None:
    low = _PromptProvider("example.low", "low priority", 200)
    high = _PromptProvider("example.high", "high priority", 10)
    scope = _scope("example.low", "example.high")
    context = SimpleNamespace(
        invocation_scope=scope,
        extras={
            "runtime_assembly": _Assembly(
                {"example.low": low, "example.high": high},
            ),
        },
    )

    # pylint: disable=protected-access
    prompt = await AgentBuilder()._build_prompt_from_providers(
        context,
        SimpleNamespace(),
    )
    # pylint: enable=protected-access

    assert prompt == "high priority\n\nlow priority"


@pytest.mark.asyncio
async def test_builder_rejects_prompt_fragment_owned_by_another_provider() -> (
    None
):
    provider = _PromptProvider("example.owner", "unsafe", 10)

    async def wrong_fragments(
        scope: InvocationScope,
        host: object,
    ) -> tuple[PromptFragment, ...]:
        del scope, host
        return (
            PromptFragment(
                fragment_id="another.provider.main",
                content="unsafe",
            ),
        )

    provider.list_fragments = wrong_fragments  # type: ignore[method-assign]
    context = SimpleNamespace(
        invocation_scope=_scope("example.owner"),
        extras={
            "runtime_assembly": _Assembly({"example.owner": provider}),
        },
    )

    with pytest.raises(ValueError, match="is not owned by provider"):
        # pylint: disable=protected-access
        await AgentBuilder()._build_prompt_from_providers(
            context,
            SimpleNamespace(),
        )
        # pylint: enable=protected-access


class _PluginPromptProvider:
    provider_id = "example.prompts.provider"

    def __init__(self, version: str) -> None:
        self.version = version

    async def health_check(self) -> bool:
        return True

    async def list_fragments(self, scope, host):
        del scope, host
        return ()


def _plugin_prompt_bundle(version: str) -> CapabilityBundle:
    return CapabilityBundle(
        provider_id="example.prompts",
        provider_kind=CapabilityProviderKind.PLUGIN,
        version=version,
        contributions=(
            CapabilityContribution(
                contribution_id="provider",
                slot="prompt.provider",
                entrypoint="example:prompt_provider",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_plugin_prompt_replacement_keeps_old_invocation() -> None:
    registry = GenerationRegistry()
    await registry.activate_bundle(
        _plugin_prompt_bundle("1.0.0"),
        lambda _: _PluginPromptProvider("1.0.0"),
    )
    selection = CapabilitySelection(
        prompt_provider_ids=("example.prompts.provider",),
    )
    old = await RuntimeAssemblyFactory(registry).open(
        agent_id="default",
        session_id="old",
        root_agent_id="default",
        root_session_id="old",
        workspace_dir="/tmp/qwenpaw-workspace",
        selection=selection,
    )
    await registry.activate_bundle(
        _plugin_prompt_bundle("2.0.0"),
        lambda _: _PluginPromptProvider("2.0.0"),
    )
    new = await RuntimeAssemblyFactory(registry).open(
        agent_id="default",
        session_id="new",
        root_agent_id="default",
        root_session_id="new",
        workspace_dir="/tmp/qwenpaw-workspace",
        selection=selection,
    )

    old_provider = old.require(
        "example.prompts.provider",
        "prompt.provider",
    )
    new_provider = new.require(
        "example.prompts.provider",
        "prompt.provider",
    )
    assert old_provider.version == "1.0.0"
    assert new_provider.version == "2.0.0"
    await old.close()
    await new.close()


@pytest.mark.asyncio
async def test_new_invocation_auto_selects_installed_prompt_provider() -> None:
    registry = GenerationRegistry()
    await registry.activate_bundle(
        _plugin_prompt_bundle("1.0.0"),
        lambda _: _PluginPromptProvider("1.0.0"),
    )

    assembly = await RuntimeAssemblyFactory(registry).open(
        agent_id="default",
        session_id="chat",
        root_agent_id="default",
        root_session_id="chat",
        workspace_dir="/tmp/qwenpaw-workspace",
    )

    assert assembly.scope.selection.prompt_provider_ids == (
        "example.prompts.provider",
        "qwenpaw.system.prompts.workspace-prompt",
    )
    await assembly.close()
