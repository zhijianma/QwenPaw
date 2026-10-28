# -*- coding: utf-8 -*-
"""Envelope-to-Chat persistence contract tests."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from qwenpaw.app.chats import event_materializer as event_module
from qwenpaw.app.chats.event_materializer import ChatEventMaterializer
from qwenpaw.app.chats.storage import (
    AcceptanceRequest,
    ChatStorageCoordinator,
    ChatStoragePaths,
    ConversationCreateRequest,
    accept_input,
    load_runtime_run_handle,
    read_transcript,
    reserve_recall_sequences,
)
from qwenpaw.app.chats.storage.common import connect_database
from qwenpaw.schemas import (
    AgentResponse,
    DataContent,
    FunctionCall,
    FunctionCallOutput,
    ImageContent,
    Message,
    MessageType,
    Role,
    RunStatus,
    TextContent,
)


CONVERSATION_ID = "11111111-1111-4111-8111-111111111111"


def _materializer(tmp_path: Path, *, include_initial: bool = False):
    paths = ChatStoragePaths(tmp_path)
    ChatStorageCoordinator(paths).create_conversation(
        ConversationCreateRequest(
            conversation_id=CONVERSATION_ID,
            agent_id="agent",
            owner_scope="console:user",
            display_name="Events",
            backend="qwenpaw",
            now_us=1,
        ),
    )
    lease = reserve_recall_sequences(
        paths.catalog,
        workspace_id=str(tmp_path.resolve()),
        count=1,
        lease_owner="process",
        now_us=2,
    )
    assert lease is not None
    prompt = Message(
        id="user-message",
        role=Role.USER,
        content=[TextContent(text="prompt").completed()],
        status=RunStatus.Completed,
    )
    result = accept_input(
        paths.conversation(CONVERSATION_ID),
        AcceptanceRequest(
            conversation_id=CONVERSATION_ID,
            client_request_id="request",
            message=prompt,
            process_instance_id="process",
            now_us=2,
        ),
        recall_sequences=lease.values(),
    )
    handle = load_runtime_run_handle(
        paths.conversation(CONVERSATION_ID),
        conversation_id=CONVERSATION_ID,
        run_id=result.work_id,
    )
    materializer = ChatEventMaterializer(
        paths,
        handle,
        lease_owner="process",
        initial_message=prompt if include_initial else None,
    )
    return paths, materializer


def test_live_view_starts_with_the_accepted_user_message(
    tmp_path: Path,
) -> None:
    _paths, materializer = _materializer(
        tmp_path,
        include_initial=True,
    )

    messages = materializer.live_view["messages"]

    assert len(messages) == 1
    assert messages[0]["message_id"] == "user-message"
    assert messages[0]["deferred"] is False
    assert messages[0]["message"]["role"] == "user"
    assert messages[0]["message"]["content"][0]["text"] == "prompt"


@pytest.mark.asyncio
async def test_stream_terminal_is_durable_before_observe_returns(
    tmp_path: Path,
) -> None:
    paths, materializer = _materializer(tmp_path)
    started = Message(
        id="assistant-message",
        role=Role.ASSISTANT,
        type=MessageType.MESSAGE,
    )
    started.object = "message"
    delta = TextContent(text="durable delta", delta=True, index=0)
    delta.msg_id = started.id
    terminal = AgentResponse(status=RunStatus.Completed)
    terminal.object = "response"

    await materializer.observe(started)
    await materializer.observe(delta)
    committed = await materializer.observe(
        terminal,
        runtime_context=b'{"context":"saved"}',
    )

    assert committed is True
    transcript = read_transcript(paths.conversation(CONVERSATION_ID))
    assert [item.content[0].text for item in transcript] == [
        "prompt",
        "durable delta",
    ]
    with connect_database(
        paths.conversation(CONVERSATION_ID),
        readonly=True,
    ) as connection:
        assert (
            connection.execute(
                "SELECT state FROM chat_runs",
            ).fetchone()[0]
            == "completed"
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM chat_runtime_blobs",
            ).fetchone()[0]
            == 1
        )


@pytest.mark.asyncio
async def test_clear_terminal_commits_clear_context_revision(
    tmp_path: Path,
) -> None:
    paths, materializer = _materializer(tmp_path)
    terminal = AgentResponse(status=RunStatus.Completed)
    terminal.object = "response"

    committed = await materializer.observe(
        terminal,
        runtime_context=b'{"state":{"context":[]}}',
        context_kind="clear",
    )

    assert committed is True
    with connect_database(
        paths.conversation(CONVERSATION_ID),
        readonly=True,
    ) as connection:
        row = connection.execute(
            "SELECT cr.kind, cr.anchor_turn_id FROM "
            "chat_context_revisions AS cr JOIN chat_turns AS t ON "
            "t.context_revision_id = cr.context_revision_id "
            "WHERE cr.kind = 'clear'",
        ).fetchone()
    assert row["kind"] == "clear"
    assert row["anchor_turn_id"]


@pytest.mark.asyncio
async def test_live_result_keeps_threshold_tail_in_immutable_overlay(
    tmp_path: Path,
) -> None:
    _paths, materializer = _materializer(tmp_path)
    started = Message(
        id="assistant-message",
        role=Role.ASSISTANT,
        type=MessageType.MESSAGE,
    )
    started.object = "message"
    delta = TextContent(text="pending", delta=True, index=0)
    delta.msg_id = started.id

    state = await materializer.observe_live(started)
    result = await materializer.observe_live(delta)

    assert state.mutations == ()
    assert result.terminal is False
    assert len(result.mutations) == 1
    mutation = result.mutations[0]
    assert mutation.kind == "block_append"
    assert mutation.expected_offset == 0
    assert mutation.entity_version == 1
    assert mutation.payload["text"] == "pending"
    assert mutation.payload["index"] == 0
    assert isinstance(mutation.payload["message_id"], str)
    block = result.view["blocks"][0]
    normalized_message_id = block["message_id"]
    assert block["durable_bytes"] == 0
    assert block["total_bytes"] == 7
    assert block["pending"] == (
        {
            "start_byte": 0,
            "entity_version": 1,
            "text": "pending",
        },
    )

    completed = Message(
        id=started.id,
        role=Role.ASSISTANT,
        type=MessageType.MESSAGE,
        content=[TextContent(text="pending", index=0).completed()],
        status=RunStatus.Completed,
    )
    completed.object = "message"
    completed_result = await materializer.observe_live(completed)
    assert completed_result.view["blocks"] == ()
    assert len(completed_result.view["messages"]) == 1
    live_message = completed_result.view["messages"][0]
    assert live_message["message_id"] == normalized_message_id
    assert live_message["deferred"] is False
    assert live_message["message"]["id"] == normalized_message_id
    assert completed_result.mutations[0].kind == "entity_state"
    assert completed_result.mutations[0].entity_id == normalized_message_id
    assert completed_result.mutations[0].payload["live_message"] == (
        live_message
    )


@pytest.mark.asyncio
async def test_oversized_completed_message_uses_bounded_live_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _paths, materializer = _materializer(tmp_path)
    monkeypatch.setattr(event_module, "LIVE_MESSAGE_MAX_BYTES", 32)
    completed = Message(
        id="large-message",
        role=Role.ASSISTANT,
        content=[TextContent(text="x" * 128).completed()],
        status=RunStatus.Completed,
    )
    completed.object = "message"

    result = await materializer.observe_live(completed)

    live_message = result.view["messages"][0]
    assert live_message["deferred"] is True
    assert live_message["message"] is None
    assert live_message["encoded_bytes"] == 32
    assert result.mutations[0].payload["live_message"] == live_message


@pytest.mark.asyncio
async def test_completed_tool_message_flushes_immediately(
    tmp_path: Path,
) -> None:
    paths, materializer = _materializer(tmp_path)
    tool_message = Message(
        id="tool-call",
        role=Role.ASSISTANT,
        type=MessageType.PLUGIN_CALL,
        content=[
            DataContent(
                data=FunctionCall(
                    call_id="call-1",
                    name="search",
                    arguments='{"query":"qwenpaw"}',
                ).model_dump(),
            ).completed(),
        ],
        status=RunStatus.Completed,
    )
    tool_message.object = "message"

    tool_live = await materializer.observe_live(tool_message)
    result_message = Message(
        id="tool-result",
        role=Role.TOOL,
        type=MessageType.PLUGIN_CALL_OUTPUT,
        content=[
            DataContent(
                data=FunctionCallOutput(
                    call_id="call-1",
                    name="search",
                    output="result",
                ).model_dump(),
            ).completed(),
        ],
        status=RunStatus.Completed,
    )
    result_message.object = "message"
    result_live = await materializer.observe_live(result_message)

    assert [item["message_id"] for item in result_live.view["messages"]] == [
        tool_live.mutations[0].entity_id,
        result_live.mutations[0].entity_id,
    ]

    with connect_database(
        paths.conversation(CONVERSATION_ID),
        readonly=True,
    ) as connection:
        row = connection.execute(
            "SELECT m.state, b.kind, b.total_bytes "
            "FROM chat_messages AS m JOIN chat_blocks AS b "
            "ON b.message_id = m.message_id WHERE m.role = 'assistant'",
        ).fetchone()
        assert row["state"] == "completed"
        assert row["kind"] == "data"
        assert row["total_bytes"] > 0
        link = connection.execute(
            "SELECT state, result_block_id FROM chat_tool_links",
        ).fetchone()
        assert link["state"] == "completed"
        assert link["result_block_id"] is not None


@pytest.mark.asyncio
async def test_streamed_multimodal_message_keeps_one_message(
    tmp_path: Path,
) -> None:
    paths, materializer = _materializer(tmp_path)
    started = Message(
        id="multimodal",
        role=Role.ASSISTANT,
        type=MessageType.MESSAGE,
    )
    started.object = "message"
    delta = TextContent(text="streamed", delta=True, index=0)
    delta.msg_id = started.id
    completed = Message(
        id=started.id,
        role=Role.ASSISTANT,
        type=MessageType.MESSAGE,
        content=[
            TextContent(text="streamed", index=0).completed(),
            ImageContent(
                image_url="https://example.invalid/image.png",
            ).completed(),
        ],
        status=RunStatus.Completed,
    )
    completed.object = "message"
    terminal = AgentResponse(status=RunStatus.Completed)
    terminal.object = "response"

    await materializer.observe(started)
    await materializer.observe(delta)
    await materializer.observe(completed)
    await materializer.observe(terminal)

    transcript = read_transcript(paths.conversation(CONVERSATION_ID))
    assert len(transcript) == 2
    assert [block.type.value for block in transcript[-1].content] == [
        "text",
        "image",
    ]
    assert transcript[-1].content[0].text == "streamed"


@pytest.mark.asyncio
async def test_multiple_streamed_blocks_share_one_message(
    tmp_path: Path,
) -> None:
    paths, materializer = _materializer(tmp_path)
    started = Message(id="multi-block", role=Role.ASSISTANT)
    started.object = "message"
    first = TextContent(text="first", delta=True, index=0)
    first.msg_id = started.id
    second = TextContent(text="second", delta=True, index=1)
    second.msg_id = started.id
    completed = Message(
        id=started.id,
        role=Role.ASSISTANT,
        content=[
            TextContent(text="first", index=0).completed(),
            TextContent(text="second", index=1).completed(),
        ],
        status=RunStatus.Completed,
    )
    completed.object = "message"
    terminal = AgentResponse(status=RunStatus.Completed)
    terminal.object = "response"

    await materializer.observe(started)
    await materializer.observe(first)
    await materializer.observe(second)
    await materializer.observe(completed)
    await materializer.observe(terminal)

    transcript = read_transcript(paths.conversation(CONVERSATION_ID))
    assert len(transcript) == 2
    assert [block.text for block in transcript[-1].content] == [
        "first",
        "second",
    ]
    with connect_database(
        paths.conversation(CONVERSATION_ID),
        readonly=True,
    ) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM chat_messages "
                "WHERE role = 'assistant'",
            ).fetchone()[0]
            == 1
        )


@pytest.mark.asyncio
async def test_abort_maps_cancellation_to_interrupted_entities(
    tmp_path: Path,
) -> None:
    paths, materializer = _materializer(tmp_path)
    started = Message(id="message", role=Role.ASSISTANT)
    started.object = "message"
    delta = TextContent(text="partial", delta=True)
    delta.msg_id = started.id
    await materializer.observe(started)
    await materializer.observe(delta)

    await materializer.abort(asyncio.CancelledError())

    with connect_database(
        paths.conversation(CONVERSATION_ID),
        readonly=True,
    ) as connection:
        assert (
            connection.execute(
                "SELECT state FROM chat_runs",
            ).fetchone()[0]
            == "canceled"
        )
        assert (
            connection.execute(
                "SELECT b.state FROM chat_blocks AS b "
                "JOIN chat_messages AS m ON m.message_id = b.message_id "
                "WHERE b.kind = 'text' AND m.role = 'assistant'",
            ).fetchone()[0]
            == "interrupted"
        )


@pytest.mark.asyncio
async def test_terminal_commit_survives_caller_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, materializer = _materializer(tmp_path)
    started = threading.Event()
    release = threading.Event()
    original_finalize = event_module.finalize_run

    def blocked_finalize(*args, **kwargs):
        started.set()
        assert release.wait(timeout=5)
        return original_finalize(*args, **kwargs)

    monkeypatch.setattr(event_module, "finalize_run", blocked_finalize)
    task = asyncio.create_task(
        materializer.finalize(
            state="completed",
            runtime_context=None,
        ),
    )
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert materializer.terminal_state == "completed"
    await materializer.abort(asyncio.CancelledError())
    with connect_database(
        paths.conversation(CONVERSATION_ID),
        readonly=True,
    ) as connection:
        assert (
            connection.execute(
                "SELECT state FROM chat_runs",
            ).fetchone()[0]
            == "completed"
        )


@pytest.mark.asyncio
async def test_abort_marks_inflight_tool_failed(tmp_path: Path) -> None:
    paths, materializer = _materializer(tmp_path)
    tool_message = Message(
        id="tool-call",
        role=Role.ASSISTANT,
        type=MessageType.PLUGIN_CALL,
        content=[
            DataContent(
                data=FunctionCall(
                    call_id="call-1",
                    name="search",
                    arguments="{}",
                ).model_dump(),
            ).completed(),
        ],
        status=RunStatus.Completed,
    )
    tool_message.object = "message"
    await materializer.observe(tool_message)

    await materializer.abort(RuntimeError("tool failed"))

    with connect_database(
        paths.conversation(CONVERSATION_ID),
        readonly=True,
    ) as connection:
        assert (
            connection.execute(
                "SELECT state FROM chat_tool_links",
            ).fetchone()[0]
            == "failed"
        )


@pytest.mark.asyncio
async def test_terminal_source_commit_survives_projection_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, materializer = _materializer(tmp_path)
    terminal = AgentResponse(status=RunStatus.Completed)
    terminal.object = "response"

    def fail_projection(*_args, **_kwargs) -> None:
        raise OSError("recall projection unavailable")

    monkeypatch.setattr(
        "qwenpaw.app.chats.event_materializer.project_conversation",
        fail_projection,
    )

    assert await materializer.observe(terminal) is True
    with connect_database(
        paths.conversation(CONVERSATION_ID),
        readonly=True,
    ) as connection:
        assert (
            connection.execute(
                "SELECT state FROM chat_runs",
            ).fetchone()[0]
            == "completed"
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM chat_projection_outbox "
                "WHERE state = 'pending'",
            ).fetchone()[0]
            > 0
        )
    with connect_database(paths.catalog, readonly=True) as connection:
        repair = connection.execute(
            "SELECT expected_generation, state FROM chat_catalog_repairs",
        ).fetchone()
        assert tuple(repair) == (3, "pending")


@pytest.mark.asyncio
async def test_terminal_commit_survives_repair_queue_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, materializer = _materializer(tmp_path)
    terminal = AgentResponse(status=RunStatus.Completed)
    terminal.object = "response"

    def fail(*_args, **_kwargs) -> None:
        raise OSError("storage unavailable")

    monkeypatch.setattr(
        "qwenpaw.app.chats.event_materializer.project_conversation",
        fail,
    )
    monkeypatch.setattr(
        "qwenpaw.app.chats.event_materializer.enqueue_projection_repair",
        fail,
    )

    assert await materializer.observe(terminal) is True
    with connect_database(
        paths.conversation(CONVERSATION_ID),
        readonly=True,
    ) as connection:
        assert (
            connection.execute(
                "SELECT state FROM chat_runs",
            ).fetchone()[0]
            == "completed"
        )
