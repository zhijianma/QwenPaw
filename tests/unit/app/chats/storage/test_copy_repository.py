# -*- coding: utf-8 -*-
"""Independent Chat copy storage invariants."""

from __future__ import annotations

from pathlib import Path

import pytest

from qwenpaw.app.chats.application import ChatApplicationService
from qwenpaw.app.chats.storage import (
    AcceptanceRequest,
    ChatStorageCoordinator,
    ChatStoragePaths,
    CatalogRepository,
    ConversationCreateRequest,
    ConversationCopyRequest,
    accept_input,
    finalize_run,
    load_runtime_run_handle,
    read_transcript,
)
from qwenpaw.app.chats.storage.common import connect_database
from qwenpaw.schemas import Message, Role, RunStatus, TextContent


SOURCE_ID = "11111111-1111-4111-8111-111111111111"
TARGET_ID = "22222222-2222-4222-8222-222222222222"


def _message(text: str) -> Message:
    return Message(
        role=Role.USER,
        content=[TextContent(text=text).completed()],
        status=RunStatus.Completed,
    )


def _source(tmp_path: Path):
    paths = ChatStoragePaths(tmp_path)
    ChatStorageCoordinator(paths).create_conversation(
        ConversationCreateRequest(
            conversation_id=SOURCE_ID,
            agent_id="agent",
            owner_scope="console:user",
            display_name="Source",
            backend="qwenpaw",
            now_us=1,
        ),
    )
    turns = []
    for index, text in enumerate(("first", "second"), start=1):
        accepted = accept_input(
            paths.conversation(SOURCE_ID),
            AcceptanceRequest(
                conversation_id=SOURCE_ID,
                client_request_id=f"request-{index}",
                message=_message(text),
                process_instance_id="process",
                now_us=index * 10,
            ),
            recall_sequences=(index,),
        )
        handle = load_runtime_run_handle(
            paths.conversation(SOURCE_ID),
            conversation_id=SOURCE_ID,
            run_id=accepted.work_id,
        )
        finalize_run(
            paths.conversation(SOURCE_ID),
            handle,
            state="completed",
            runtime_context=f'{{"turn":{index}}}'.encode(),
            usage=None,
            now_us=index * 10 + 1,
        )
        turns.append(accepted.turn_id)
    return paths, turns


def test_copy_copies_lineage_and_context_into_independent_chat(
    tmp_path: Path,
) -> None:
    paths, turns = _source(tmp_path)
    marker = tmp_path / "workspace-marker.txt"
    marker.write_text("unchanged", encoding="utf-8")
    coordinator = ChatStorageCoordinator(paths)

    coordinator.copy_conversation(
        ConversationCopyRequest(
            conversation_id=TARGET_ID,
            source_conversation_id=SOURCE_ID,
            source_turn_id=turns[0],
            root_conversation_id=SOURCE_ID,
            session_id=f"copy:{TARGET_ID}",
            agent_id="agent",
            owner_scope="console:user",
            display_name="Source (copy)",
            group_id=None,
            now_us=100,
        ),
        recall_sequences=[100],
    )

    source_messages = read_transcript(paths.conversation(SOURCE_ID))
    target_messages = read_transcript(paths.conversation(TARGET_ID))
    assert len(source_messages) == 2
    assert len(target_messages) == 1
    assert target_messages[0].content == source_messages[0].content
    assert target_messages[0].id != source_messages[0].id
    assert marker.read_text(encoding="utf-8") == "unchanged"

    with connect_database(
        paths.conversation(TARGET_ID),
        readonly=True,
    ) as connection:
        context = connection.execute(
            "SELECT rb.payload FROM chat_meta AS m JOIN "
            "chat_context_revisions AS cr ON cr.context_revision_id = "
            "m.current_context_revision_id JOIN chat_runtime_blobs AS rb "
            "ON rb.blob_hash = cr.runtime_blob_hash "
            "WHERE m.singleton_id = 1",
        ).fetchone()
        assert bytes(context["payload"]) == b'{"turn":1}'
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM chat_runs",
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM chat_queue_items",
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT recall_seq FROM chat_blocks",
            ).fetchone()[0]
            == 100
        )

    paths.conversation(SOURCE_ID).unlink()
    surviving = read_transcript(paths.conversation(TARGET_ID))
    assert len(surviving) == 1


@pytest.mark.asyncio
async def test_application_copy_publishes_independent_target(
    tmp_path: Path,
) -> None:
    paths, turns = _source(tmp_path)
    application = ChatApplicationService(tmp_path)

    await application.copy_chat(
        SOURCE_ID,
        target_conversation_id=TARGET_ID,
        target_session_id=f"copy:{TARGET_ID}",
        source_turn_id=turns[0],
        display_name="Source (copy)",
        group_id=None,
    )

    source_view = await application.history_view(SOURCE_ID)
    target_view = await application.history_view(TARGET_ID)
    assert source_view["head_turn_id"] == turns[1]
    assert target_view["head_turn_id"] != turns[0]
    with connect_database(paths.catalog, readonly=True) as connection:
        target = connection.execute(
            "SELECT lifecycle FROM chat_catalog_entries WHERE "
            "conversation_id = ?",
            (TARGET_ID,),
        ).fetchone()
        assert target["lifecycle"] == "ready"


def test_copy_cleans_published_target_when_catalog_projection_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, turns = _source(tmp_path)
    catalog = CatalogRepository(paths.catalog)
    coordinator = ChatStorageCoordinator(paths, catalog=catalog)

    def fail_projection(*_args, **_kwargs):
        raise RuntimeError("projection failed")

    monkeypatch.setattr(catalog, "set_runtime_projection", fail_projection)

    with pytest.raises(RuntimeError, match="projection failed"):
        coordinator.copy_conversation(
            ConversationCopyRequest(
                conversation_id=TARGET_ID,
                source_conversation_id=SOURCE_ID,
                source_turn_id=turns[0],
                root_conversation_id=SOURCE_ID,
                session_id=f"copy:{TARGET_ID}",
                agent_id="agent",
                owner_scope="console:user",
                display_name="Source (copy)",
                group_id=None,
                now_us=100,
            ),
            recall_sequences=[100],
        )

    assert not paths.conversation(TARGET_ID).exists()
    target = catalog.get(TARGET_ID)
    assert target is not None
    assert target.lifecycle == "deleted"
