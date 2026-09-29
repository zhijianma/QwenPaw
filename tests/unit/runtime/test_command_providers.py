# -*- coding: utf-8 -*-
"""Tests for generation-pinned Command Provider routing."""

from types import SimpleNamespace

import pytest
from agentscope.message import Msg, TextBlock

from qwenpaw.capabilities import GenerationRegistry
from qwenpaw.capabilities.system_commands import WorkspaceCommandProvider
from qwenpaw.kernel.invocation import CapabilitySelection, InvocationScope
from qwenpaw.kernel.invocation import DEFAULT_COMMAND_PROVIDER_ID
from qwenpaw.kernel.models import (
    CapabilityBundle,
    CapabilityContribution,
    CapabilityProviderKind,
    CommandDefinition,
    CommandDisposition,
    CommandMessage,
    CommandResult,
)
from qwenpaw.runtime.assembly import RuntimeAssemblyFactory
from qwenpaw.runtime.command_providers import (
    CommandRouterSession,
    WorkspaceCommandHost,
)
from qwenpaw.runtime.slash_command_registry import (
    CommandSpec,
    FallbackDispatch,
    SlashCommandRegistry,
    SYSTEM_COMMAND_OWNER_ID,
)


def _scope() -> InvocationScope:
    return InvocationScope(
        agent_id="default",
        session_id="chat",
        root_agent_id="default",
        root_session_id="chat",
        workspace_dir="/tmp/qwenpaw-workspace",
        registry_generation=8,
    )


async def _clear_handler(_context, _arguments):
    return Msg(
        name="assistant",
        role="assistant",
        content=[TextBlock(type="text", text="cleared")],
    )


def _context(registry: SlashCommandRegistry):
    plugins = SimpleNamespace(slash_command_registry=registry)
    workspace = SimpleNamespace(plugins=plugins)
    return SimpleNamespace(workspace=workspace)


@pytest.mark.asyncio
async def test_workspace_provider_uses_fixed_catalog_snapshot() -> None:
    registry = SlashCommandRegistry()
    registry.register(
        CommandSpec(
            name="clear",
            handler=_clear_handler,
            owner_id=SYSTEM_COMMAND_OWNER_ID,
            protected=True,
        ),
    )
    host = WorkspaceCommandHost.capture(
        _context(registry),
        "qwenpaw.system.commands.workspace-commands",
    )
    session = await WorkspaceCommandProvider().open(_scope(), host)

    async def later_handler(_context, _arguments):
        return None

    registry.register(CommandSpec(name="later", handler=later_handler))

    assert [item.name for item in session.list_commands()] == ["clear"]
    result = await session.dispatch(
        SimpleNamespace(raw_text="/clear", command_name="clear"),
    )
    assert result.disposition == CommandDisposition.RESPOND
    assert result.message is not None
    assert result.message.text == "cleared"


def test_legacy_registry_rejects_plugin_shadow_of_system_command() -> None:
    registry = SlashCommandRegistry()

    with pytest.raises(ValueError, match="reserved system command"):
        registry.register(CommandSpec(name="clear", handler=_clear_handler))


@pytest.mark.asyncio
async def test_dynamic_fallback_distinguishes_continue_from_no_match() -> None:
    registry = SlashCommandRegistry()

    async def fallback(raw_text, _context):
        if raw_text == "/skill run":
            return FallbackDispatch(handled=True)
        return None

    registry.register_fallback(fallback)
    handled, response = await registry.dispatch_detailed(
        "/skill run",
        object(),
    )
    missing, _ = await registry.dispatch_detailed("/missing", object())

    assert handled is True
    assert response is None
    assert missing is False


class _Session:
    def __init__(
        self,
        provider_id: str,
        definitions: tuple[CommandDefinition, ...],
        *,
        fallback: bool = False,
        response: str = "ok",
    ) -> None:
        self.provider_id = provider_id
        self.allows_dynamic_fallback = fallback
        self._definitions = definitions
        self._response = response

    def list_commands(self):
        return self._definitions

    async def dispatch(self, _request):
        return CommandResult(
            disposition=CommandDisposition.RESPOND,
            message=CommandMessage(text=self._response),
        )

    async def fallback(self, _request):
        return CommandResult(disposition=CommandDisposition.NOT_HANDLED)

    async def close(self):
        return None


def _definition(
    provider_id: str,
    command_id: str,
    name: str,
    *,
    protected: bool = False,
) -> CommandDefinition:
    return CommandDefinition(
        provider_id=provider_id,
        command_id=command_id,
        name=name,
        protected=protected,
    )


@pytest.mark.asyncio
async def test_protected_name_wins_independent_of_provider_order() -> None:
    system_id = "qwenpaw.system.commands.workspace-commands"
    plugin_id = "example.commands.provider"
    system = _Session(
        system_id,
        (
            _definition(
                system_id,
                f"{system_id}.clear",
                "clear",
                protected=True,
            ),
        ),
        response="system",
    )
    plugin = _Session(
        plugin_id,
        (_definition(plugin_id, f"{plugin_id}.clear", "clear"),),
        response="plugin",
    )

    for sessions in ((plugin, system), (system, plugin)):
        router = CommandRouterSession(sessions)
        result = await router.dispatch("/clear")
        assert result.message is not None
        assert result.message.text == "system"


@pytest.mark.asyncio
async def test_non_system_name_conflict_is_reported_not_shadowed() -> None:
    first_id = "example.first.commands"
    second_id = "example.second.commands"
    first = _Session(
        first_id,
        (_definition(first_id, f"{first_id}.deploy", "deploy"),),
    )
    second = _Session(
        second_id,
        (_definition(second_id, f"{second_id}.deploy", "deploy"),),
    )
    router = CommandRouterSession((second, first))

    result = await router.dispatch("/deploy")

    assert result.disposition == CommandDisposition.RESPOND
    assert result.message is not None
    assert "ambiguous" in result.message.text


class _PluginCommandProvider:
    provider_id = "example.commands.provider"

    def __init__(self, version: str) -> None:
        self.version = version

    async def health_check(self) -> bool:
        return True

    async def open(self, scope, host):
        return SimpleNamespace(scope=scope, host=host)


def _plugin_bundle(version: str) -> CapabilityBundle:
    return CapabilityBundle(
        provider_id="example.commands",
        provider_kind=CapabilityProviderKind.PLUGIN,
        version=version,
        contributions=(
            CapabilityContribution(
                contribution_id="provider",
                slot="command.provider",
                entrypoint="example:command_provider",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_plugin_command_replacement_keeps_old_invocation() -> None:
    registry = GenerationRegistry()
    await registry.activate_bundle(
        _plugin_bundle("1.0.0"),
        lambda _: _PluginCommandProvider("1.0.0"),
    )
    selection = CapabilitySelection(
        command_provider_ids=("example.commands.provider",),
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
        _plugin_bundle("2.0.0"),
        lambda _: _PluginCommandProvider("2.0.0"),
    )
    new = await RuntimeAssemblyFactory(registry).open(
        agent_id="default",
        session_id="new",
        root_agent_id="default",
        root_session_id="new",
        workspace_dir="/tmp/qwenpaw-workspace",
        selection=selection,
    )

    assert (
        old.require(
            "example.commands.provider",
            "command.provider",
        ).version
        == "1.0.0"
    )
    assert (
        new.require(
            "example.commands.provider",
            "command.provider",
        ).version
        == "2.0.0"
    )
    await old.close()
    await new.close()


@pytest.mark.asyncio
async def test_new_invocation_auto_selects_command_providers() -> None:
    registry = GenerationRegistry()
    await registry.activate_bundle(
        _plugin_bundle("1.0.0"),
        lambda _: _PluginCommandProvider("1.0.0"),
    )

    assembly = await RuntimeAssemblyFactory(registry).open(
        agent_id="default",
        session_id="chat",
        root_agent_id="default",
        root_session_id="chat",
        workspace_dir="/tmp/qwenpaw-workspace",
    )

    assert assembly.scope.selection.command_provider_ids == (
        "example.commands.provider",
        DEFAULT_COMMAND_PROVIDER_ID,
    )
    await assembly.close()
