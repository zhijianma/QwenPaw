# -*- coding: utf-8 -*-
"""Tests for steer delivery at QwenPaw Agent safe points."""

# pylint: disable=protected-access

from types import SimpleNamespace
from uuid import uuid4

import pytest
from agentscope.message import Msg, TextBlock

from qwenpaw.agents.react_agent import QwenPawAgent
from qwenpaw.invocation_control import InvocationControlService
from qwenpaw.kernel import (
    ControlCommand,
    ControlCommandKind,
    SteerSafePoint,
)
from qwenpaw.runtime.interaction_middleware import (
    RuntimeInteractionMiddleware,
)


def _steer(invocation_id):
    return ControlCommand(
        kind=ControlCommandKind.STEER,
        agent_id="default",
        conversation_id="chat-1",
        idempotency_key=str(uuid4()),
        expected_revision=1,
        target_invocation_id=invocation_id,
        instruction="Do not call the old tools.",
    )


@pytest.mark.asyncio
async def test_reasoning_safe_point_appends_auditable_user_message() -> None:
    service = InvocationControlService()
    invocation_id = uuid4()
    session = await service.open_steering(invocation_id)
    command = _steer(invocation_id)
    receipt = await service.offer_steer(command)
    state = SimpleNamespace(context=[])
    agent = SimpleNamespace(
        _request_context={"_steering_session": session},
        state=state,
    )

    middleware = RuntimeInteractionMiddleware()

    async def reasoning():
        yield SimpleNamespace(event="model-call")

    items = [
        item async for item in middleware.on_reasoning(agent, {}, reasoning)
    ]

    assert len(items) == 1
    assert state.context[-1].content[0].text == command.instruction
    assert state.context[-1].metadata == {
        "qwenpaw_control_command_id": str(command.command_id),
        "qwenpaw_steer_safe_point": "before_reasoning",
    }
    assert await receipt is SteerSafePoint.BEFORE_REASONING


@pytest.mark.asyncio
async def test_after_reasoning_steer_withholds_final_message() -> None:
    service = InvocationControlService()
    invocation_id = uuid4()
    session = await service.open_steering(invocation_id)
    command = _steer(invocation_id)
    state = SimpleNamespace(context=[])
    agent = SimpleNamespace(
        _request_context={"_steering_session": session},
        state=state,
    )
    middleware = RuntimeInteractionMiddleware()
    receipt = None

    async def reasoning():
        nonlocal receipt
        receipt = await service.offer_steer(command)
        yield Msg(
            name="assistant",
            role="assistant",
            content=[TextBlock(text="Old final answer")],
        )

    items = [
        item async for item in middleware.on_reasoning(agent, {}, reasoning)
    ]

    assert items == []
    assert agent._steer_forced_continue is True
    assert state.context[-1].content[0].text == command.instruction
    assert receipt is not None
    assert await receipt is SteerSafePoint.AFTER_REASONING


@pytest.mark.asyncio
async def test_tool_boundary_invalidates_whole_unadmitted_remainder() -> None:
    service = InvocationControlService()
    invocation_id = uuid4()
    session = await service.open_steering(invocation_id)
    command = _steer(invocation_id)
    receipt = await service.offer_steer(command)
    tool_calls = [
        SimpleNamespace(id="call-1", name="first"),
        SimpleNamespace(id="call-2", name="second"),
    ]

    class FakeState:
        def __init__(self):
            self.context = []

        def get_unfinished_tool_calls(self, _name):
            return tool_calls

    class FakeAgent:
        name = "assistant"

        def __init__(self):
            self._request_context = {"_steering_session": session}
            self._steer_cancelled_tool_call_ids = set()
            self.state = FakeState()

        async def _handle_error_tool_call(
            self,
            tool_call,
            _message,
            state,
        ):
            yield (tool_call.id, state.value)

    agent = FakeAgent()
    (
        events,
        applied,
    ) = await QwenPawAgent._apply_pending_steers_at_tool_boundary(
        agent,
        SteerSafePoint.BEFORE_TOOL_BATCH,
    )

    assert applied == 1
    assert events == [("call-1", "denied"), ("call-2", "denied")]
    assert agent._steer_cancelled_tool_call_ids == {"call-1", "call-2"}
    assert agent.state.context[-1].content[0].text == command.instruction
    assert await receipt is SteerSafePoint.BEFORE_TOOL_BATCH


@pytest.mark.asyncio
async def test_after_tool_boundary_never_rewrites_completed_results() -> None:
    service = InvocationControlService()
    invocation_id = uuid4()
    session = await service.open_steering(invocation_id)
    command = _steer(invocation_id)
    receipt = await service.offer_steer(command)
    tool_calls = [SimpleNamespace(id="call-1", name="completed")]

    class FakeState:
        def __init__(self):
            self.context = []

        def get_unfinished_tool_calls(self, _name):
            return tool_calls

    class FakeAgent:
        name = "assistant"

        def __init__(self):
            self._request_context = {"_steering_session": session}
            self._steer_cancelled_tool_call_ids = set()
            self.state = FakeState()

        async def _handle_error_tool_call(
            self,
            tool_call,
            _message,
            state,
        ):
            yield (tool_call.id, state.value)

    agent = FakeAgent()
    events, applied = (
        await QwenPawAgent._apply_pending_steers_at_tool_boundary(
            agent,
            SteerSafePoint.AFTER_TOOL_BATCH,
        )
    )

    assert applied == 1
    assert events == []
    assert agent._steer_cancelled_tool_call_ids == set()
    assert agent.state.context[-1].content[0].text == command.instruction
    assert await receipt is SteerSafePoint.AFTER_TOOL_BATCH
