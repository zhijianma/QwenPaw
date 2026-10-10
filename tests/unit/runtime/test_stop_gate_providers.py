# -*- coding: utf-8 -*-
"""Tests for generation-pinned loop Stop Gate Providers."""

from types import SimpleNamespace

import pytest
from agentscope.message import Msg, TextBlock

from qwenpaw.agents.react_agent import QwenPawAgent
from qwenpaw.capabilities import GenerationRegistry
from qwenpaw.capabilities.system_stop_gates import WorkspaceStopGateProvider
from qwenpaw.kernel.invocation import (
    CapabilitySelection,
    DEFAULT_STOP_GATE_PROVIDER_ID,
    InvocationScope,
)
from qwenpaw.kernel.models import (
    CapabilityBundle,
    CapabilityContribution,
    CapabilityProviderKind,
    StopGateAction,
    StopGateDecision,
    StopGateDefinition,
    StopGateInput,
    StopGateMessage,
)
from qwenpaw.loop.gates.base import (
    StopAction,
    StopHandlerRegistration,
    StopHandlerResult,
)
from qwenpaw.runtime.assembly import RuntimeAssemblyFactory
from qwenpaw.runtime.stop_gate_providers import (
    StopGateRouterSession,
    WorkspaceStopGateHost,
)


def _scope() -> InvocationScope:
    return InvocationScope(
        agent_id="default",
        session_id="chat",
        root_agent_id="default",
        root_session_id="chat",
        workspace_dir="/tmp/qwenpaw-workspace",
        registry_generation=10,
    )


class _LegacyHandler:
    def __init__(self, result: StopHandlerResult) -> None:
        self.result = result
        self.calls = 0

    async def __call__(self, _context):
        self.calls += 1
        return self.result


def _registration(
    name: str,
    result: StopHandlerResult,
    *,
    scope: str = "",
    active=True,
):
    return StopHandlerRegistration(
        plugin_id=f"test-{name}",
        handler=_LegacyHandler(result),
        priority=10,
        name=name,
        scope=scope,
        is_active=lambda: active,
    )


def _context(registrations):
    plugins = SimpleNamespace(stop_handlers=registrations)
    workspace = SimpleNamespace(plugins=plugins)
    return SimpleNamespace(
        workspace=workspace,
        agent=SimpleNamespace(),
    )


@pytest.mark.asyncio
async def test_workspace_provider_uses_fixed_registration_snapshot() -> None:
    registrations = [
        _registration(
            "first",
            StopHandlerResult(
                action=StopAction.INTERRUPT_AND_CONTINUE,
                continuation_message="keep going",
            ),
        ),
    ]
    host = WorkspaceStopGateHost.capture(
        _context(registrations),
        DEFAULT_STOP_GATE_PROVIDER_ID,
    )
    session = await WorkspaceStopGateProvider().open(_scope(), host)
    registrations.append(
        _registration(
            "later",
            StopHandlerResult(action=StopAction.TERMINATE),
        ),
    )
    router = StopGateRouterSession((session,))

    decision = await router.evaluate(
        StopGateInput(iteration=1, has_tool_calls=False),
    )

    assert [item.gate_id for item in router.list_gates()] == [
        f"{DEFAULT_STOP_GATE_PROVIDER_ID}.first",
    ]
    assert decision.action == StopGateAction.INTERRUPT_AND_CONTINUE
    assert decision.continuation_message == "keep going"


@pytest.mark.asyncio
async def test_empty_legacy_termination_does_not_hide_plugin_gate() -> None:
    system_id = DEFAULT_STOP_GATE_PROVIDER_ID
    registrations = [
        _registration(
            "default",
            StopHandlerResult(action=StopAction.TERMINATE),
        ),
    ]
    host = WorkspaceStopGateHost.capture(
        _context(registrations),
        system_id,
    )
    system_session = await WorkspaceStopGateProvider().open(
        _scope(),
        host,
    )
    plugin_id = "example.gates.provider"
    plugin_gate_id = f"{plugin_id}.review"
    plugin_session = _Session(
        plugin_id,
        (
            _definition(
                plugin_id,
                "review",
                priority=120,
                scope="",
            ),
        ),
        {
            plugin_gate_id: StopGateDecision(
                action=StopGateAction.INTERRUPT_AND_CONTINUE,
                continuation_message="review once",
                reason="plugin gate reached",
            ),
        },
    )

    decision = await StopGateRouterSession(
        (system_session, plugin_session),
    ).evaluate(
        StopGateInput(iteration=1, has_tool_calls=True),
    )

    assert decision.action == StopGateAction.INTERRUPT_AND_CONTINUE
    assert decision.reason == "plugin gate reached"
    assert registrations[0].handler.calls == 1


@pytest.mark.asyncio
async def test_legacy_termination_with_reason_remains_actionable() -> None:
    registrations = [
        _registration(
            "safety",
            StopHandlerResult(
                action=StopAction.TERMINATE,
                reason="safety gate stopped the loop",
            ),
        ),
    ]
    host = WorkspaceStopGateHost.capture(
        _context(registrations),
        DEFAULT_STOP_GATE_PROVIDER_ID,
    )
    session = await WorkspaceStopGateProvider().open(_scope(), host)

    decision = await StopGateRouterSession((session,)).evaluate(
        StopGateInput(iteration=1, has_tool_calls=False),
    )

    assert decision.action == StopGateAction.TERMINATE
    assert decision.reason == "safety gate stopped the loop"


class _Session:
    def __init__(
        self,
        provider_id: str,
        definitions: tuple[StopGateDefinition, ...],
        decisions: dict[str, StopGateDecision],
        active: dict[str, bool] | None = None,
    ) -> None:
        self.provider_id = provider_id
        self._definitions = definitions
        self._decisions = decisions
        self._active = active or {}
        self.started = 0
        self.reset = 0

    def list_gates(self):
        return self._definitions

    def is_active(self, gate_id):
        return self._active.get(gate_id, False)

    async def evaluate(self, gate_id, _gate_input):
        return self._decisions.get(gate_id, StopGateDecision())

    async def start_turn(self):
        self.started += 1

    async def reset_conversation(self):
        self.reset += 1

    async def close(self):
        return None


def _definition(
    provider_id: str,
    name: str,
    *,
    priority: int,
    scope: str,
) -> StopGateDefinition:
    return StopGateDefinition(
        gate_id=f"{provider_id}.{name}",
        provider_id=provider_id,
        priority=priority,
        scope=scope,
    )


@pytest.mark.asyncio
async def test_active_explicit_scope_suppresses_default_scope() -> None:
    provider_id = "example.gates.provider"
    default_id = f"{provider_id}.default"
    goal_id = f"{provider_id}.goal"
    session = _Session(
        provider_id,
        (
            _definition(
                provider_id,
                "default",
                priority=1,
                scope="default",
            ),
            _definition(provider_id, "goal", priority=20, scope="goal"),
        ),
        {
            default_id: StopGateDecision(
                action=StopGateAction.TERMINATE,
                reason="default",
            ),
            goal_id: StopGateDecision(
                action=StopGateAction.INTERRUPT_AND_CONTINUE,
                reason="goal",
            ),
        },
        {goal_id: True},
    )

    decision = await StopGateRouterSession((session,)).evaluate(
        StopGateInput(iteration=1, has_tool_calls=False),
    )

    assert decision.reason == "goal"


@pytest.mark.asyncio
async def test_unscoped_gate_runs_before_active_scope() -> None:
    provider_id = "example.gates.provider"
    safety_id = f"{provider_id}.safety"
    goal_id = f"{provider_id}.goal"
    session = _Session(
        provider_id,
        (
            _definition(provider_id, "safety", priority=1, scope=""),
            _definition(provider_id, "goal", priority=20, scope="goal"),
        ),
        {
            safety_id: StopGateDecision(
                action=StopGateAction.TERMINATE,
                reason="safety",
            ),
        },
        {goal_id: True},
    )

    decision = await StopGateRouterSession((session,)).evaluate(
        StopGateInput(iteration=1, has_tool_calls=False),
    )

    assert decision.reason == "safety"


@pytest.mark.asyncio
async def test_router_forwards_lifecycle_to_provider_sessions() -> None:
    provider_id = "example.gates.provider"
    session = _Session(provider_id, (), {})
    router = StopGateRouterSession((session,))

    await router.start_turn()
    await router.reset_conversation()

    assert session.started == 1
    assert session.reset == 1


@pytest.mark.asyncio
async def test_agent_uses_pinned_session_instead_of_global_registry() -> None:
    class _PinnedSession:
        async def evaluate(self, gate_input):
            assert gate_input.final_message is not None
            assert gate_input.final_message.text == "candidate"
            return StopGateDecision(
                action=StopGateAction.TERMINATE,
                reason="pinned",
                final_message=StopGateMessage(text="accepted"),
            )

    fake_agent = SimpleNamespace(
        _stop_gate_session=_PinnedSession(),
        state=SimpleNamespace(cur_iter=3),
    )
    message = Msg(
        name="assistant",
        role="assistant",
        content=[TextBlock(type="text", text="candidate")],
    )

    # The protected call directly verifies the Agent integration boundary.
    # pylint: disable-next=protected-access
    result = await QwenPawAgent._run_stop_handlers(
        fake_agent,
        message,
    )

    assert result.reason == "pinned"
    assert result.final_message.content[0].text == "accepted"


class _PluginStopGateProvider:
    provider_id = "example.gates.provider"

    def __init__(self, version: str) -> None:
        self.version = version

    async def health_check(self) -> bool:
        return True

    async def open(self, scope, host):
        return SimpleNamespace(scope=scope, host=host)


def _plugin_bundle(version: str) -> CapabilityBundle:
    return CapabilityBundle(
        provider_id="example.gates",
        provider_kind=CapabilityProviderKind.PLUGIN,
        version=version,
        contributions=(
            CapabilityContribution(
                contribution_id="provider",
                slot="loop.gate.provider",
                entrypoint="example:stop_gate_provider",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_plugin_gate_replacement_keeps_old_invocation() -> None:
    registry = GenerationRegistry()
    await registry.activate_bundle(
        _plugin_bundle("1.0.0"),
        lambda _: _PluginStopGateProvider("1.0.0"),
    )
    selection = CapabilitySelection(
        stop_gate_provider_ids=("example.gates.provider",),
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
        lambda _: _PluginStopGateProvider("2.0.0"),
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
            "example.gates.provider",
            "loop.gate.provider",
        ).version
        == "1.0.0"
    )
    assert (
        new.require(
            "example.gates.provider",
            "loop.gate.provider",
        ).version
        == "2.0.0"
    )
    await old.close()
    await new.close()


@pytest.mark.asyncio
async def test_new_invocation_auto_selects_stop_gate_providers() -> None:
    registry = GenerationRegistry()
    await registry.activate_bundle(
        _plugin_bundle("1.0.0"),
        lambda _: _PluginStopGateProvider("1.0.0"),
    )

    assembly = await RuntimeAssemblyFactory(registry).open(
        agent_id="default",
        session_id="chat",
        root_agent_id="default",
        root_session_id="chat",
        workspace_dir="/tmp/qwenpaw-workspace",
    )

    assert assembly.scope.selection.stop_gate_provider_ids == (
        "example.gates.provider",
        DEFAULT_STOP_GATE_PROVIDER_ID,
    )
    await assembly.close()
