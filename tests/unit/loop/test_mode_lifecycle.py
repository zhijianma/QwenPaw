# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Tests for mode-owned handler selection and reset lifecycle."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from qwenpaw.loop.gates.base import (
    StopAction,
    StopHandlerRegistration,
)
from qwenpaw.loop.gates.handler import StopHandler
from qwenpaw.loop.gates.rubric import QualitativeRubricGate
from qwenpaw.kernel import (
    CapabilityProviderKind,
    ConversationOutcomeStatus,
    InvocationScope,
)
from qwenpaw.loop.gates.runner import _filter_by_scope
from qwenpaw.modes.goal.goal_mode import GoalMode, GoalSession
from qwenpaw.modes.mission import MissionMode
from qwenpaw.modes.mission.gates import MissionGate
from qwenpaw.modes.mission.state import write_loop_config, write_prd_json
from qwenpaw.runtime.runtime import Runtime
from qwenpaw.runtime.outcome_context import scoped_outcome_context
from qwenpaw.runtime.outcome_hosts import provider_outcome_host


class _OutcomeHost:
    def __init__(self, *, fails: bool = False) -> None:
        self.fails = fails
        self.requests = []

    async def declare(self, request):
        if self.fails:
            raise RuntimeError("store unavailable")
        self.requests.append(request)
        return SimpleNamespace(outcome_id=uuid4())


def _registration(
    scope: str,
    *,
    is_active=None,
) -> StopHandlerRegistration:
    return StopHandlerRegistration(
        plugin_id=f"test-{scope}",
        handler=StopHandler(),
        name=f"{scope}-handler",
        scope=scope,
        is_active=is_active,
    )


def test_explicit_mode_scope_replaces_default_scope():
    """An active mode handler suppresses the default handler."""
    default = _registration("default")
    goal = _registration("goal", is_active=lambda: True)

    selected = _filter_by_scope([default, goal])

    assert selected == [goal]


def test_inactive_mode_scope_keeps_default_scope():
    """An inactive mode handler leaves the default handler selected."""
    default = _registration("default")
    goal = _registration("goal", is_active=lambda: False)

    selected = _filter_by_scope([default, goal])

    assert selected == [default]


def test_unscoped_plugin_handler_is_always_selected():
    """Unscoped plugin handlers remain available with explicit modes."""
    plugin = _registration("")
    default = _registration("default")
    goal = _registration("goal", is_active=lambda: True)

    selected = _filter_by_scope([plugin, default, goal])

    assert selected == [plugin, goal]


@pytest.mark.asyncio
async def test_goal_reset_removes_only_current_session():
    """Conversation reset does not clear another goal conversation."""
    mode = GoalMode()
    mode._sessions["session-a"] = GoalSession(goal="first")
    mode._sessions["session-b"] = GoalSession(goal="second")
    ctx = SimpleNamespace(session_id="session-a")

    await mode.on_conversation_reset(ctx)

    assert "session-a" not in mode._sessions
    assert "session-b" in mode._sessions


@pytest.mark.asyncio
async def test_goal_uses_chat_identity_across_transport_sessions():
    """A transport session change does not replace the Chat-owned goal."""
    mode = GoalMode()
    host = _OutcomeHost()

    with scoped_outcome_context("chat-a", host):
        ctx = SimpleNamespace(
            session_id="transport-a",
            invocation_scope=SimpleNamespace(conversation_id="chat-a"),
            workspace=SimpleNamespace(
                plugins=SimpleNamespace(modes=[mode]),
            ),
        )
        response = await mode.commands()[0].handler(ctx, "Fix the tests")
        assert response is None
        assert mode.get_session("chat-a") is not None

    with scoped_outcome_context("chat-a", host):
        assert mode.active_session() is mode.get_session("chat-a")
        assert mode.get_session("transport-a") is None


@pytest.mark.asyncio
async def test_goal_completion_persists_explicit_business_outcome():
    """Technical tool completion cannot replace the business Outcome."""
    mode = GoalMode()
    host = _OutcomeHost()
    mode._sessions["chat-a"] = GoalSession(goal="Fix the tests")

    with scoped_outcome_context("chat-a", host):
        result = await mode.tools()[2].func("complete")

    assert result.startswith("Goal marked as complete")
    assert mode.get_session("chat-a") is None
    assert len(host.requests) == 1
    assert host.requests[0].status is ConversationOutcomeStatus.ACHIEVED


@pytest.mark.asyncio
async def test_goal_completion_reaches_real_outcome_store(tmp_path):
    """Goal completion crosses the real Host admission and SQLite store."""
    workspace = SimpleNamespace(workspace_dir=tmp_path)
    correlation_id = uuid4()
    scope = InvocationScope(
        correlation_id=correlation_id,
        agent_id="default",
        conversation_id="chat-a",
        session_id="transport-a",
        root_agent_id="default",
        root_session_id="transport-a",
        workspace_dir=str(tmp_path),
        registry_generation=3,
    )
    host = provider_outcome_host(
        workspace,
        scope,
        producer_id="qwenpaw.system.goal-mode",
        provider_kind=CapabilityProviderKind.SYSTEM,
    )
    assert host is not None
    mode = GoalMode()
    mode._sessions["chat-a"] = GoalSession(goal="Fix the tests")

    with scoped_outcome_context("chat-a", host):
        result = await mode.tools()[2].func("complete")

    outcomes = (
        await workspace.conversation_outcome_store.latest_for_correlations(
            agent_id="default",
            conversation_id="chat-a",
            correlation_ids=(correlation_id,),
        )
    )
    assert result.startswith("Goal marked as complete")
    assert len(outcomes) == 1
    assert outcomes[0].correlation_id == correlation_id
    assert outcomes[0].status is ConversationOutcomeStatus.ACHIEVED
    assert outcomes[0].invocation_id == scope.invocation_id


@pytest.mark.asyncio
async def test_goal_outcome_failure_keeps_goal_active():
    """A failed durable declaration must not report business completion."""
    mode = GoalMode()
    host = _OutcomeHost(fails=True)
    session = GoalSession(goal="Fix the tests")
    mode._sessions["chat-a"] = session

    with scoped_outcome_context("chat-a", host):
        result = await mode.tools()[2].func("complete")

    assert "remains active" in result
    assert session.active
    assert mode.get_session("chat-a") is session


@pytest.mark.asyncio
async def test_goal_blocked_is_not_achieved():
    """A blocked goal receives an explicit non-achieved disposition."""
    mode = GoalMode()
    host = _OutcomeHost()
    session = GoalSession(goal="Fix the tests")
    mode._sessions["chat-a"] = session

    with scoped_outcome_context("chat-a", host):
        result = await mode.tools()[2].func("blocked")

    assert result.startswith("Goal marked as blocked")
    assert not session.active
    assert host.requests[0].status is (ConversationOutcomeStatus.NOT_ACHIEVED)


@pytest.mark.asyncio
async def test_goal_legacy_session_can_finish_without_chat_outcome():
    """A channel without ChatSpec keeps the compatibility fast path."""
    mode = GoalMode()
    session = GoalSession(goal="Answer the channel message")
    mode._sessions["legacy-session"] = session

    with patch(
        "qwenpaw.modes.goal.goal_mode.get_current_session_id",
        return_value="legacy-session",
    ), scoped_outcome_context(None, None):
        result = await mode.tools()[2].func("complete")

    assert result.startswith("Goal marked as complete")
    assert mode.get_session("legacy-session") is None


@pytest.mark.asyncio
async def test_goal_reset_prefers_chat_identity():
    """Reset removes the Chat goal even when transport identity differs."""
    mode = GoalMode()
    mode._sessions["chat-a"] = GoalSession(goal="first")
    mode._sessions["transport-a"] = GoalSession(goal="legacy")
    ctx = SimpleNamespace(
        session_id="transport-a",
        invocation_scope=SimpleNamespace(conversation_id="chat-a"),
    )

    await mode.on_conversation_reset(ctx)

    assert "chat-a" not in mode._sessions
    assert "transport-a" in mode._sessions


@pytest.mark.asyncio
async def test_goal_activation_rejects_an_active_explicit_mode():
    """A persistent session mode must be exited before another starts."""
    mode = GoalMode()
    active = SimpleNamespace(
        name="mission",
        is_active=lambda _ctx: True,
    )
    ctx = SimpleNamespace(
        session_id="session-a",
        workspace=SimpleNamespace(
            plugins=SimpleNamespace(modes=[active, mode]),
        ),
    )

    response = await mode.commands()[0].handler(ctx, "Fix the tests")

    assert response is not None
    assert "active mission mode" in response.content[0].text
    assert mode.get_session("session-a") is None


@pytest.mark.asyncio
async def test_mission_turn_start_restores_persisted_session(tmp_path):
    """Mission state is active before stop-handler scope selection."""
    mode = MissionMode()
    mode._gate = MissionGate()
    ctx = SimpleNamespace(
        mode_state={
            "mission": {
                "active": True,
                "loop_dir": str(tmp_path),
                "phase": "execution",
            },
        },
    )

    with patch(
        "qwenpaw.loop.gates.loop_gate._session_id",
        return_value="mission-session",
    ):
        await mode.on_turn_start(ctx)
        assert mode._is_gate_active()


@pytest.mark.asyncio
async def test_internal_mission_uses_native_prd_gate(tmp_path):
    mode = MissionMode()
    write_loop_config(tmp_path, {"current_phase": "execution"})
    prd = {
        "userStories": [{"id": "ASSET-1", "title": "plugin", "passes": False}],
    }
    write_prd_json(tmp_path, prd)

    mode.start_internal_mission("migration-mission", tmp_path)
    assert not await mode.check_internal_mission("migration-mission")
    prd["userStories"][0]["passes"] = True
    write_prd_json(tmp_path, prd)
    assert await mode.check_internal_mission("migration-mission")
    mode.finish_internal_mission("migration-mission")


@pytest.mark.asyncio
async def test_mission_state_uses_session_lifecycle_and_reset(tmp_path):
    """Stage 1 phase persists and reset clears the same mode_state."""
    write_loop_config(
        tmp_path,
        {"current_phase": "prd_generation"},
    )
    mode = MissionMode()
    mode._gate = MissionGate()
    ctx = SimpleNamespace(mode_state={})

    with patch(
        "qwenpaw.loop.gates.loop_gate._session_id",
        return_value="mission-session",
    ):
        mode._gate.activate_for_mission(tmp_path)
        await mode.sync_persistent_state(ctx)

        assert ctx.mode_state == {
            "mission": {
                "active": True,
                "loop_dir": str(tmp_path),
                "phase": "prd_generation",
            },
        }

        write_loop_config(
            tmp_path,
            {"current_phase": "execution_confirmed"},
        )
        await mode.sync_persistent_state(ctx)

        assert ctx.mode_state["mission"]["phase"] == "execution_confirmed"

        await mode.on_conversation_reset(ctx)

        assert ctx.mode_state == {}
        assert not mode._is_gate_active()


@pytest.mark.asyncio
async def test_runtime_awaits_mode_turn_start_callbacks():
    """Runtime awaits the pinned mode session turn-start callback."""
    calls = []

    class _Session:
        async def start_turn(self):
            calls.append("started")

    workspace = SimpleNamespace()
    runtime = Runtime(workspace=workspace, app_services=None)
    ctx = SimpleNamespace(extras={"agent_mode_session": _Session()})

    await runtime._start_modes(ctx)

    assert calls == ["started"]


@pytest.mark.asyncio
async def test_qualitative_rubric_state_is_session_isolated():
    """Resetting one rubric session leaves another session untouched."""
    gate = QualitativeRubricGate(
        rubric="continue",
        max_evaluations=1,
    )

    with patch(
        "qwenpaw.loop.gates.loop_gate._session_id",
        return_value="session-a",
    ):
        first_a = await gate.check({})

    with patch(
        "qwenpaw.loop.gates.loop_gate._session_id",
        return_value="session-b",
    ):
        first_b = await gate.check({})

    with patch(
        "qwenpaw.loop.gates.loop_gate._session_id",
        return_value="session-a",
    ):
        gate.reset_session()
        next_a = await gate.check({})

    with patch(
        "qwenpaw.loop.gates.loop_gate._session_id",
        return_value="session-b",
    ):
        next_b = await gate.check({})

    assert first_a.action == StopAction.INTERRUPT_AND_CONTINUE
    assert first_b.action == StopAction.INTERRUPT_AND_CONTINUE
    assert next_a.action == StopAction.INTERRUPT_AND_CONTINUE
    assert next_b.action == StopAction.BYPASS
