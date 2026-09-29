# -*- coding: utf-8 -*-
"""Tests for generation-pinned lifecycle Hook Provider routing."""

from types import SimpleNamespace

import pytest
from agentscope.message import Msg, TextBlock

from qwenpaw.capabilities import GenerationRegistry
from qwenpaw.capabilities.system_hooks import WorkspaceHookProvider
from qwenpaw.kernel.invocation import (
    CapabilitySelection,
    DEFAULT_HOOK_PROVIDER_ID,
    InvocationScope,
)
from qwenpaw.kernel.models import (
    CapabilityBundle,
    CapabilityContribution,
    CapabilityProviderKind,
    HookDefinition,
    HookDisposition,
    HookMessage,
    HookOutcome,
    LifecyclePhase,
)
from qwenpaw.runtime.assembly import RuntimeAssemblyFactory
from qwenpaw.runtime.hook_providers import (
    HookRouterSession,
    WorkspaceHookHost,
)
from qwenpaw.runtime.hooks import (
    HookAction,
    HookBase,
    HookRegistry,
    HookResult,
)
from qwenpaw.runtime.phases import Phase


def _scope() -> InvocationScope:
    return InvocationScope(
        agent_id="default",
        session_id="chat",
        root_agent_id="default",
        root_session_id="chat",
        workspace_dir="/tmp/qwenpaw-workspace",
        registry_generation=9,
    )


class _RecordingHook(HookBase):
    phase = Phase.PRE_DISPATCH
    priority = 100

    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self._calls = calls

    async def run(self, _context):
        self._calls.append(self.name)
        return HookResult()


def _context(registry: HookRegistry):
    plugins = SimpleNamespace(hook_registry=registry)
    workspace = SimpleNamespace(plugins=plugins)
    return SimpleNamespace(
        workspace=workspace,
        context_injections=[],
        inject_context=lambda *args, **kwargs: None,
    )


@pytest.mark.asyncio
async def test_workspace_hook_provider_uses_fixed_snapshot() -> None:
    calls: list[str] = []
    registry = HookRegistry()
    registry.register(_RecordingHook("first", calls))
    host = WorkspaceHookHost.capture(
        _context(registry),
        DEFAULT_HOOK_PROVIDER_ID,
    )
    session = await WorkspaceHookProvider().open(_scope(), host)
    registry.register(_RecordingHook("later", calls))
    router = HookRouterSession((session,))

    definitions = router.list_hooks()
    outcome = await router.run(LifecyclePhase.PRE_DISPATCH)

    assert [item.hook_id for item in definitions] == [
        f"{DEFAULT_HOOK_PROVIDER_ID}.first",
    ]
    assert outcome.disposition == HookDisposition.CONTINUE
    assert calls == ["first"]


class _Session:
    def __init__(
        self,
        provider_id: str,
        definitions: tuple[HookDefinition, ...],
        outcomes: dict[str, HookOutcome],
        calls: list[str],
    ) -> None:
        self.provider_id = provider_id
        self._definitions = definitions
        self._outcomes = outcomes
        self._calls = calls

    def list_hooks(self):
        return self._definitions

    async def run_hook(self, hook_id):
        self._calls.append(hook_id)
        return self._outcomes.get(hook_id, HookOutcome())

    async def close(self):
        return None


def _definition(
    provider_id: str,
    name: str,
    *,
    priority: int = 100,
    before: tuple[str, ...] = (),
) -> HookDefinition:
    return HookDefinition(
        hook_id=f"{provider_id}.{name}",
        provider_id=provider_id,
        phase=LifecyclePhase.PRE_EXECUTE,
        priority=priority,
        before=before,
    )


@pytest.mark.asyncio
async def test_router_honors_cross_provider_dependencies() -> None:
    calls: list[str] = []
    first_id = "example.first.hooks"
    second_id = "example.second.hooks"
    first_hook = f"{first_id}.first"
    second_hook = f"{second_id}.second"
    first = _Session(
        first_id,
        (_definition(first_id, "first", priority=200),),
        {},
        calls,
    )
    second = _Session(
        second_id,
        (
            _definition(
                second_id,
                "second",
                priority=300,
                before=(first_hook,),
            ),
        ),
        {},
        calls,
    )

    await HookRouterSession((first, second)).run(LifecyclePhase.PRE_EXECUTE)

    assert calls == [second_hook, first_hook]


@pytest.mark.asyncio
async def test_skip_is_sticky_and_short_circuit_stops_phase() -> None:
    calls: list[str] = []
    provider_id = "example.lifecycle.hooks"
    skip_id = f"{provider_id}.skip"
    continue_id = f"{provider_id}.continue"
    session = _Session(
        provider_id,
        (
            _definition(provider_id, "skip", priority=10),
            _definition(provider_id, "continue", priority=20),
        ),
        {
            skip_id: HookOutcome(disposition=HookDisposition.SKIP_AGENT),
        },
        calls,
    )

    outcome = await HookRouterSession((session,)).run(
        LifecyclePhase.PRE_EXECUTE,
    )

    assert outcome.disposition == HookDisposition.SKIP_AGENT
    assert calls == [skip_id, continue_id]

    calls.clear()
    stop_id = f"{provider_id}.stop"
    stopped = _Session(
        provider_id,
        (
            _definition(provider_id, "stop", priority=5),
            _definition(provider_id, "continue", priority=20),
        ),
        {
            stop_id: HookOutcome(
                disposition=HookDisposition.SHORT_CIRCUIT,
                message=HookMessage(text="stopped"),
            ),
        },
        calls,
    )

    outcome = await HookRouterSession((stopped,)).run(
        LifecyclePhase.PRE_EXECUTE,
    )

    assert outcome.message is not None
    assert outcome.message.text == "stopped"
    assert calls == [stop_id]


class _ShortCircuitHook(HookBase):
    name = "short-circuit"
    phase = Phase.PRE_DISPATCH

    async def run(self, _context):
        return HookResult(
            action=HookAction.SHORT_CIRCUIT,
            payload=Msg(
                name="assistant",
                role="assistant",
                content=[TextBlock(type="text", text="legacy response")],
            ),
        )


@pytest.mark.asyncio
async def test_workspace_hook_translates_legacy_message() -> None:
    registry = HookRegistry()
    registry.register(_ShortCircuitHook())
    host = WorkspaceHookHost.capture(
        _context(registry),
        DEFAULT_HOOK_PROVIDER_ID,
    )
    session = await WorkspaceHookProvider().open(_scope(), host)
    outcome = await HookRouterSession((session,)).run(
        LifecyclePhase.PRE_DISPATCH,
    )

    assert outcome.disposition == HookDisposition.SHORT_CIRCUIT
    assert outcome.message is not None
    assert outcome.message.text == "legacy response"


class _PluginHookProvider:
    provider_id = "example.hooks.provider"

    def __init__(self, version: str) -> None:
        self.version = version

    async def health_check(self) -> bool:
        return True

    async def open(self, scope, host):
        return SimpleNamespace(scope=scope, host=host)


def _plugin_bundle(version: str) -> CapabilityBundle:
    return CapabilityBundle(
        provider_id="example.hooks",
        provider_kind=CapabilityProviderKind.PLUGIN,
        version=version,
        contributions=(
            CapabilityContribution(
                contribution_id="provider",
                slot="hook.provider",
                entrypoint="example:hook_provider",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_plugin_hook_replacement_keeps_old_invocation() -> None:
    registry = GenerationRegistry()
    await registry.activate_bundle(
        _plugin_bundle("1.0.0"),
        lambda _: _PluginHookProvider("1.0.0"),
    )
    selection = CapabilitySelection(
        hook_provider_ids=("example.hooks.provider",),
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
        lambda _: _PluginHookProvider("2.0.0"),
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
            "example.hooks.provider",
            "hook.provider",
        ).version
        == "1.0.0"
    )
    assert (
        new.require(
            "example.hooks.provider",
            "hook.provider",
        ).version
        == "2.0.0"
    )
    await old.close()
    await new.close()


@pytest.mark.asyncio
async def test_new_invocation_auto_selects_hook_providers() -> None:
    registry = GenerationRegistry()
    await registry.activate_bundle(
        _plugin_bundle("1.0.0"),
        lambda _: _PluginHookProvider("1.0.0"),
    )

    assembly = await RuntimeAssemblyFactory(registry).open(
        agent_id="default",
        session_id="chat",
        root_agent_id="default",
        root_session_id="chat",
        workspace_dir="/tmp/qwenpaw-workspace",
    )

    assert assembly.scope.selection.hook_provider_ids == (
        "example.hooks.provider",
        DEFAULT_HOOK_PROVIDER_ID,
    )
    await assembly.close()
