# -*- coding: utf-8 -*-
"""Ownership-boundary tests for the global chat API."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from qwenpaw.app.chats import api as chats_api
from qwenpaw.app.chats.api import (
    ChatCopyCreateRequest,
    get_chat,
    get_chat_rollout_status,
    get_chat_status,
    list_chats,
    post_chat_copy,
)
from qwenpaw.app.chats.live import ChatLiveHub, ChatLivePath
from qwenpaw.app.chats.pagination_cursor import InvalidChatCursorError
from qwenpaw.app.chats.storage.pagination_repository import (
    ChatPage,
    ChatPageView,
    ChatViewInvalidatedError,
)
from qwenpaw.app.chats.models import ChatSpec
from qwenpaw.schemas import Message, Role, RunStatus, TextContent


def _chat(chat_id: str, *, app_id: str | None = None) -> ChatSpec:
    meta = (
        {
            "pawapp": {
                "app_id": app_id,
                "agent_id": "datapaw",
            },
        }
        if app_id
        else {}
    )
    return ChatSpec(
        id=chat_id,
        session_id=f"console:{chat_id}",
        user_id="default",
        channel="console",
        meta=meta,
    )


@pytest.mark.asyncio
async def test_chat_copy_creates_an_independent_chat_spec():
    source = _chat("source-chat")
    source.name = "Research"
    calls = []
    published = []

    async def copy_chat(*_args, **_kwargs):
        calls.append("copy")

    async def create_chat(_target):
        calls.append("publish")
        published.append(_target)
        return _target.model_copy(update={"group_id": "default"})

    application = SimpleNamespace(
        copy_chat=AsyncMock(side_effect=copy_chat),
        discard_unpublished_chat=AsyncMock(),
    )
    manager = SimpleNamespace(
        get_chat=AsyncMock(return_value=source),
        list_chats=AsyncMock(return_value=[source]),
        create_chat=AsyncMock(side_effect=create_chat),
        delete_chats=AsyncMock(),
    )
    workspace = SimpleNamespace(
        chat_application=application,
        config=SimpleNamespace(backend="qwenpaw"),
    )

    result = await post_chat_copy(
        "source-chat",
        ChatCopyCreateRequest(source_turn_id="turn-7"),
        mgr=manager,
        workspace=workspace,
    )

    assert result.id != source.id
    assert result.name == "Research (2)"
    assert result.session_id == f"copy:{result.id}"
    assert result.group_id == "default"
    assert result.meta == {
        "copy_source_chat_id": "source-chat",
        "copy_source_turn_id": "turn-7",
        "copy_root_chat_id": "source-chat",
        "copy_base_name": "Research",
    }
    assert calls == ["copy", "publish"]
    manager.create_chat.assert_awaited_once_with(published[0])
    assert published[0].group_id is None
    application.copy_chat.assert_awaited_once_with(
        "source-chat",
        target_conversation_id=result.id,
        target_session_id=result.session_id,
        source_turn_id="turn-7",
        display_name="Research (2)",
        group_id=source.group_id,
        backend="qwenpaw",
    )


@pytest.mark.asyncio
async def test_chat_copy_discards_physical_copy_when_spec_publish_fails():
    source = _chat("source-chat")
    application = SimpleNamespace(
        copy_chat=AsyncMock(),
        discard_unpublished_chat=AsyncMock(),
    )
    manager = SimpleNamespace(
        get_chat=AsyncMock(return_value=source),
        list_chats=AsyncMock(return_value=[source]),
        create_chat=AsyncMock(side_effect=OSError("catalog unavailable")),
    )
    workspace = SimpleNamespace(
        chat_application=application,
        config=SimpleNamespace(backend="qwenpaw"),
    )

    with pytest.raises(OSError, match="catalog unavailable"):
        await post_chat_copy(
            "source-chat",
            ChatCopyCreateRequest(source_turn_id="turn-7"),
            mgr=manager,
            workspace=workspace,
        )

    target_id = application.copy_chat.await_args.kwargs[
        "target_conversation_id"
    ]
    application.discard_unpublished_chat.assert_awaited_once_with(target_id)


def test_next_copy_display_name_uses_the_highest_existing_number():
    source = _chat("source-chat")
    source.name = "Research"
    copy_two = _chat("copy-two")
    copy_two.name = "Research (2)"
    copy_four = _chat("copy-four")
    copy_four.name = "Research (4)"
    unrelated = _chat("unrelated")
    unrelated.name = "Other (99)"

    assert (
        chats_api._next_copy_display_name(  # pylint: disable=protected-access
            source.name,
            [source, copy_two, copy_four, unrelated],
        )
        == "Research (5)"
    )


def test_next_copy_display_name_keeps_the_chat_name_limit():
    source = _chat("source-chat")
    source.name = "R" * 500

    result = (
        chats_api._next_copy_display_name(  # pylint: disable=protected-access
            source.name,
            [source],
        )
    )

    assert len(result) == 500
    assert result.endswith(" (2)")


def test_router_exposes_chat_copy_without_in_chat_mutations():
    routes = {
        (route.path, method)
        for route in chats_api.router.routes
        for method in route.methods or set()
    }

    assert ("/chats/{chat_id}/copies", "POST") in routes


@pytest.mark.asyncio
async def test_rollout_status_is_minimal_when_v2_is_disabled():
    request = SimpleNamespace()
    workspace = SimpleNamespace(chat_application=None)
    with patch.object(
        chats_api,
        "get_workspace",
        AsyncMock(return_value=workspace),
    ):
        result = await get_chat_rollout_status(request)

    assert result == {
        "enabled": False,
        "ui_enabled": False,
        "healthy": True,
        "stop_reasons": [],
        "warnings": [],
    }


@pytest.mark.asyncio
async def test_rollout_status_exposes_read_only_health_snapshot():
    request = SimpleNamespace()
    application = SimpleNamespace(paths=object())
    workspace = SimpleNamespace(chat_application=application)
    status = SimpleNamespace(
        as_dict=lambda: {
            "healthy": False,
            "stop_reasons": ("quarantined_chat",),
            "warnings": (),
        },
    )
    with (
        patch.object(
            chats_api,
            "get_workspace",
            AsyncMock(return_value=workspace),
        ),
        patch.object(
            chats_api,
            "run_sync_io",
            AsyncMock(return_value=status),
        ) as read_status,
        patch.object(
            chats_api,
            "chat_v2_ui_enabled",
            return_value=False,
        ),
    ):
        result = await get_chat_rollout_status(request)

    read_status.assert_awaited_once_with(
        chats_api.read_chat_rollout_status,
        application.paths,
    )
    assert result == {
        "enabled": True,
        "ui_enabled": False,
        "healthy": False,
        "stop_reasons": ("quarantined_chat",),
        "warnings": (),
    }


@pytest.mark.asyncio
async def test_v2_ui_kill_switch_preserves_normalized_storage_access(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("QWENPAW_CHAT_V2_UI", "0")
    manager = SimpleNamespace(
        get_chat=AsyncMock(return_value=_chat("normalized")),
    )
    workspace = SimpleNamespace(chat_application=SimpleNamespace())

    with pytest.raises(HTTPException) as raised:
        await chats_api.get_chat_view(
            chat_id="normalized",
            mgr=manager,
            workspace=workspace,
        )

    assert raised.value.status_code == 503
    assert raised.value.detail == {"code": "chat_v2_ui_disabled"}
    assert workspace.chat_application is not None


@pytest.mark.asyncio
async def test_list_chats_can_exclude_app_owned_dialogues():
    normal = _chat("normal")
    app_owned = _chat("app-owned", app_id="datapaw")
    manager = SimpleNamespace(
        list_chats=AsyncMock(return_value=[normal, app_owned]),
    )
    tracker = SimpleNamespace(get_status=AsyncMock(return_value="idle"))

    result = await list_chats(
        user_id=None,
        channel=None,
        archived=False,
        include_app_owned=False,
        mgr=manager,
        workspace=SimpleNamespace(task_tracker=tracker),
    )

    assert [chat.id for chat in result] == ["normal"]
    tracker.get_status.assert_awaited_once_with("normal")


@pytest.mark.asyncio
async def test_get_chat_hides_app_owned_dialogue_when_caller_opts_out():
    manager = SimpleNamespace(
        get_chat=AsyncMock(return_value=_chat("app-owned", app_id="datapaw")),
    )

    with pytest.raises(HTTPException) as raised:
        await get_chat(
            chat_id="app-owned",
            include_app_owned=False,
            mgr=manager,
            session=SimpleNamespace(),
            workspace=SimpleNamespace(),
        )

    assert raised.value.status_code == 404


@pytest.mark.asyncio
async def test_get_chat_v2_reads_normalized_transcript_only():
    message = Message(
        role=Role.USER,
        content=[TextContent(text="normalized").completed()],
        status=RunStatus.Completed,
    )
    application = SimpleNamespace(
        load_transcript=AsyncMock(return_value=[message]),
    )
    tracker = SimpleNamespace(get_status=AsyncMock(return_value="idle"))
    legacy_session = SimpleNamespace(
        get_session_state_dict=AsyncMock(
            side_effect=AssertionError("V2 must not read legacy JSON"),
        ),
    )

    result = await get_chat(
        chat_id="normalized",
        include_app_owned=True,
        mgr=SimpleNamespace(
            get_chat=AsyncMock(return_value=_chat("normalized")),
        ),
        session=legacy_session,
        workspace=SimpleNamespace(
            chat_application=application,
            config=SimpleNamespace(backend="qwenpaw"),
            task_tracker=tracker,
        ),
    )

    assert result.messages == [message]
    application.load_transcript.assert_awaited_once_with(
        "normalized",
        backend="qwenpaw",
    )
    legacy_session.get_session_state_dict.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_chat_status_uses_tracker_without_loading_chat_persistence():
    tracker = SimpleNamespace(get_status=AsyncMock(return_value="running"))
    workspace = SimpleNamespace(task_tracker=tracker)

    result = await get_chat_status(
        chat_id="chat-1",
        workspace=workspace,
    )

    assert result.status == "running"
    tracker.get_status.assert_awaited_once_with("chat-1")


@pytest.mark.asyncio
async def test_get_chat_status_treats_unknown_run_key_as_idle():
    tracker = SimpleNamespace(get_status=AsyncMock(return_value="idle"))

    result = await get_chat_status(
        chat_id="missing",
        workspace=SimpleNamespace(task_tracker=tracker),
    )

    assert result.status == "idle"
    tracker.get_status.assert_awaited_once_with("missing")


@pytest.mark.asyncio
async def test_get_chat_view_does_not_load_transcript_content():
    view = {
        "conversation_id": "normalized",
        "head_turn_id": "head",
    }
    application = SimpleNamespace(
        history_view=AsyncMock(return_value=view),
    )
    workspace = SimpleNamespace(
        chat_application=application,
        config=SimpleNamespace(backend="qwenpaw"),
    )

    result = await chats_api.get_chat_view(
        chat_id="normalized",
        mgr=SimpleNamespace(
            get_chat=AsyncMock(return_value=_chat("normalized")),
        ),
        workspace=workspace,
    )

    assert result == view
    application.history_view.assert_awaited_once_with(
        "normalized",
        backend="qwenpaw",
    )


@pytest.mark.asyncio
async def test_turn_page_rechecks_chat_visibility_and_agent_scope():
    page = ChatPage(
        items=({"turn_id": "turn"},),
        older_cursor=None,
        newer_cursor=None,
        view=ChatPageView("normalized", "turn", "version"),
        bytes=128,
        truncated_entity=False,
    )
    application = SimpleNamespace(turn_page=AsyncMock(return_value=page))
    workspace = SimpleNamespace(
        agent_id="agent",
        chat_application=application,
        config=SimpleNamespace(backend="qwenpaw"),
    )

    result = await chats_api.get_chat_turn_page(
        chat_id="normalized",
        head_turn_id="turn",
        view_version="version",
        cursor=None,
        target_turn_id="target-turn",
        limit=30,
        mgr=SimpleNamespace(
            get_chat=AsyncMock(return_value=_chat("normalized")),
        ),
        workspace=workspace,
    )

    assert result["items"] == [{"turn_id": "turn"}]
    application.turn_page.assert_awaited_once_with(
        "normalized",
        subject="agent",
        backend="qwenpaw",
        head_turn_id="turn",
        view_version="version",
        cursor=None,
        target_turn_id="target-turn",
        limit=30,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "status", "detail"),
    [
        (
            InvalidChatCursorError("forged"),
            400,
            {"code": "invalid_cursor"},
        ),
        (
            ChatViewInvalidatedError("replacement"),
            409,
            {
                "code": "view_invalidated",
                "head_turn_id": "replacement",
            },
        ),
    ],
)
async def test_history_page_maps_cursor_and_view_failures(
    failure: Exception,
    status: int,
    detail: dict,
):
    application = SimpleNamespace(
        turn_page=AsyncMock(side_effect=failure),
    )
    workspace = SimpleNamespace(
        agent_id="agent",
        chat_application=application,
        config=SimpleNamespace(backend="qwenpaw"),
    )

    with pytest.raises(HTTPException) as raised:
        await chats_api.get_chat_turn_page(
            chat_id="normalized",
            head_turn_id="head",
            view_version="version",
            cursor="cursor",
            target_turn_id=None,
            limit=30,
            mgr=SimpleNamespace(
                get_chat=AsyncMock(return_value=_chat("normalized")),
            ),
            workspace=workspace,
        )

    assert raised.value.status_code == status
    assert raised.value.detail == detail


@pytest.mark.asyncio
async def test_search_chats_filters_archived_and_app_owned_results():
    page = SimpleNamespace(
        as_dict=lambda: {
            "items": [
                {"conversation_id": "active", "turn_id": "turn-a"},
                {"conversation_id": "archived", "turn_id": "turn-b"},
                {"conversation_id": "app", "turn_id": "turn-c"},
            ],
            "next_before_seq": 4,
        },
    )
    application = SimpleNamespace(
        search_history=AsyncMock(return_value=page),
    )
    active = _chat("active")
    app_owned = _chat("app", app_id="pawapp")
    manager = SimpleNamespace(
        list_chats=AsyncMock(return_value=[active, app_owned]),
    )
    workspace = SimpleNamespace(chat_application=application)

    with patch.object(chats_api, "chat_v2_ui_enabled", return_value=True):
        result = await chats_api.search_chats(
            query="needle",
            before_seq=5,
            limit=20,
            mgr=manager,
            workspace=workspace,
        )

    assert result == {
        "items": [
            {"conversation_id": "active", "turn_id": "turn-a"},
        ],
        "next_before_seq": 4,
    }
    manager.list_chats.assert_awaited_once_with(archived=False)
    application.search_history.assert_awaited_once_with(
        "needle",
        before_seq=5,
        limit=20,
    )


@pytest.mark.asyncio
async def test_live_endpoint_sends_sync_before_post_barrier_event():
    page = ChatPage(
        items=(),
        older_cursor=None,
        newer_cursor=None,
        view=ChatPageView("normalized", None, "version"),
        bytes=128,
        truncated_entity=False,
    )
    application = SimpleNamespace(turn_page=AsyncMock(return_value=page))
    hub = ChatLiveHub()
    path = ChatLivePath(
        conversation_id="normalized",
        run_id="run",
        turn_id="turn",
    )
    await hub.start_run(path, {"blocks": ({"text": "pending"},)})
    workspace = SimpleNamespace(
        agent_id="agent",
        chat_application=application,
        task_tracker=SimpleNamespace(chat_live_hub=hub),
        config=SimpleNamespace(backend="qwenpaw"),
    )

    response = await chats_api.stream_chat_live(
        chat_id="normalized",
        run_epoch=None,
        event_sequence=None,
        mgr=SimpleNamespace(
            get_chat=AsyncMock(return_value=_chat("normalized")),
        ),
        workspace=workspace,
    )
    iterator = response.body_iterator
    sync = await anext(iterator)
    published = await hub.publish(
        path,
        kind="terminal",
        payload={"state": "completed"},
        terminal=True,
    )
    terminal = await anext(iterator)

    assert "event: sync" in sync
    assert '"text":"pending"' in sync
    assert "event: terminal" in terminal
    assert published.cursor.run_epoch in terminal
    with pytest.raises(StopAsyncIteration):
        await anext(iterator)


@pytest.mark.asyncio
async def test_live_endpoint_rejects_partial_cursor():
    workspace = SimpleNamespace(
        agent_id="agent",
        chat_application=SimpleNamespace(),
        config=SimpleNamespace(backend="qwenpaw"),
    )

    with pytest.raises(HTTPException) as raised:
        await chats_api.stream_chat_live(
            chat_id="normalized",
            run_epoch="epoch",
            event_sequence=None,
            mgr=SimpleNamespace(
                get_chat=AsyncMock(return_value=_chat("normalized")),
            ),
            workspace=workspace,
        )

    assert raised.value.status_code == 400
    assert raised.value.detail == {"code": "invalid_live_cursor"}


@pytest.mark.asyncio
async def test_delete_chat_stops_a_live_run():
    """Deleting a running chat must not leave its run in the tracker.

    A live run whose chat is deleted would otherwise stay in ``_runs``
    for good, so ``get_global_status()['running_task_count']`` stays
    inflated while the chat list can no longer show that chat (#7991).
    """
    from qwenpaw.app.task_tracker import TaskTracker

    tracker = TaskTracker()
    release = asyncio.Event()

    async def stream(_payload):
        await release.wait()
        yield "data: done\n\n"

    await tracker.attach_or_start("chat-live", None, stream)
    assert (await tracker.get_global_status())["running_task_count"] == 1

    manager = SimpleNamespace(
        get_chat=AsyncMock(return_value=_chat("chat-live")),
        delete_chats=AsyncMock(return_value=True),
    )
    workspace = SimpleNamespace(task_tracker=tracker)

    with patch(
        "qwenpaw.app.chats.deletion_service.CHECKPOINT_RUNTIME."
        "delete_session_checkpoints",
        new=AsyncMock(),
    ):
        result = await chats_api.delete_chat(
            chat_id="chat-live",
            mgr=manager,
            workspace=workspace,
        )

    assert result == {"deleted": True}
    assert await tracker.list_active_tasks() == []
    assert (await tracker.get_global_status())["running_task_count"] == 0
