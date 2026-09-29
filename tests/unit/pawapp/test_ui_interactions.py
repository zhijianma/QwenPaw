# -*- coding: utf-8 -*-
"""PawApp UI interaction integration tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

from qwenpaw.interactions import InteractionService
from qwenpaw.kernel import (
    ActorRef,
    ActorType,
    InteractionResponse,
)
from qwenpaw.pawapp.context import PawAppContext, UIBridge
from qwenpaw.pawapp.task import SSEChannel, TaskManager


class _EventChannel:
    """Collect task delivery events without SSE framing."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.changed = asyncio.Event()

    async def send_event(self, event: dict) -> None:
        self.events.append(event)
        self.changed.set()


async def test_confirm_uses_chat_owned_durable_interaction(
    tmp_path: Path,
) -> None:
    service = InteractionService(tmp_path / "interactions.sqlite3")
    channel = _EventChannel()
    invocation_id = uuid4()
    bridge = UIBridge(
        sse_channel=channel,
        interaction_service=service,
        agent_id="default",
        chat_id="chat-child",
        invocation_id=invocation_id,
    )

    pending = asyncio.create_task(
        bridge.confirm(
            "Publish this change?",
            data={"branch": "feature"},
            timeout=5,
        ),
    )
    await asyncio.wait_for(channel.changed.wait(), timeout=1)

    event = channel.events[0]
    interaction_id = UUID(event["interaction_id"])
    request = await service.get_request(interaction_id)
    assert request is not None
    assert request.conversation_id == "chat-child"
    assert request.invocation_id == invocation_id
    assert event["agent_id"] == "default"
    assert event["chat_id"] == "chat-child"
    assert event["request_id"] == event["interaction_id"]

    await service.resolve(
        InteractionResponse(
            interaction_id=interaction_id,
            idempotency_key="approve-once",
            expected_revision=1,
            actor=ActorRef(type=ActorType.USER, id="tester"),
            selected_option_ids=("approve",),
            values={"reviewed": True},
        ),
    )

    assert await pending == {
        "action": "approve",
        "data": {"reviewed": True},
    }
    assert (
        await service.list_open(
            agent_id="default",
            conversation_id="chat-child",
        )
        == ()
    )


async def test_confirm_reports_cancelled_invocation(tmp_path: Path) -> None:
    service = InteractionService(tmp_path / "interactions.sqlite3")
    channel = _EventChannel()
    invocation_id = uuid4()
    bridge = UIBridge(
        sse_channel=channel,
        interaction_service=service,
        agent_id="default",
        chat_id="chat-child",
        invocation_id=invocation_id,
    )

    pending = asyncio.create_task(bridge.confirm("Continue?", timeout=5))
    await asyncio.wait_for(channel.changed.wait(), timeout=1)
    await service.cancel_invocation(
        invocation_id,
        detail="test cancellation",
    )

    assert await pending == {"action": "cancel", "data": None}


async def test_unbound_confirm_fails_closed() -> None:
    bridge = UIBridge(sse_channel=_EventChannel())

    try:
        await bridge.confirm("Continue?")
    except RuntimeError as exc:
        assert "task interaction runtime" in str(exc)
    else:
        raise AssertionError("unbound confirmation must fail closed")


class _BoundContext:
    """Task context exposing the OS runtime binding contract."""

    def __init__(self) -> None:
        self.agent_id = "default"
        self.user_id = "tester"
        self.invocation_id = None
        self.cancelled = None

    async def bind_task_runtime(
        self,
        *,
        sse_channel: SSEChannel,
        invocation_id: UUID,
    ) -> str:
        self.invocation_id = invocation_id
        self.channel = sse_channel
        return "chat-child"

    async def _cancel_task_runtime(self, invocation_id: UUID) -> None:
        self.cancelled = invocation_id


async def test_task_cancel_uses_bound_invocation_and_app_scope() -> None:
    manager = TaskManager()
    context = _BoundContext()
    release = asyncio.Event()

    async def handler(_ctx, **_params):
        await release.wait()

    task_id = await manager.create_task("review-app", handler, context, {})
    await asyncio.sleep(0)

    assert manager.get_task(task_id).chat_id == "chat-child"
    assert context.invocation_id == UUID(task_id)
    assert await manager.cancel_task(task_id, app_id="other-app") is False
    assert (
        await manager.cancel_task(
            task_id,
            app_id="review-app",
            agent_id="other-agent",
        )
        is False
    )
    assert (
        await manager.cancel_task(
            task_id,
            app_id="review-app",
            agent_id="default",
            user_id="tester",
        )
        is True
    )
    assert context.cancelled == UUID(task_id)
    assert manager.get_task(task_id).done is True


async def test_task_binding_failure_does_not_leave_visible_record() -> None:
    manager = TaskManager()

    class BrokenContext:
        async def bind_task_runtime(self, **_kwargs):
            raise RuntimeError("interaction store unavailable")

    async def handler(_ctx, **_params):
        raise AssertionError("handler must not run")

    try:
        await manager.create_task("review-app", handler, BrokenContext(), {})
    except RuntimeError as exc:
        assert "interaction store unavailable" in str(exc)
    else:
        raise AssertionError("task binding must fail closed")

    assert not manager._tasks  # pylint: disable=protected-access


async def test_context_binds_explicit_owned_chat_spec() -> None:
    chat = SimpleNamespace(
        id="chat-child",
        session_id="pawapp:review-app:dialogue:child",
        user_id="tester",
        channel="console",
        name="Fork child",
        created_at="created",
        updated_at="updated",
        archived=False,
        pinned=False,
        meta={
            "pawapp": {
                "app_id": "review-app",
                "agent_id": "default",
            },
        },
    )

    class ChatManager:
        async def get_chat(self, chat_id):
            assert chat_id == "chat-child"
            return chat

    workspace = SimpleNamespace(
        chat_manager=ChatManager(),
        interaction_service=object(),
    )

    class Registry:
        async def get_agent(self, agent_id):
            assert agent_id == "default"
            return workspace

    context = PawAppContext(
        app_id="review-app",
        agent_id="default",
        channel="console",
        user_id="tester",
        chat_id="chat-child",
        _workspace_registry=Registry(),
    )

    bound = await context.bind_task_runtime(
        sse_channel=_EventChannel(),
        invocation_id=uuid4(),
    )

    assert bound == "chat-child"


def test_task_chat_binding_accepts_host_chat_and_rejects_foreign_scope() -> (
    None
):
    context = PawAppContext(
        app_id="review-app",
        agent_id="default",
        channel="console",
        user_id="tester",
    )
    host_chat = SimpleNamespace(
        session_id="console:tester",
        user_id="tester",
        channel="console",
        meta={},
    )
    foreign_user = SimpleNamespace(
        session_id="console:other",
        user_id="other",
        channel="console",
        meta={},
    )
    foreign_app = SimpleNamespace(
        session_id="pawapp:other-app",
        user_id="tester",
        channel="console",
        meta={
            "pawapp": {
                "app_id": "other-app",
                "agent_id": "default",
            },
        },
    )

    # pylint: disable=protected-access
    assert context._can_bind_task_chat(host_chat) is True
    assert context._can_bind_task_chat(foreign_user) is False
    assert context._can_bind_task_chat(foreign_app) is False
