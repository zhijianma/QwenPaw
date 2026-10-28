# -*- coding: utf-8 -*-
"""Unified Chat application acceptance tests."""

from __future__ import annotations

import asyncio
import importlib
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qwenpaw.app.chats.application import ChatApplicationService
from qwenpaw.app.chats.storage.pagination_repository import (
    ChatPage,
    ChatPageView,
)
from qwenpaw.app.chats.models import ChatSpec, ChatsFile
from qwenpaw.app.chats.storage import (
    ChatStorageSchemaError,
    ChatStorageCoordinator,
    ChatStoragePaths,
    ConversationCreateRequest,
    AcceptanceRequest,
    accept_input,
    finalize_run,
    load_runtime_run_handle,
)
from qwenpaw.app.chats.storage.common import connect_database
from qwenpaw.runtime.tool_guard import _persist_chat_approval
from qwenpaw.schemas import Message, Role, RunStatus, TextContent


CONVERSATION_ID = "11111111-1111-4111-8111-111111111111"


class _CheckpointMaterializer:
    def __init__(self, conversation_id: str) -> None:
        self.conversation_id = conversation_id
        self.flush_partial_blocks = AsyncMock()


@pytest.mark.asyncio
async def test_service_initializes_an_empty_workspace(tmp_path: Path) -> None:
    service = ChatApplicationService(tmp_path)

    report = await service.initialize(agent_id="agent", now_us=1)

    assert report is None
    assert service.paths.catalog.is_file()


@pytest.mark.asyncio
async def test_checkpoint_flush_targets_only_matching_materializers(
    tmp_path: Path,
) -> None:
    service = ChatApplicationService(tmp_path)
    matching = _CheckpointMaterializer(CONVERSATION_ID)
    unrelated = _CheckpointMaterializer("other")
    service.register_materializer(matching)
    service.register_materializer(unrelated)

    await service.flush_partial_blocks(CONVERSATION_ID)

    matching.flush_partial_blocks.assert_awaited_once_with()
    unrelated.flush_partial_blocks.assert_not_awaited()


@pytest.mark.asyncio
async def test_identical_page_reads_coalesce_without_sharing_cancellation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = ChatApplicationService(tmp_path)
    service.ensure_ready = AsyncMock(  # type: ignore[method-assign]
        return_value=CONVERSATION_ID,
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0
    page = ChatPage(
        items=(),
        older_cursor=None,
        newer_cursor=None,
        view=ChatPageView(
            CONVERSATION_ID,
            None,
            "version",
        ),
        bytes=128,
        truncated_entity=False,
    )

    async def fake_to_thread(_function, *_args, **_kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return page

    application_module = importlib.import_module(
        "qwenpaw.app.chats.application",
    )
    monkeypatch.setattr(
        application_module.asyncio,
        "to_thread",
        fake_to_thread,
    )

    async def load_page() -> ChatPage:
        return await service.turn_page(
            CONVERSATION_ID,
            subject="agent",
            head_turn_id="head",
            view_version="version",
            cursor="cursor",
        )

    first = asyncio.create_task(load_page())
    second = asyncio.create_task(load_page())
    await entered.wait()
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()

    assert await second is page
    assert calls == 1


@pytest.mark.asyncio
async def test_service_records_intent_before_durable_acceptance(
    tmp_path: Path,
) -> None:
    paths = ChatStoragePaths(tmp_path)
    ChatStorageCoordinator(paths).create_conversation(
        ConversationCreateRequest(
            conversation_id=CONVERSATION_ID,
            agent_id="agent",
            owner_scope="console:user",
            display_name="Application",
            backend="qwenpaw",
            now_us=1,
        ),
    )
    service = ChatApplicationService(tmp_path, process_instance_id="process")
    message = Message(
        role=Role.USER,
        content=[TextContent(text="durable first").completed()],
        status=RunStatus.Completed,
    )

    result = await service.accept_input(
        conversation_id=CONVERSATION_ID,
        client_request_id="request",
        message=message,
        now_us=2,
    )
    replay = await service.accept_input(
        conversation_id=CONVERSATION_ID,
        client_request_id="request",
        message=message,
        now_us=3,
    )

    assert result.kind == "run"
    assert replay.work_id == result.work_id
    assert replay.replayed is True
    with connect_database(paths.catalog, readonly=True) as connection:
        intent = connection.execute(
            "SELECT kind, run_or_queue_id, state FROM chat_runtime_intents",
        ).fetchone()
        assert tuple(intent) == ("run", result.work_id, "recorded")
    with connect_database(
        paths.conversation(CONVERSATION_ID),
        readonly=True,
    ) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM chat_turns").fetchone()[0]
            == 1
        )

    recovered = await ChatApplicationService(tmp_path).recover_after_restart(
        now_us=4,
    )
    assert recovered == 1


@pytest.mark.asyncio
async def test_active_migration_prioritizes_pinned_and_skips_archived(
    tmp_path: Path,
) -> None:
    instant = datetime(2026, 1, 1, tzinfo=timezone.utc)
    pinned_id = "22222222-2222-4222-8222-222222222222"
    recent_id = "33333333-3333-4333-8333-333333333333"
    archived_id = "44444444-4444-4444-8444-444444444444"
    chats = ChatsFile(
        chats=[
            ChatSpec(
                id=pinned_id,
                name="Pinned",
                session_id="console:pinned",
                user_id="user",
                channel="console",
                pinned=True,
                created_at=instant,
                updated_at=instant,
            ),
            ChatSpec(
                id=recent_id,
                name="Recent",
                session_id="console:recent",
                user_id="user",
                channel="console",
                created_at=instant,
                updated_at=instant + timedelta(days=1),
            ),
            ChatSpec(
                id=archived_id,
                name="Archived",
                session_id="console:archived",
                user_id="user",
                channel="console",
                archived_at=instant + timedelta(days=2),
                created_at=instant,
                updated_at=instant + timedelta(days=2),
            ),
        ],
    )
    (tmp_path / "chats.json").write_text(
        json.dumps(chats.model_dump(mode="json")),
        encoding="utf-8",
    )
    service = ChatApplicationService(tmp_path)
    await service.initialize(agent_id="agent", now_us=1)
    migrate = AsyncMock()
    service._migration.migrate = migrate  # pylint: disable=protected-access

    await service.migrate_active_legacy()

    assert [call.args[0] for call in migrate.await_args_list] == [
        pinned_id,
        recent_id,
    ]
    with connect_database(
        ChatStoragePaths(tmp_path).catalog,
        readonly=True,
    ) as connection:
        states = {
            str(row["conversation_id"]): str(row["state"])
            for row in connection.execute(
                "SELECT conversation_id, state FROM chat_migrations",
            )
        }
        catalog = {
            str(row["conversation_id"]): (
                int(row["pinned"]),
                row["archived_at_us"],
            )
            for row in connection.execute(
                "SELECT conversation_id, pinned, archived_at_us "
                "FROM chat_catalog_entries",
            )
        }
    assert states[pinned_id] == "queued"
    assert states[recent_id] == "queued"
    assert states[archived_id] == "legacy_only"
    assert catalog[pinned_id] == (1, None)
    assert catalog[archived_id][1] is not None


@pytest.mark.asyncio
async def test_ready_chat_schema_corruption_is_quarantined(
    tmp_path: Path,
) -> None:
    paths = ChatStoragePaths(tmp_path)
    ChatStorageCoordinator(paths).create_conversation(
        ConversationCreateRequest(
            conversation_id=CONVERSATION_ID,
            agent_id="agent",
            owner_scope="console:user",
            display_name="Corrupt",
            backend="qwenpaw",
            now_us=1,
        ),
    )
    with connect_database(paths.conversation(CONVERSATION_ID)) as connection:
        connection.execute("DROP INDEX ix_chat_messages_turn")

    with pytest.raises(ChatStorageSchemaError):
        await ChatApplicationService(tmp_path).ensure_ready(CONVERSATION_ID)

    with connect_database(paths.catalog, readonly=True) as connection:
        lifecycle = connection.execute(
            "SELECT lifecycle FROM chat_catalog_entries "
            "WHERE conversation_id = ?",
            (CONVERSATION_ID,),
        ).fetchone()[0]
    assert lifecycle == "quarantined"


@pytest.mark.asyncio
async def test_valid_quarantined_chat_is_restored_to_ready(
    tmp_path: Path,
) -> None:
    paths = ChatStoragePaths(tmp_path)
    ChatStorageCoordinator(paths).create_conversation(
        ConversationCreateRequest(
            conversation_id=CONVERSATION_ID,
            agent_id="agent",
            owner_scope="console:user",
            display_name="Recoverable",
            backend="qwenpaw",
            now_us=1,
        ),
    )
    with connect_database(paths.catalog) as connection:
        connection.execute(
            "UPDATE chat_catalog_entries SET lifecycle = 'quarantined' "
            "WHERE conversation_id = ?",
            (CONVERSATION_ID,),
        )

    result = await ChatApplicationService(tmp_path).ensure_ready(
        CONVERSATION_ID,
    )

    assert result == CONVERSATION_ID
    with connect_database(paths.catalog, readonly=True) as connection:
        lifecycle = connection.execute(
            "SELECT lifecycle FROM chat_catalog_entries "
            "WHERE conversation_id = ?",
            (CONVERSATION_ID,),
        ).fetchone()[0]
    assert lifecycle == "ready"


@pytest.mark.asyncio
async def test_clear_context_keeps_the_durable_transcript(
    tmp_path: Path,
) -> None:
    paths = ChatStoragePaths(tmp_path)
    ChatStorageCoordinator(paths).create_conversation(
        ConversationCreateRequest(
            conversation_id=CONVERSATION_ID,
            agent_id="agent",
            owner_scope="console:user",
            display_name="Clear",
            backend="qwenpaw",
            now_us=1,
        ),
    )
    service = ChatApplicationService(tmp_path, process_instance_id="process")
    accepted = await service.accept_input(
        conversation_id=CONVERSATION_ID,
        client_request_id="clear-request",
        message=Message(
            role=Role.USER,
            content=[TextContent(text="/clear").completed()],
            status=RunStatus.Completed,
        ),
        now_us=2,
    )
    handle = load_runtime_run_handle(
        paths.conversation(CONVERSATION_ID),
        conversation_id=CONVERSATION_ID,
        run_id=accepted.work_id,
    )
    finalize_run(
        paths.conversation(CONVERSATION_ID),
        handle,
        state="completed",
        runtime_context=b'{"state":{"context":[]}}',
        context_kind="clear",
        usage=None,
        error_digest=None,
        now_us=3,
    )

    transcript = await service.load_transcript(CONVERSATION_ID)
    context = await service.load_runtime_context(CONVERSATION_ID)

    assert [message.content[0].text for message in transcript] == ["/clear"]
    assert context == {"state": {"context": []}}
    with connect_database(
        paths.conversation(CONVERSATION_ID),
        readonly=True,
    ) as connection:
        assert (
            connection.execute(
                "SELECT kind FROM chat_context_revisions "
                "WHERE anchor_turn_id = ?",
                (accepted.turn_id,),
            ).fetchone()[0]
            == "clear"
        )


@pytest.mark.asyncio
async def test_chat_copy_context_respects_clear_boundary(
    tmp_path: Path,
) -> None:
    paths = ChatStoragePaths(tmp_path)
    ChatStorageCoordinator(paths).create_conversation(
        ConversationCreateRequest(
            conversation_id=CONVERSATION_ID,
            agent_id="agent",
            owner_scope="console:user",
            display_name="Clear copies",
            backend="qwenpaw",
            now_us=1,
        ),
    )
    service = ChatApplicationService(tmp_path, process_instance_id="process")

    first = await service.accept_input(
        conversation_id=CONVERSATION_ID,
        client_request_id="before-clear",
        message=Message(
            role=Role.USER,
            content=[TextContent(text="remember").completed()],
            status=RunStatus.Completed,
        ),
        now_us=2,
    )
    first_handle = load_runtime_run_handle(
        paths.conversation(CONVERSATION_ID),
        conversation_id=CONVERSATION_ID,
        run_id=first.work_id,
    )
    finalize_run(
        paths.conversation(CONVERSATION_ID),
        first_handle,
        state="completed",
        runtime_context=b'{"state":{"context":["remember"]}}',
        usage=None,
        now_us=3,
    )
    cleared = await service.accept_input(
        conversation_id=CONVERSATION_ID,
        client_request_id="clear",
        message=Message(
            role=Role.USER,
            content=[TextContent(text="/clear").completed()],
            status=RunStatus.Completed,
        ),
        now_us=4,
    )
    clear_handle = load_runtime_run_handle(
        paths.conversation(CONVERSATION_ID),
        conversation_id=CONVERSATION_ID,
        run_id=cleared.work_id,
    )
    finalize_run(
        paths.conversation(CONVERSATION_ID),
        clear_handle,
        state="completed",
        runtime_context=b'{"state":{"context":[]}}',
        context_kind="clear",
        usage=None,
        now_us=5,
    )

    before_id = "22222222-2222-4222-8222-222222222222"
    after_id = "33333333-3333-4333-8333-333333333333"
    await service.copy_chat(
        CONVERSATION_ID,
        target_conversation_id=before_id,
        target_session_id=f"copy:{before_id}",
        source_turn_id=first.turn_id,
        display_name="Before clear",
        group_id=None,
    )
    await service.copy_chat(
        CONVERSATION_ID,
        target_conversation_id=after_id,
        target_session_id=f"copy:{after_id}",
        source_turn_id=cleared.turn_id,
        display_name="After clear",
        group_id=None,
    )

    assert await service.load_runtime_context(before_id) == {
        "state": {"context": ["remember"]},
    }
    assert await service.load_runtime_context(after_id) == {
        "state": {"context": []},
    }


@pytest.mark.asyncio
async def test_service_waits_for_and_claims_durable_queue_item(
    tmp_path: Path,
) -> None:
    paths = ChatStoragePaths(tmp_path)
    ChatStorageCoordinator(paths).create_conversation(
        ConversationCreateRequest(
            conversation_id=CONVERSATION_ID,
            agent_id="agent",
            owner_scope="console:user",
            display_name="Queue",
            backend="qwenpaw",
            now_us=1,
        ),
    )
    service = ChatApplicationService(tmp_path, process_instance_id="process")
    first = await service.accept_input(
        conversation_id=CONVERSATION_ID,
        client_request_id="first",
        message=Message(
            role=Role.USER,
            content=[TextContent(text="first").completed()],
            status=RunStatus.Completed,
        ),
        now_us=2,
    )
    queued = await service.accept_input(
        conversation_id=CONVERSATION_ID,
        client_request_id="second",
        message=Message(
            role=Role.USER,
            content=[TextContent(text="second").completed()],
            status=RunStatus.Completed,
        ),
        now_us=3,
    )
    waiter = asyncio.create_task(
        service.wait_for_queued_run(
            conversation_id=CONVERSATION_ID,
            accepted=queued,
        ),
    )
    await asyncio.sleep(0)
    handle = load_runtime_run_handle(
        paths.conversation(CONVERSATION_ID),
        conversation_id=CONVERSATION_ID,
        run_id=first.work_id,
    )
    finalize_run(
        paths.conversation(CONVERSATION_ID),
        handle,
        state="completed",
        runtime_context=None,
        usage=None,
        error_digest=None,
        now_us=4,
    )

    claimed = await asyncio.wait_for(waiter, timeout=1)

    assert claimed.kind == "run"
    assert claimed.turn_id == queued.turn_id
    transcript = await service.load_transcript(CONVERSATION_ID)
    assert [message.content[0].text for message in transcript] == [
        "first",
        "second",
    ]


@pytest.mark.asyncio
async def test_canceling_queue_wait_closes_durable_queue_and_intent(
    tmp_path: Path,
) -> None:
    paths = ChatStoragePaths(tmp_path)
    ChatStorageCoordinator(paths).create_conversation(
        ConversationCreateRequest(
            conversation_id=CONVERSATION_ID,
            agent_id="agent",
            owner_scope="console:user",
            display_name="Queue cancellation",
            backend="qwenpaw",
            now_us=1,
        ),
    )
    service = ChatApplicationService(tmp_path, process_instance_id="process")
    first = await service.accept_input(
        conversation_id=CONVERSATION_ID,
        client_request_id="first",
        message=Message(
            role=Role.USER,
            content=[TextContent(text="first").completed()],
            status=RunStatus.Completed,
        ),
        now_us=2,
    )
    queued = await service.accept_input(
        conversation_id=CONVERSATION_ID,
        client_request_id="second",
        message=Message(
            role=Role.USER,
            content=[TextContent(text="second").completed()],
            status=RunStatus.Completed,
        ),
        now_us=3,
    )
    waiter = asyncio.create_task(
        service.wait_for_queued_run(
            conversation_id=CONVERSATION_ID,
            accepted=queued,
        ),
    )
    await asyncio.sleep(0)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    with connect_database(
        paths.conversation(CONVERSATION_ID),
        readonly=True,
    ) as connection:
        queue_state = connection.execute(
            "SELECT state FROM chat_queue_items WHERE queue_item_id = ?",
            (queued.work_id,),
        ).fetchone()[0]
        turn_state = connection.execute(
            "SELECT state FROM chat_turns WHERE turn_id = ?",
            (queued.turn_id,),
        ).fetchone()[0]
    with connect_database(paths.catalog, readonly=True) as connection:
        intent_state = connection.execute(
            "SELECT state FROM chat_runtime_intents "
            "WHERE client_request_id = 'second'",
        ).fetchone()[0]
    assert first.kind == "run"
    assert queue_state == "canceled"
    assert turn_state == "canceled"
    assert intent_state == "terminal"


@pytest.mark.asyncio
async def test_restart_closes_empty_starting_intent_without_recovery(
    tmp_path: Path,
) -> None:
    paths = ChatStoragePaths(tmp_path)
    ChatStorageCoordinator(paths).create_conversation(
        ConversationCreateRequest(
            conversation_id=CONVERSATION_ID,
            agent_id="agent",
            owner_scope="console:user",
            display_name="Empty intent",
            backend="qwenpaw",
            now_us=1,
        ),
    )
    service = ChatApplicationService(tmp_path)
    service._record_intent(  # pylint: disable=protected-access
        CONVERSATION_ID,
        "request",
        2,
    )

    recovered = await service.recover_after_restart(now_us=3)

    assert recovered == 0
    with connect_database(paths.catalog, readonly=True) as connection:
        state = connection.execute(
            "SELECT state FROM chat_runtime_intents",
        ).fetchone()[0]
    assert state == "terminal"


@pytest.mark.asyncio
async def test_restart_recovers_source_when_catalog_intent_is_starting(
    tmp_path: Path,
) -> None:
    paths = ChatStoragePaths(tmp_path)
    ChatStorageCoordinator(paths).create_conversation(
        ConversationCreateRequest(
            conversation_id=CONVERSATION_ID,
            agent_id="agent",
            owner_scope="console:user",
            display_name="Source accepted",
            backend="qwenpaw",
            now_us=1,
        ),
    )
    service = ChatApplicationService(tmp_path)
    service._record_intent(  # pylint: disable=protected-access
        CONVERSATION_ID,
        "request",
        2,
    )
    accepted = accept_input(
        paths.conversation(CONVERSATION_ID),
        AcceptanceRequest(
            conversation_id=CONVERSATION_ID,
            client_request_id="request",
            message=Message(
                role=Role.USER,
                content=[TextContent(text="accepted").completed()],
                status=RunStatus.Completed,
            ),
            process_instance_id="old-process",
            now_us=2,
        ),
        recall_sequences=(1,),
    )

    recovered = await service.recover_after_restart(now_us=3)

    assert recovered == 1
    with connect_database(
        paths.conversation(CONVERSATION_ID),
        readonly=True,
    ) as connection:
        run_state = connection.execute(
            "SELECT state FROM chat_runs WHERE run_id = ?",
            (accepted.work_id,),
        ).fetchone()[0]
        turn_state = connection.execute(
            "SELECT state FROM chat_turns WHERE turn_id = ?",
            (accepted.turn_id,),
        ).fetchone()[0]
    with connect_database(paths.catalog, readonly=True) as connection:
        intent_state = connection.execute(
            "SELECT state FROM chat_runtime_intents",
        ).fetchone()[0]
    assert run_state == "interrupted"
    assert turn_state == "interrupted"
    assert intent_state == "terminal"


@pytest.mark.asyncio
async def test_tool_approval_boundaries_are_persisted_in_transcript(
    tmp_path: Path,
) -> None:
    paths = ChatStoragePaths(tmp_path)
    ChatStorageCoordinator(paths).create_conversation(
        ConversationCreateRequest(
            conversation_id=CONVERSATION_ID,
            agent_id="agent",
            owner_scope="console:user",
            display_name="Approval",
            backend="qwenpaw",
            now_us=1,
        ),
    )
    service = ChatApplicationService(tmp_path, process_instance_id="process")
    accepted = await service.accept_input(
        conversation_id=CONVERSATION_ID,
        client_request_id="request",
        message=Message(
            role=Role.USER,
            content=[TextContent(text="run tool").completed()],
            status=RunStatus.Completed,
        ),
        now_us=2,
    )
    pending = SimpleNamespace(
        request_id="approval-1",
        tool_name="shell",
        severity="high",
        findings_count=1,
        result_summary="Command needs approval",
        scope=SimpleNamespace(value="exact"),
        extra={"tool_call": {"id": "call-1"}},
    )
    context = {
        "workspace_dir": str(tmp_path),
        "chat_conversation_id": CONVERSATION_ID,
        "chat_run_id": accepted.work_id,
    }

    await _persist_chat_approval(context, pending, state="pending")
    await _persist_chat_approval(
        context,
        pending,
        state="resolved",
        decision=SimpleNamespace(value="approved"),
    )

    transcript = await service.load_transcript(CONVERSATION_ID)
    approval_blocks = [message.content[0].data for message in transcript[1:]]
    assert [block["state"] for block in approval_blocks] == [
        "pending",
        "resolved",
    ]
    assert approval_blocks[1]["decision"] == "approved"
    assert approval_blocks[1]["tool_call_id"] == "call-1"


@pytest.mark.asyncio
async def test_tool_approval_flush_finishes_before_cancellation_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = ChatStoragePaths(tmp_path)
    ChatStorageCoordinator(paths).create_conversation(
        ConversationCreateRequest(
            conversation_id=CONVERSATION_ID,
            agent_id="agent",
            owner_scope="console:user",
            display_name="Approval cancellation",
            backend="qwenpaw",
            now_us=1,
        ),
    )
    service = ChatApplicationService(tmp_path, process_instance_id="process")
    accepted = await service.accept_input(
        conversation_id=CONVERSATION_ID,
        client_request_id="request",
        message=Message(
            role=Role.USER,
            content=[TextContent(text="run tool").completed()],
            status=RunStatus.Completed,
        ),
        now_us=2,
    )
    storage_module = importlib.import_module("qwenpaw.app.chats.storage")
    real_append = storage_module.append_completed_message
    append_finished = threading.Event()
    allow_return = threading.Event()

    def blocked_append(*args, **kwargs):  # noqa: ANN
        result = real_append(*args, **kwargs)
        append_finished.set()
        allow_return.wait(timeout=2)
        return result

    monkeypatch.setattr(
        storage_module,
        "append_completed_message",
        blocked_append,
    )
    pending = SimpleNamespace(
        request_id="approval-1",
        tool_name="shell",
        severity="high",
        findings_count=1,
        result_summary="Command needs approval",
        scope=None,
        extra={"tool_call": {"id": "call-1"}},
    )
    task = asyncio.create_task(
        _persist_chat_approval(
            {
                "workspace_dir": str(tmp_path),
                "chat_conversation_id": CONVERSATION_ID,
                "chat_run_id": accepted.work_id,
            },
            pending,
            state="pending",
        ),
    )
    assert await asyncio.to_thread(append_finished.wait, 1)

    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    allow_return.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    transcript = await service.load_transcript(CONVERSATION_ID)
    assert transcript[-1].content[0].data["state"] == "pending"


@pytest.mark.asyncio
async def test_acceptance_cancellation_closes_committed_source_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = ChatStoragePaths(tmp_path)
    ChatStorageCoordinator(paths).create_conversation(
        ConversationCreateRequest(
            conversation_id=CONVERSATION_ID,
            agent_id="agent",
            owner_scope="console:user",
            display_name="Acceptance cancellation",
            backend="qwenpaw",
            now_us=1,
        ),
    )
    service = ChatApplicationService(tmp_path, process_instance_id="process")
    application_module = importlib.import_module(
        "qwenpaw.app.chats.application",
    )
    real_project = application_module.project_conversation
    projection_started = threading.Event()
    allow_projection = threading.Event()

    def blocked_project(*args, **kwargs):  # noqa: ANN
        projection_started.set()
        allow_projection.wait(timeout=2)
        return real_project(*args, **kwargs)

    monkeypatch.setattr(
        application_module,
        "project_conversation",
        blocked_project,
    )
    task = asyncio.create_task(
        service.accept_input(
            conversation_id=CONVERSATION_ID,
            client_request_id="request",
            message=Message(
                role=Role.USER,
                content=[TextContent(text="cancel me").completed()],
                status=RunStatus.Completed,
            ),
            now_us=2,
        ),
    )
    assert await asyncio.to_thread(projection_started.wait, 1)

    task.cancel()
    allow_projection.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    with connect_database(
        paths.conversation(CONVERSATION_ID),
        readonly=True,
    ) as connection:
        run_state = connection.execute(
            "SELECT state FROM chat_runs",
        ).fetchone()[0]
        turn_state = connection.execute(
            "SELECT state FROM chat_turns",
        ).fetchone()[0]
    with connect_database(paths.catalog, readonly=True) as connection:
        intent_state = connection.execute(
            "SELECT state FROM chat_runtime_intents",
        ).fetchone()[0]
    assert run_state == "canceled"
    assert turn_state == "canceled"
    assert intent_state == "terminal"
