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
    CONSOLE_INTERACTION_CONTINUATION_ENVELOPE,
    CONSOLE_SUBMISSION_ENVELOPE,
    WorkspaceChatSubmissionDispatcher,
)
from qwenpaw.app.task_tracker import TaskTracker
from qwenpaw.invocation_control import (
    InvocationControlService,
    SQLiteInvocationControl,
)
from qwenpaw.interactions import InteractionService
from qwenpaw.kernel import (
    ActorRef,
    ActorType,
    ContinuationDispatchStatus,
    ContinuationMode,
    InteractionKind,
    InteractionMode,
    InteractionOption,
    InteractionRequest,
    InteractionResponse,
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


@pytest.mark.asyncio
async def test_workspace_dispatcher_recovers_interaction_continuation(
    tmp_path: Path,
) -> None:
    """A committed answer survives a crash before Submission enqueue."""
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
    interaction_path = tmp_path / "interactions.sqlite3"
    interactions = InteractionService(interaction_path)
    correlation_id = uuid4()
    request = InteractionRequest(
        kind=InteractionKind.USER_INPUT,
        mode=InteractionMode.BLOCKING,
        agent_id="default",
        conversation_id=chat.id,
        invocation_id=uuid4(),
        correlation_id=correlation_id,
        continuation_mode=ContinuationMode.CONVERSATION_TURN,
        title="Choose output",
        prompt="Which format?",
        options=(
            InteractionOption(option_id="md", label="Markdown"),
            InteractionOption(option_id="html", label="HTML"),
        ),
    )
    await interactions.open(request)
    await interactions.resolve(
        InteractionResponse(
            interaction_id=request.interaction_id,
            idempotency_key="answer-1",
            expected_revision=1,
            actor=ActorRef(type=ActorType.USER, id="local-user"),
            selected_option_ids=("html",),
        ),
    )
    await interactions.close()

    observed: list[dict] = []

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

    restarted = InteractionService(interaction_path)
    workspace = SimpleNamespace(
        agent_id="default",
        chat_manager=manager,
        channel_manager=ChannelManager(),
        task_tracker=TaskTracker(),
        interaction_service=restarted,
    )
    dispatcher = WorkspaceChatSubmissionDispatcher(
        workspace=workspace,
        control=control,
    )

    original_mark = restarted.mark_continuation_dispatched

    async def fail_after_enqueue(*_args, **_kwargs):
        raise RuntimeError("simulated crash after enqueue")

    restarted.mark_continuation_dispatched = fail_after_enqueue
    # White-box drain isolates the crash window between two durable stores.
    # pylint: disable=protected-access
    with pytest.raises(RuntimeError, match="simulated crash"):
        await dispatcher._dispatch_ready_continuations()
    restarted.mark_continuation_dispatched = original_mark
    await dispatcher._dispatch_ready_continuations()
    # pylint: enable=protected-access

    await dispatcher.start()
    async with asyncio.timeout(2):
        while not observed:
            await asyncio.sleep(0.01)

    [payload] = observed
    context = payload["meta"]["request_context"]
    assert payload["content_parts"] == [
        {
            "type": "text",
            "text": (
                "User response to QwenPaw interaction "
                f"{request.interaction_id}: HTML"
            ),
        },
    ]
    assert context["interaction_id"] == str(request.interaction_id)
    assert context["os_correlation_id"] == str(correlation_id)
    submission = await control.get_submission(
        UUID(context["os_submission_id"]),
    )
    assert submission is not None
    assert submission.input_envelope is not None
    assert (
        submission.input_envelope.kind
        == CONSOLE_INTERACTION_CONTINUATION_ENVELOPE
    )
    assert submission.correlation_id == correlation_id
    continuations = await restarted.list_ready_continuations(
        agent_id="default",
    )
    assert not continuations
    dispatched = await restarted.mark_continuation_dispatched(
        request.interaction_id,
        submission.submission_id,
    )
    assert dispatched.status is ContinuationDispatchStatus.DISPATCHED

    dispatcher.wake_continuations()
    await asyncio.sleep(0.05)
    assert len(observed) == 1
    await dispatcher.stop()
    await control.close()
