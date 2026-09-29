# -*- coding: utf-8 -*-
"""Tests for task-independent Memory Provider assembly and adapters."""

from types import SimpleNamespace
from typing import Any

import pytest

from qwenpaw.capabilities import GenerationRegistry
from qwenpaw.capabilities.system_memory import WorkspaceMemoryProvider
from qwenpaw.kernel.invocation import CapabilitySelection, InvocationScope
from qwenpaw.kernel.memory import (
    MemoryStateScope,
    MemoryStateUnavailableError,
)
from qwenpaw.kernel.models import (
    CapabilityBundle,
    CapabilityContribution,
    CapabilityProviderKind,
)
from qwenpaw.plugins.loader import PluginLoader
from qwenpaw.runtime.assembly import RuntimeAssemblyFactory
from qwenpaw.runtime.builder import AgentBuilder
from qwenpaw.runtime.memory_providers import (
    ProviderMemoryHost,
    WorkspaceMemoryHost,
)


def _memory_tool() -> None:
    """Represent one backend-owned memory tool."""


class _Backend:
    def __init__(self) -> None:
        self.closed = False

    def get_memory_prompt(self) -> str:
        return "Remember stable user preferences."

    def list_memory_tools(self) -> list[Any]:
        return [_memory_tool]

    def build_middlewares(self) -> list[str]:
        return ["memory-middleware"]

    async def close(self) -> bool:
        self.closed = True
        return True


def _scope() -> InvocationScope:
    return InvocationScope(
        agent_id="default",
        session_id="chat",
        root_agent_id="default",
        root_session_id="chat",
        workspace_dir="/tmp/qwenpaw-workspace",
        registry_generation=4,
    )


@pytest.mark.asyncio
async def test_workspace_memory_session_delegates_without_owning_backend() -> (
    None
):
    backend = _Backend()
    session = await WorkspaceMemoryProvider().open(
        _scope(),
        WorkspaceMemoryHost(backend),
    )

    assert session.get_prompt() == "Remember stable user preferences."
    assert session.list_tools() == (_memory_tool,)
    assert session.build_middlewares() == ["memory-middleware"]
    await session.close()
    assert backend.closed is False


@pytest.mark.asyncio
async def test_builder_opens_memory_from_pinned_assembly() -> None:
    registry = GenerationRegistry()
    assembly = await RuntimeAssemblyFactory(registry).open(
        agent_id="default",
        session_id="chat",
        root_agent_id="default",
        root_session_id="chat",
        workspace_dir="/tmp/qwenpaw-workspace",
    )
    backend = _Backend()
    context = SimpleNamespace(
        extras={"runtime_assembly": assembly},
        invocation_scope=assembly.scope,
        workspace=SimpleNamespace(memory_manager=backend),
    )

    # pylint: disable=protected-access
    session = await AgentBuilder()._open_memory_session(context)
    # pylint: enable=protected-access

    assert session.get_prompt() == "Remember stable user preferences."
    await assembly.close()


@pytest.mark.asyncio
async def test_memory_host_scopes_config_and_state_to_chat_spec(
    tmp_path,
) -> None:
    host = WorkspaceMemoryHost(
        None,
        provider_id="example.memory.provider",
        agent_id="agent-1",
        conversation_id="chat-spec-1",
        workspace_dir=tmp_path,
        provider_config={"prompt_prefix": "Project"},
    )

    config = host.config_snapshot()
    config["prompt_prefix"] = "mutated"
    assert host.config_snapshot() == {"prompt_prefix": "Project"}

    agent_state = await host.state(MemoryStateScope.AGENT).write(
        "label",
        "agent",
        expected_revision=0,
    )
    chat_state = await host.state(MemoryStateScope.CONVERSATION).write(
        "label",
        "chat",
        expected_revision=0,
    )
    assert agent_state.owner_id == "agent-1"
    assert chat_state.owner_id == "chat-spec-1"

    without_chat = WorkspaceMemoryHost(
        None,
        provider_id="example.memory.provider",
        agent_id="agent-1",
        workspace_dir=tmp_path,
    )
    with pytest.raises(MemoryStateUnavailableError, match="ChatSpec.id"):
        without_chat.state(MemoryStateScope.CONVERSATION)


class _PluginMemorySession:
    def __init__(self, version: str) -> None:
        self.version = version

    def get_prompt(self) -> str:
        return self.version

    def list_tools(self) -> tuple[()]:
        return ()

    async def close(self) -> None:
        return None


class _PluginMemoryProvider:
    provider_id = "example.memory.provider"

    def __init__(self, version: str) -> None:
        self.version = version
        self.host: Any = None

    async def health_check(self) -> bool:
        return True

    async def open(
        self,
        scope: InvocationScope,
        host: object,
    ) -> _PluginMemorySession:
        del scope
        self.host = host
        return _PluginMemorySession(self.version)


def _plugin_memory_bundle(version: str) -> CapabilityBundle:
    return CapabilityBundle(
        provider_id="example.memory",
        provider_kind=CapabilityProviderKind.PLUGIN,
        version=version,
        contributions=(
            CapabilityContribution(
                contribution_id="provider",
                slot="memory.provider",
                entrypoint="example:memory_provider",
                config_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "prompt_prefix": {"type": "string"},
                    },
                },
            ),
        ),
    )


@pytest.mark.asyncio
async def test_plugin_memory_replacement_keeps_old_invocation() -> None:
    registry = GenerationRegistry()
    await registry.activate_bundle(
        _plugin_memory_bundle("1.0.0"),
        lambda _: _PluginMemoryProvider("1.0.0"),
    )
    selection = CapabilitySelection(
        memory_provider_id="example.memory.provider",
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
        _plugin_memory_bundle("2.0.0"),
        lambda _: _PluginMemoryProvider("2.0.0"),
    )
    new = await RuntimeAssemblyFactory(registry).open(
        agent_id="default",
        session_id="new",
        root_agent_id="default",
        root_session_id="new",
        workspace_dir="/tmp/qwenpaw-workspace",
        selection=selection,
    )

    old_provider = old.require("example.memory.provider", "memory.provider")
    new_provider = new.require("example.memory.provider", "memory.provider")
    assert old_provider.version == "1.0.0"
    assert new_provider.version == "2.0.0"
    await old.close()
    await new.close()


@pytest.mark.asyncio
async def test_builder_gives_plugin_memory_config_and_chat_state(
    tmp_path,
) -> None:
    registry = GenerationRegistry()
    provider = _PluginMemoryProvider("1.0.0")
    await registry.activate_bundle(
        _plugin_memory_bundle("1.0.0"),
        lambda _: provider,
    )
    assembly = await RuntimeAssemblyFactory(registry).open(
        agent_id="agent-1",
        conversation_id="chat-spec-1",
        session_id="transport-session",
        root_agent_id="agent-1",
        root_session_id="transport-session",
        workspace_dir=tmp_path,
        selection=CapabilitySelection(
            memory_provider_id="example.memory.provider",
        ),
    )
    context = SimpleNamespace(
        extras={"runtime_assembly": assembly},
        invocation_scope=assembly.scope,
        workspace=SimpleNamespace(
            memory_manager=None,
            config=SimpleNamespace(
                capability_configs={
                    "example.memory.provider": {
                        "prompt_prefix": "Pinned",
                    },
                },
            ),
        ),
    )

    # pylint: disable=protected-access
    session = await AgentBuilder()._open_memory_session(context)
    # pylint: enable=protected-access

    assert provider.host is not None
    assert isinstance(provider.host, ProviderMemoryHost)
    assert not hasattr(provider.host, "compatibility_backend")
    assert provider.host.config_snapshot() == {"prompt_prefix": "Pinned"}
    snapshot = await provider.host.state(
        MemoryStateScope.CONVERSATION,
    ).write("summary", "persisted", expected_revision=0)
    assert snapshot.owner_id == "chat-spec-1"
    assert snapshot.owner_id != assembly.scope.session_id
    await session.close()
    await assembly.close()


def test_memory_provider_unload_cleans_owned_tool_governance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unloading a Memory Provider removes its registered tool identities."""
    from qwenpaw.governance import tool_registry

    removed: list[str] = []
    monkeypatch.setattr(
        tool_registry,
        "DEFAULT_REGISTRY",
        SimpleNamespace(unregister_owner=removed.append),
    )
    manifest = SimpleNamespace(
        id="example.memory",
        contributions=(
            SimpleNamespace(
                contribution_id="provider",
                slot="memory.provider",
            ),
            SimpleNamespace(
                contribution_id="panel",
                slot="ui.task.panel",
            ),
        ),
    )

    # pylint: disable=protected-access
    PluginLoader._cleanup_contribution_tool_governance(manifest)
    # pylint: enable=protected-access

    assert removed == ["example.memory.provider"]
