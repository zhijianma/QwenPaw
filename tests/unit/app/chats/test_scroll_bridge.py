# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Scroll compatibility bridge tests for normalized Chat storage."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from qwenpaw.agents.context import build_scroll_components
from qwenpaw.agents.context.types import LogEntry
from qwenpaw.app.chats.application import ChatApplicationService
from qwenpaw.app.chats.event_materializer import ChatEventMaterializer
from qwenpaw.app.chats.scroll_bridge import ChatRecallHistoryStore
from qwenpaw.app.chats.storage import (
    ChatStorageCoordinator,
    ChatStoragePaths,
    ConversationCreateRequest,
    load_runtime_run_handle,
)
from qwenpaw.app.chats.storage.common import (
    connect_database,
    immediate_transaction,
)
from qwenpaw.schemas import (
    AgentResponse,
    Message,
    Role,
    RunStatus,
    TextContent,
)
from qwenpaw.config.config import LightContextConfig


CONVERSATION_ID = "11111111-1111-4111-8111-111111111111"


async def test_scroll_bridge_uses_recall_projection_not_history_db(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = ChatStoragePaths(tmp_path)
    ChatStorageCoordinator(paths).create_conversation(
        ConversationCreateRequest(
            conversation_id=CONVERSATION_ID,
            agent_id="agent",
            owner_scope="console:user",
            display_name="Scroll",
            backend="qwenpaw",
            now_us=1,
        ),
    )
    with connect_database(paths.catalog) as connection:
        with immediate_transaction(connection):
            connection.execute(
                "INSERT INTO chat_alias_index("
                "agent_id, alias_namespace, external_key, owner_scope, "
                "conversation_id, created_at_us, updated_at_us) "
                "VALUES ('agent', 'legacy_session', 'console:scroll', "
                "'console:user', ?, 1, 1)",
                (CONVERSATION_ID,),
            )
    service = ChatApplicationService(tmp_path, process_instance_id="process")
    accepted = await service.accept_input(
        conversation_id=CONVERSATION_ID,
        client_request_id="request",
        message=Message(
            id="user-message",
            role=Role.USER,
            content=[TextContent(text="prompt").completed()],
            status=RunStatus.Completed,
        ),
        now_us=2,
    )
    monkeypatch.setenv("QWENPAW_CHAT_STORAGE_V2", "1")
    components = build_scroll_components(
        agent_config=SimpleNamespace(
            running=SimpleNamespace(
                light_context_config=LightContextConfig(strategy="scroll"),
            ),
        ),
        workspace_dir=str(tmp_path),
        model=object(),
        session_id="console:scroll",
        agent_id="agent",
    )
    assert components is not None
    history = components.context_manager._history
    assert isinstance(
        history,
        ChatRecallHistoryStore,
    )
    components.context_manager.close()
    bridge = ChatRecallHistoryStore.resolve(
        tmp_path,
        session_id="console:scroll",
        agent_id="agent",
    )

    sequence = bridge.append(
        session_id="console:scroll",
        agent_id="agent",
        dedup_key="assistant-message",
        entry=LogEntry(
            kind="model_turn",
            role="assistant",
            content="provisional answer",
        ),
    )
    replay = bridge.append(
        session_id="console:scroll",
        agent_id="agent",
        dedup_key="assistant-message",
        entry=LogEntry(kind="model_turn", content="ignored duplicate"),
    )

    assert replay == sequence
    assert bridge.path == paths.recall
    assert not (tmp_path / "history.db").exists()
    assert bridge.contents_by_seqs({sequence}) == {
        sequence: "provisional answer",
    }
    assert bridge.purge(before="2100-01-01") == 0
    with connect_database(paths.recall, readonly=True) as connection:
        row = connection.execute(
            "SELECT provisional, dedup_key FROM conversation_history "
            "WHERE seq = ?",
            (sequence,),
        ).fetchone()
        assert tuple(row) == (1, "assistant-message")

    handle = load_runtime_run_handle(
        paths.conversation(CONVERSATION_ID),
        conversation_id=CONVERSATION_ID,
        run_id=accepted.work_id,
    )
    materializer = ChatEventMaterializer(
        paths,
        handle,
        lease_owner="process",
        session_id="console:scroll",
    )
    started = Message(id="assistant-message", role=Role.ASSISTANT)
    started.object = "message"
    delta = TextContent(text="final answer", delta=True)
    delta.msg_id = started.id
    terminal = AgentResponse(status=RunStatus.Completed)
    terminal.object = "response"
    await materializer.observe(started)
    await materializer.observe(delta)
    await materializer.observe(terminal)

    with connect_database(paths.recall, readonly=True) as connection:
        rows = connection.execute(
            "SELECT seq, content, provisional FROM conversation_history "
            "WHERE dedup_key = 'assistant-message'",
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            (sequence, "final answer", 0),
        ]
