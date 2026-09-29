# -*- coding: utf-8 -*-
"""Tests for channel-neutral ``/stop`` Interrupt semantics."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from qwenpaw.kernel import ControlCommandStatus
from qwenpaw.runtime.commands.control.base import ControlContext
from qwenpaw.runtime.commands.control.stop_handler import StopCommandHandler


def _context(workspace, *, payload=None) -> ControlContext:
    return ControlContext(
        workspace=workspace,
        payload=payload or {"message_id": "channel-message-1"},
        channel=SimpleNamespace(channel="slack"),
        session_id="slack:user-1",
        user_id="user-1",
        agent_id="agent-a",
        args={},
    )


def _workspace(receipt):
    return SimpleNamespace(
        agent_id="agent-a",
        invocation_control=SimpleNamespace(
            interrupt_current=AsyncMock(return_value=receipt),
            acknowledge_interrupt=AsyncMock(),
        ),
        chat_manager=SimpleNamespace(
            get_chat_id_by_session=AsyncMock(return_value="chat-1"),
        ),
        task_tracker=SimpleNamespace(
            request_stop=AsyncMock(return_value=False),
        ),
        channel_manager=SimpleNamespace(clear_queue=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_stop_uses_os_interrupt_without_clearing_queue() -> None:
    workspace = _workspace(
        SimpleNamespace(
            status=ControlCommandStatus.APPLIED,
            command_id=uuid4(),
        ),
    )

    result = await StopCommandHandler().handle(_context(workspace))

    assert "running invocation interrupted" in result
    workspace.invocation_control.interrupt_current.assert_awaited_once_with(
        agent_id="agent-a",
        conversation_id="chat-1",
        idempotency_key="channel-stop:channel-message-1",
    )
    workspace.task_tracker.request_stop.assert_not_awaited()
    workspace.channel_manager.clear_queue.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_falls_back_without_clearing_queue() -> None:
    workspace = _workspace(None)
    workspace.task_tracker.request_stop.return_value = True

    result = await StopCommandHandler().handle(_context(workspace))

    assert "running invocation interrupted" in result
    workspace.task_tracker.request_stop.assert_awaited_once_with("chat-1")
    workspace.channel_manager.clear_queue.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_acknowledges_accepted_command_after_fallback() -> None:
    command_id = uuid4()
    workspace = _workspace(
        SimpleNamespace(
            status=ControlCommandStatus.ACCEPTED,
            command_id=command_id,
        ),
    )
    workspace.task_tracker.request_stop.return_value = True

    await StopCommandHandler().handle(_context(workspace))

    acknowledge = workspace.invocation_control.acknowledge_interrupt
    acknowledge.assert_awaited_once_with(
        command_id,
        applied=True,
        detail="channel compatibility cancellation applied",
    )
    workspace.channel_manager.clear_queue.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_scopes_chat_lookup_to_requesting_user() -> None:
    workspace = _workspace(None)

    await StopCommandHandler().handle(_context(workspace))

    workspace.chat_manager.get_chat_id_by_session.assert_awaited_once_with(
        "slack:user-1",
        "slack",
        user_id="user-1",
    )
