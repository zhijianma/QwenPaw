# -*- coding: utf-8 -*-
"""Tests for invocation-scoped Agent Mode Provider assembly."""

from types import SimpleNamespace

import pytest

from qwenpaw.capabilities import GenerationRegistry
from qwenpaw.capabilities.system_modes import WorkspaceAgentModeProvider
from qwenpaw.kernel.invocation import CapabilitySelection, InvocationScope
from qwenpaw.kernel.models import (
    CapabilityBundle,
    CapabilityContribution,
    CapabilityProviderKind,
)
from qwenpaw.runtime.assembly import RuntimeAssemblyFactory
from qwenpaw.runtime.mode_providers import (
    ProviderAgentModeHost,
    WorkspaceAgentModeHost,
)


class _Mode:
    def __init__(self, name: str, active: bool = True) -> None:
        self.name = name
        self.active = active
        self.started = 0
        self.reset = 0

    def is_active(self, context: object) -> bool:
        del context
        return self.active

    async def on_turn_start(self, context: object) -> None:
        del context
        self.started += 1

    async def on_conversation_reset(self, context: object) -> None:
        del context
        self.reset += 1


def _scope() -> InvocationScope:
    return InvocationScope(
        agent_id="default",
        session_id="chat",
        root_agent_id="default",
        root_session_id="chat",
        workspace_dir="/tmp/qwenpaw-workspace",
        registry_generation=5,
    )


@pytest.mark.asyncio
async def test_workspace_mode_session_uses_captured_snapshot() -> None:
    first = _Mode("default")
    second = _Mode("goal", active=False)
    plugins = SimpleNamespace(modes=[first, second])
    context = SimpleNamespace(workspace=SimpleNamespace(plugins=plugins))
    host = WorkspaceAgentModeHost.capture(context)
    session = await WorkspaceAgentModeProvider().open(_scope(), host)

    plugins.modes[:] = [_Mode("replacement")]

    assert session.active_mode_names() == ("default",)
    await session.start_turn()
    await session.reset_conversation()
    assert first.started == 1
    assert second.started == 1
    assert first.reset == 1
    assert second.reset == 1
    await session.close()


@pytest.mark.asyncio
async def test_workspace_mode_reset_isolates_one_broken_mode() -> None:
    """One reset failure cannot block the remaining captured modes."""
    broken = _Mode("broken")
    healthy = _Mode("healthy")

    async def fail_reset(context: object) -> None:
        del context
        raise RuntimeError("reset failed")

    broken.on_conversation_reset = fail_reset  # type: ignore[method-assign]
    context = SimpleNamespace(
        workspace=SimpleNamespace(
            plugins=SimpleNamespace(modes=[broken, healthy]),
        ),
    )
    session = await WorkspaceAgentModeProvider().open(
        _scope(),
        WorkspaceAgentModeHost.capture(context),
    )

    await session.reset_conversation()

    assert healthy.reset == 1


class _PluginModeSession:
    def __init__(self, version: str) -> None:
        self.version = version

    def active_mode_names(self) -> tuple[str, ...]:
        return (self.version,)

    async def start_turn(self) -> None:
        return None

    async def reset_conversation(self) -> None:
        return None

    async def close(self) -> None:
        return None


class _PluginModeProvider:
    provider_id = "example.modes.provider"

    def __init__(self, version: str) -> None:
        self.version = version

    async def health_check(self) -> bool:
        return True

    async def open(
        self,
        scope: InvocationScope,
        host: object,
    ) -> _PluginModeSession:
        del scope, host
        return _PluginModeSession(self.version)


def _plugin_mode_bundle(version: str) -> CapabilityBundle:
    return CapabilityBundle(
        provider_id="example.modes",
        provider_kind=CapabilityProviderKind.PLUGIN,
        version=version,
        contributions=(
            CapabilityContribution(
                contribution_id="provider",
                slot="agent.mode.provider",
                entrypoint="example:mode_provider",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_plugin_mode_replacement_keeps_old_invocation() -> None:
    registry = GenerationRegistry()
    await registry.activate_bundle(
        _plugin_mode_bundle("1.0.0"),
        lambda _: _PluginModeProvider("1.0.0"),
    )
    selection = CapabilitySelection(
        agent_mode_provider_id="example.modes.provider",
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
        _plugin_mode_bundle("2.0.0"),
        lambda _: _PluginModeProvider("2.0.0"),
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
        "example.modes.provider",
        "agent.mode.provider",
    )
    new_provider = new.require(
        "example.modes.provider",
        "agent.mode.provider",
    )
    assert old_provider.version == "1.0.0"
    assert new_provider.version == "2.0.0"
    old_session = await old_provider.open(
        old.scope,
        ProviderAgentModeHost(),
    )
    new_session = await new_provider.open(
        new.scope,
        ProviderAgentModeHost(),
    )
    assert old_session.active_mode_names() == ("1.0.0",)
    assert new_session.active_mode_names() == ("2.0.0",)
    await old_session.close()
    await new_session.close()
    await old.close()
    await new.close()
