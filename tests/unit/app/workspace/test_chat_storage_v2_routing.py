# -*- coding: utf-8 -*-
"""Workspace boundary tests for the normalized Chat rollout path."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from qwenpaw.app.chats.application import (
    AcceptanceReplayError,
    ChatApplicationService,
)
from qwenpaw.app.chats.feature_flags import (
    ChatStorageRollbackBlockedError,
    chat_storage_v2_enabled,
)
from qwenpaw.app import _app as app_module
from qwenpaw.app.chats.storage import (
    ChatStorageCoordinator,
    ChatStoragePaths,
    ConversationCreateRequest,
)
from qwenpaw.app.chats.storage.common import (
    connect_database,
    immediate_transaction,
)
from qwenpaw.app.workspace.workspace import Workspace
from qwenpaw.schemas import (
    AgentRequest,
    AgentResponse,
    Message,
    Role,
    RunStatus,
    TextContent,
)


CONVERSATION_ID = "11111111-1111-4111-8111-111111111111"


def test_chat_storage_v2_is_enabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("QWENPAW_CHAT_STORAGE_V2", raising=False)

    assert chat_storage_v2_enabled()


@pytest.mark.asyncio
async def test_workspace_flag_off_uses_legacy_path_without_v2_files(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWENPAW_CHAT_STORAGE_V2", "0")
    workspace = Workspace("agent", str(tmp_path))

    async def backend(_request):
        yield TextContent(text="legacy", delta=False).completed()

    monkeypatch.setattr(workspace, "_stream_query_backend", backend)
    request = AgentRequest(
        session_id="console:legacy",
        user_id="user",
        input=[],
    )

    events = [item async for item in workspace.stream_query(request)]

    assert events[0].text == "legacy"
    assert workspace.chat_application is None
    assert not ChatStoragePaths(tmp_path).root.exists()


def test_workspace_blocks_legacy_route_after_v2_cutover(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWENPAW_CHAT_STORAGE_V2", "0")
    ChatStorageCoordinator(ChatStoragePaths(tmp_path)).create_conversation(
        ConversationCreateRequest(
            conversation_id=CONVERSATION_ID,
            agent_id="agent",
            owner_scope="console:user",
            display_name="Cut over",
            backend="qwenpaw",
            now_us=1,
        ),
    )
    workspace = Workspace("agent", str(tmp_path))

    with pytest.raises(ChatStorageRollbackBlockedError):
        _ = workspace.chat_application


@pytest.mark.asyncio
async def test_workspace_stop_closes_chat_after_service_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = Workspace("agent", str(tmp_path))
    workspace._started = True  # pylint: disable=protected-access
    calls: list[str] = []

    async def revoke_workspace(_workspace) -> None:
        calls.append("plugins")

    async def stop_services(**_kwargs) -> None:
        calls.append("services")
        raise RuntimeError("service cleanup failed")

    class ChatApplication:
        async def close(self) -> None:
            calls.append("chat")

    monkeypatch.setattr(
        "qwenpaw.plugins.registry.PluginRegistry",
        lambda: SimpleNamespace(
            projector=SimpleNamespace(revoke_workspace=revoke_workspace),
        ),
    )
    monkeypatch.setattr(
        workspace._service_manager,  # pylint: disable=protected-access
        "stop_all",
        stop_services,
    )
    setattr(workspace, "_chat_application", ChatApplication())

    with pytest.raises(RuntimeError, match="service cleanup failed"):
        await workspace.stop()

    assert calls == ["plugins", "services", "chat"]
    assert getattr(workspace, "_chat_application") is None


@pytest.mark.asyncio
async def test_startup_blocks_legacy_scroll_sync_after_v2_cutover(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWENPAW_CHAT_STORAGE_V2", "0")
    ChatStorageCoordinator(ChatStoragePaths(tmp_path)).create_conversation(
        ConversationCreateRequest(
            conversation_id=CONVERSATION_ID,
            agent_id="agent",
            owner_scope="console:user",
            display_name="Cut over",
            backend="qwenpaw",
            now_us=1,
        ),
    )
    monkeypatch.setattr(
        "qwenpaw.config.utils.get_agent_dirs",
        lambda: [tmp_path],
    )

    with pytest.raises(ChatStorageRollbackBlockedError):
        # pylint: disable-next=protected-access
        await app_module._sync_scroll_history_on_startup()


@pytest.mark.asyncio
async def test_workspace_commits_terminal_before_exposing_response(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWENPAW_CHAT_STORAGE_V2", "1")
    monkeypatch.setattr(
        "qwenpaw.app.workspace.workspace.load_agent_config",
        lambda _agent_id: SimpleNamespace(backend="qwenpaw"),
    )
    paths = ChatStoragePaths(tmp_path)
    ChatStorageCoordinator(paths).create_conversation(
        ConversationCreateRequest(
            conversation_id=CONVERSATION_ID,
            agent_id="agent",
            owner_scope="console:user",
            display_name="Routing",
            backend="qwenpaw",
            now_us=1,
        ),
    )
    with connect_database(paths.catalog) as connection:
        with immediate_transaction(connection):
            connection.execute(
                "INSERT INTO chat_alias_index("
                "agent_id, alias_namespace, external_key, owner_scope, "
                "conversation_id, source_generation, created_at_us, "
                "updated_at_us) "
                "VALUES ('agent', 'legacy_session', 'console:session', "
                "'console:user', ?, 1, 1, 1)",
                (CONVERSATION_ID,),
            )

    workspace = Workspace("agent", str(tmp_path))
    monkeypatch.setattr(
        workspace,
        "_chat_application",
        ChatApplicationService(
            tmp_path,
            process_instance_id="process",
        ),
    )

    async def backend(request):
        message = Message(id="assistant", role=Role.ASSISTANT)
        message.object = "message"
        yield message
        delta = TextContent(text="persisted", delta=True)
        delta.msg_id = message.id
        yield delta
        request.chat_runtime_context = b'{"state":"saved"}'
        response = AgentResponse(status=RunStatus.Completed)
        response.object = "response"
        yield response

    monkeypatch.setattr(workspace, "_stream_query_backend", backend)
    request = AgentRequest(
        session_id="console:session",
        user_id="user",
        input=[
            Message(
                id="request",
                role=Role.USER,
                content=[TextContent(text="prompt").completed()],
                status=RunStatus.Completed,
            ),
        ],
    )
    request.channel = "console"

    events = [item async for item in workspace.stream_query(request)]

    assert getattr(events[-1], "object", None) == "response"
    transcript = await workspace.chat_application.load_transcript(
        CONVERSATION_ID,
        backend="qwenpaw",
    )
    assert [item.content[0].text for item in transcript] == [
        "prompt",
        "persisted",
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
    with connect_database(paths.catalog, readonly=True) as connection:
        assert (
            connection.execute(
                "SELECT state FROM chat_runtime_intents",
            ).fetchone()[0]
            == "terminal"
        )
    live = await workspace.task_tracker.chat_live_hub.subscribe(
        CONVERSATION_ID,
    )
    assert live.sync.terminal is True
    assert live.sync.cursor is not None
    assert live.sync.cursor.event_sequence == 3
    assert (view := live.sync.view) is not None and (
        view["run_state"],
        view["blocks"],
        len(view["messages"]),
        view["messages"][0]["message_id"],
        view["messages"][0]["message"]["role"],
        view["messages"][0]["message"]["content"][0]["text"],
    ) == ("completed", (), 1, "request", "user", "prompt")
    assert await live.queue.get() is None

    with pytest.raises(AcceptanceReplayError):
        _ = [item async for item in workspace.stream_query(request)]
    replayed_transcript = await workspace.chat_application.load_transcript(
        CONVERSATION_ID,
        backend="qwenpaw",
    )
    assert len(replayed_transcript) == 2
    with connect_database(paths.catalog, readonly=True) as connection:
        assert (
            connection.execute(
                "SELECT state FROM chat_runtime_intents",
            ).fetchone()[0]
            == "terminal"
        )
