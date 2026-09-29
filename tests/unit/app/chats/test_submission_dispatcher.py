# -*- coding: utf-8 -*-
"""Tests for the workspace Console submission adapter."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from qwenpaw.app.chats.manager import ChatManager
from qwenpaw.app.chats.models import ChatSpec
from qwenpaw.app.chats.repo import JsonChatRepository
from qwenpaw.app.chats.submission_dispatcher import (
    CONSOLE_SUBMISSION_ENVELOPE,
    WorkspaceChatSubmissionDispatcher,
)
from qwenpaw.app.task_tracker import TaskTracker
from qwenpaw.invocation_control import (
    InvocationControlService,
    SQLiteInvocationControl,
)
from qwenpaw.kernel import (
    SubmissionInputEnvelope,
    SubmissionStatus,
    TurnSubmissionRequest,
)


@pytest.mark.asyncio
async def test_workspace_dispatcher_executes_without_http_subscriber(
    tmp_path: Path,
) -> None:
    manager = ChatManager(
        repo=JsonChatRepository(tmp_path / "chats.json"),
    )
    chat = await manager.create_chat(
        ChatSpec(
            id="chat-1",
            session_id="console:chat-1",
            user_id="local-user",
            channel="console",
        ),
    )
    control = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    observed: list[dict] = []
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    class ConsoleChannel:
        async def stream_one(self, payload):
            observed.append(payload)
            context = payload["meta"]["request_context"]
            lease = await control.begin_submitted_turn(
                UUID(context["os_submission_id"]),
                invocation_id=uuid4(),
                agent_id="default",
                conversation_id=context["os_conversation_id"],
            )
            yield 'data: {"status":"running"}\n\n'
            await control.finish_turn(lease, SubmissionStatus.SUCCEEDED)

    class ChannelManager:
        @staticmethod
        async def get_channel(name: str):
            return ConsoleChannel() if name == "console" else None

    workspace = SimpleNamespace(
        agent_id="default",
        chat_manager=manager,
        channel_manager=ChannelManager(),
        task_tracker=TaskTracker(),
    )
    dispatcher = WorkspaceChatSubmissionDispatcher(
        workspace=workspace,
        control=control,
    )
    request = TurnSubmissionRequest(
        agent_id="default",
        conversation_id=chat.id,
        content="run independently",
        input_envelope=SubmissionInputEnvelope(
            kind=CONSOLE_SUBMISSION_ENVELOPE,
            payload={
                "channel_id": "console",
                "sender_id": "local-user",
                "content_parts": [
                    {"type": "text", "text": "run independently"},
                ],
                "message_id": "client-message-1",
                "message_metadata": {},
                "meta": {
                    "session_id": "console:chat-1",
                    "user_id": "local-user",
                    "request_context": {
                        "session_project_dirs": [
                            {
                                "path": str(project_dir),
                                "label": "Project",
                            },
                        ],
                    },
                },
            },
        ),
        idempotency_key="client-message-1",
    )

    await dispatcher.start()
    receipt = await dispatcher.enqueue(request, expected_revision=0)
    assert receipt.submission_id is not None
    async with asyncio.timeout(2):
        while True:
            record = await control.get_submission(receipt.submission_id)
            if (
                record is not None
                and record.status is SubmissionStatus.SUCCEEDED
            ):
                break
            await asyncio.sleep(0.01)

    assert len(observed) == 1
    context = observed[0]["meta"]["request_context"]
    assert context["os_submission_id"] == str(receipt.submission_id)
    assert context["os_conversation_id"] == chat.id
    assert context["os_submission_idempotency_key"] == "client-message-1"
    assert "session_project_dirs" not in context
    stored_chat = await manager.get_chat(chat.id)
    assert stored_chat is not None
    assert stored_chat.meta["runtime_context"]["project_dirs"] == [
        {"path": str(project_dir), "label": "Project"},
    ]
    assert await workspace.task_tracker.get_status(chat.id) == "idle"
    await dispatcher.stop()
    await control.close()
