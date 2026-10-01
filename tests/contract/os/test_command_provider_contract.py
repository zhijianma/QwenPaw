# -*- coding: utf-8 -*-
"""Shared behavioral contract for system and plugin Command Providers."""

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from qwenpaw.capabilities.system_commands import WorkspaceCommandProvider
from qwenpaw.kernel.invocation import InvocationScope
from qwenpaw.plugins.architecture import PluginManifest
from qwenpaw.plugins.generations import GenerationRegistry
from qwenpaw.plugins.sdk import (
    CommandDefinition,
    CommandDisposition,
    CommandProvider,
    CommandRequest,
    CommandResult,
    CommandSession,
)
from qwenpaw.runtime.assembly import RuntimeAssemblyFactory
from qwenpaw.runtime.command_providers import CommandRouterSession
from qwenpaw.runtime.runtime import Runtime
from qwenpaw.runtime.slash_command_registry import (
    CommandSpec,
    SlashCommandRegistry,
    SYSTEM_COMMAND_OWNER_ID,
)


class _SystemHost:
    provider_id = "qwenpaw.system.commands.workspace-commands"

    def list_commands(self) -> tuple[CommandDefinition, ...]:
        return (
            CommandDefinition(
                command_id=f"{self.provider_id}.clear",
                provider_id=self.provider_id,
                name="clear",
                protected=True,
            ),
        )

    async def dispatch(self, request: CommandRequest) -> CommandResult:
        return CommandResult(
            disposition=CommandDisposition.RESPOND,
            message={"text": f"system:{request.command_name}"},
        )

    async def fallback(self, request: CommandRequest) -> CommandResult:
        del request
        return CommandResult(disposition=CommandDisposition.NOT_HANDLED)


def _scope(workspace_dir: Path) -> InvocationScope:
    return InvocationScope(
        agent_id="default",
        conversation_id="chat-contract",
        session_id="transport-contract",
        root_agent_id="default",
        root_session_id="transport-contract",
        workspace_dir=str(workspace_dir),
        registry_generation=11,
    )


@pytest.mark.asyncio
async def test_system_and_plugin_command_providers_share_behavioral_contract(
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
    command_module = importlib.import_module("runtime_provider_kit.command")
    scope = _scope(tmp_path / "contract-project")
    providers_and_hosts = (
        (WorkspaceCommandProvider(), _SystemHost()),
        (command_module.create_provider(), SimpleNamespace()),
    )

    sessions = []
    for provider, host in providers_and_hosts:
        assert isinstance(provider, CommandProvider)
        assert await provider.health_check() is True
        session = await provider.open(scope, host)
        assert isinstance(session, CommandSession)
        assert session.provider_id == provider.provider_id
        first = tuple(session.list_commands())
        assert first == tuple(session.list_commands())
        assert first
        assert all(item.provider_id == provider.provider_id for item in first)
        assert all(
            item.command_id.startswith(f"{provider.provider_id}.")
            for item in first
        )
        if not provider.provider_id.startswith("qwenpaw.system."):
            assert session.allows_dynamic_fallback is False
            assert all(not item.protected for item in first)
        sessions.append(session)

    router = CommandRouterSession(sessions)
    system_result = await router.dispatch("/clear")
    plugin_result = await router.dispatch("/project-name detail")
    assert system_result.message is not None
    assert system_result.message.text == "system:clear"
    assert plugin_result.message is not None
    assert plugin_result.message.text == "Project: contract-project (detail)"
    await router.close()


@pytest.mark.asyncio
async def test_plugin_command_flows_through_runtime_opening_pipeline(
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
    command_module = importlib.import_module("runtime_provider_kit.command")
    manifest_data = json.loads(
        (plugin_root / "plugin.json").read_text(encoding="utf-8"),
    )
    manifest_data["contributions"] = [
        item
        for item in manifest_data["contributions"]
        if item["slot"] == "command.provider"
    ]
    registry = GenerationRegistry()
    await registry.activate(
        PluginManifest.from_dict(manifest_data),
        lambda _declaration: command_module.create_provider(),
    )
    workspace_dir = tmp_path / "contract-project"
    workspace_dir.mkdir()
    assembly = await RuntimeAssemblyFactory(registry).open(
        agent_id="default",
        conversation_id="chat-contract",
        session_id="transport-contract",
        root_agent_id="default",
        root_session_id="transport-contract",
        workspace_dir=workspace_dir,
    )
    slash_registry = SlashCommandRegistry()

    async def clear_handler(_context, _arguments):
        return SimpleNamespace(content="cleared")

    slash_registry.register(
        CommandSpec(
            name="clear",
            handler=clear_handler,
            owner_id=SYSTEM_COMMAND_OWNER_ID,
            protected=True,
        ),
    )
    context = SimpleNamespace(
        workspace=SimpleNamespace(
            plugins=SimpleNamespace(
                slash_command_registry=slash_registry,
            ),
        ),
    )
    runtime = Runtime(workspace=SimpleNamespace(), app_services=None)

    # pylint: disable=protected-access
    router = await runtime._open_command_session(context, assembly)
    # pylint: enable=protected-access
    plugin_result = await router.dispatch("/project alpha")
    system_result = await router.dispatch("/clear")

    assert assembly.scope.registry_generation == registry.generation
    assert plugin_result.message is not None
    assert plugin_result.message.text == "Project: contract-project (alpha)"
    assert system_result.message is not None
    assert system_result.message.text == "cleared"
    await router.close()
    await assembly.close()
