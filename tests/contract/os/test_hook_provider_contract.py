# -*- coding: utf-8 -*-
"""Shared behavioral contract for system and plugin Hook Providers."""

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from qwenpaw.capabilities.system_hooks import WorkspaceHookProvider
from qwenpaw.kernel.invocation import DEFAULT_HOOK_PROVIDER_ID, InvocationScope
from qwenpaw.plugins.architecture import PluginManifest
from qwenpaw.plugins.generations import GenerationRegistry
from qwenpaw.plugins.sdk import (
    HookDefinition,
    HookDisposition,
    HookOutcome,
    HookProvider,
    HookSession,
    LifecyclePhase,
)
from qwenpaw.runtime.assembly import RuntimeAssemblyFactory
from qwenpaw.runtime.hook_providers import HookRouterSession
from qwenpaw.runtime.hooks import HookRegistry
from qwenpaw.runtime.runtime import Runtime


class _RecordingHost:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.injections: list[dict[str, object]] = []

    def list_hooks(self) -> tuple[HookDefinition, ...]:
        return (
            HookDefinition(
                hook_id=f"{DEFAULT_HOOK_PROVIDER_ID}.system-context",
                provider_id=DEFAULT_HOOK_PROVIDER_ID,
                phase=LifecyclePhase.PRE_AGENT_BUILD,
                priority=100,
            ),
        )

    async def run_hook(self, hook_id: str) -> HookOutcome:
        self.calls.append(hook_id)
        return HookOutcome()

    def inject_context(
        self,
        content: str,
        *,
        priority: int = 100,
        source: str = "",
    ) -> None:
        self.injections.append(
            {
                "content": content,
                "priority": priority,
                "source": source,
            },
        )


class _RuntimeContext:
    def __init__(self) -> None:
        self.workspace = SimpleNamespace(
            plugins=SimpleNamespace(hook_registry=HookRegistry()),
        )
        self.context_injections: list[dict[str, object]] = []

    def inject_context(
        self,
        content: str,
        *,
        priority: int = 100,
        source: str = "",
    ) -> None:
        self.context_injections.append(
            {
                "content": content,
                "priority": priority,
                "source": source,
            },
        )


def _scope(workspace_dir: Path) -> InvocationScope:
    return InvocationScope(
        agent_id="default",
        conversation_id="chat-contract",
        session_id="transport-contract",
        root_agent_id="default",
        root_session_id="transport-contract",
        workspace_dir=str(workspace_dir),
        registry_generation=13,
    )


@pytest.mark.asyncio
async def test_system_and_plugin_hooks_share_behavioral_contract(
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
    hook_module = importlib.import_module("runtime_provider_kit.hook")
    scope = _scope(tmp_path / "contract-project")
    system_host = _RecordingHost()
    plugin_host = _RecordingHost()
    providers_and_hosts = (
        (WorkspaceHookProvider(), system_host),
        (hook_module.create_provider(), plugin_host),
    )
    sessions = []

    for provider, host in providers_and_hosts:
        assert isinstance(provider, HookProvider)
        assert await provider.health_check() is True
        session = await provider.open(scope, host)
        assert isinstance(session, HookSession)
        assert session.provider_id == provider.provider_id
        first = tuple(session.list_hooks())
        assert first == tuple(session.list_hooks())
        assert first
        assert all(item.provider_id == provider.provider_id for item in first)
        assert all(
            item.hook_id.startswith(f"{provider.provider_id}.")
            for item in first
        )
        sessions.append(session)

    router = HookRouterSession(sessions)
    outcome = await router.run(LifecyclePhase.PRE_AGENT_BUILD)

    assert outcome.disposition == HookDisposition.CONTINUE
    assert system_host.calls == [
        f"{DEFAULT_HOOK_PROVIDER_ID}.system-context",
    ]
    assert plugin_host.injections == [
        {
            "content": "The active project is contract-project.",
            "priority": 120,
            "source": ("runtime-provider-kit.project-hooks.project-context"),
        },
    ]
    await router.close()


@pytest.mark.asyncio
async def test_plugin_hook_flows_through_runtime_opening_pipeline(
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
    hook_module = importlib.import_module("runtime_provider_kit.hook")
    manifest_data = json.loads(
        (plugin_root / "plugin.json").read_text(encoding="utf-8"),
    )
    manifest_data["contributions"] = [
        item
        for item in manifest_data["contributions"]
        if item["slot"] == "hook.provider"
    ]
    registry = GenerationRegistry()
    await registry.activate(
        PluginManifest.from_dict(manifest_data),
        lambda _declaration: hook_module.create_provider(),
    )
    assembly = await RuntimeAssemblyFactory(registry).open(
        agent_id="default",
        conversation_id="chat-contract",
        session_id="transport-contract",
        root_agent_id="default",
        root_session_id="transport-contract",
        workspace_dir=tmp_path / "contract-project",
    )
    context = _RuntimeContext()
    runtime = Runtime(workspace=SimpleNamespace(), app_services=None)

    # pylint: disable=protected-access
    router = await runtime._open_hook_session(context, assembly)
    # pylint: enable=protected-access
    outcome = await router.run(LifecyclePhase.PRE_AGENT_BUILD)

    assert assembly.scope.registry_generation == registry.generation
    assert outcome.disposition == HookDisposition.CONTINUE
    assert context.context_injections == [
        {
            "content": "The active project is contract-project.",
            "priority": 120,
            "source": ("runtime-provider-kit.project-hooks.project-context"),
        },
    ]
    await router.close()
    await assembly.close()
