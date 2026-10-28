# -*- coding: utf-8 -*-
"""Tests for stable, byte-bounded normalized Chat history pages."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest

from qwenpaw.app.chats.integrity import build_transcript_manifest
from qwenpaw.app.chats.migration import (
    NormalizedTranscript,
    create_legacy_snapshot,
)
from qwenpaw.app.chats.pagination_cursor import (
    ChatCursorCodec,
    InvalidChatCursorError,
)
from qwenpaw.app.chats.storage import (
    ChatStoragePaths,
    materialize_normalized_transcript,
)
from qwenpaw.app.chats.storage.common import connect_database
from qwenpaw.app.chats.storage.pagination_repository import (
    ChatEntityVersionError,
    ChatPageNotFoundError,
    ChatViewInvalidatedError,
    read_block_fragment,
    read_block_page,
    read_message_page,
    read_turn_page,
)
from qwenpaw.schemas import Message, Role, RunStatus, TextContent


CONVERSATION_ID = "11111111-1111-4111-8111-111111111111"


def _message(role: Role, text: str) -> Message:
    return Message(
        role=role,
        content=[TextContent(text=text).completed()],
        status=RunStatus.Completed,
    )


def _seed(tmp_path: Path, *, huge_text: str = "") -> Path:
    messages = [
        _message(Role.USER, "user-0"),
        _message(Role.ASSISTANT, "assistant-0"),
        _message(Role.USER, "user-1"),
        _message(Role.ASSISTANT, huge_text or "assistant-1"),
        _message(Role.USER, "user-2"),
        _message(Role.ASSISTANT, "assistant-2"),
    ]
    normalized = NormalizedTranscript(
        messages=tuple(messages),
        manifest=build_transcript_manifest(messages),
        history_parts=0,
        session_parts=len(messages),
        deduplicated_parts=0,
    )
    paths = ChatStoragePaths(tmp_path)
    paths.ensure_layout()
    materialize_normalized_transcript(
        paths.conversation(CONVERSATION_ID),
        conversation_id=CONVERSATION_ID,
        agent_id="agent",
        owner_scope="console:user",
        backend="qwenpaw",
        normalized=normalized,
        snapshot=create_legacy_snapshot(
            tmp_path,
            conversation_id=CONVERSATION_ID,
            session_id="session",
            source_paths=[],
        ),
        now_us=1,
        allocated_recall_sequences=range(1, 7),
    )
    return paths.conversation(CONVERSATION_ID)


def test_turn_cursor_keeps_a_fixed_head_while_new_turns_arrive(
    tmp_path: Path,
) -> None:
    path = _seed(tmp_path)
    codec = ChatCursorCodec(b"k" * 32)
    tail = read_turn_page(
        path,
        conversation_id=CONVERSATION_ID,
        subject="agent",
        codec=codec,
        limit=1,
    )
    fixed_head = tail.view.head_turn_id
    assert [item["lineage_depth"] for item in tail.items] == [2]
    assert tail.older_cursor is not None

    with connect_database(path) as connection:
        connection.execute(
            "INSERT INTO chat_turns("
            "turn_id, parent_turn_id, client_request_id, "
            "state, lineage_depth, write_seq, created_at_us, updated_at_us) "
            "VALUES ('new-head', ?, 'new-request', 'completed', 3, "
            "100, 2, 2)",
            (fixed_head,),
        )
        connection.execute(
            "UPDATE chat_meta SET head_turn_id = 'new-head' "
            "WHERE singleton_id = 1",
        )
        connection.commit()

    older = read_turn_page(
        path,
        conversation_id=CONVERSATION_ID,
        subject="agent",
        codec=codec,
        head_turn_id=fixed_head,
        cursor=tail.older_cursor,
        limit=1,
    )

    assert older.view.head_turn_id == fixed_head
    assert [item["lineage_depth"] for item in older.items] == [1]


def test_turn_page_can_hydrate_a_stable_target_window(tmp_path: Path) -> None:
    path = _seed(tmp_path)
    codec = ChatCursorCodec(b"k" * 32)
    tail = read_turn_page(
        path,
        conversation_id=CONVERSATION_ID,
        subject="agent",
        codec=codec,
    )
    target_turn_id = str(tail.items[1]["turn_id"])

    target = read_turn_page(
        path,
        conversation_id=CONVERSATION_ID,
        subject="agent",
        codec=codec,
        head_turn_id=tail.view.head_turn_id,
        view_version=tail.view.view_version,
        target_turn_id=target_turn_id,
        limit=2,
    )

    assert [item["lineage_depth"] for item in target.items] == [0, 1]
    assert target.items[-1]["turn_id"] == target_turn_id
    assert target.newer_cursor is not None


def test_turn_target_rejects_cursor_and_unreachable_turn(
    tmp_path: Path,
) -> None:
    path = _seed(tmp_path)
    codec = ChatCursorCodec(b"k" * 32)
    tail = read_turn_page(
        path,
        conversation_id=CONVERSATION_ID,
        subject="agent",
        codec=codec,
        limit=1,
    )
    with pytest.raises(InvalidChatCursorError):
        read_turn_page(
            path,
            conversation_id=CONVERSATION_ID,
            subject="agent",
            codec=codec,
            cursor=tail.older_cursor,
            target_turn_id=str(tail.items[0]["turn_id"]),
        )
    with pytest.raises(ChatPageNotFoundError):
        read_turn_page(
            path,
            conversation_id=CONVERSATION_ID,
            subject="agent",
            codec=codec,
            target_turn_id="missing-turn",
        )


def test_turn_page_excludes_the_mutable_live_head(tmp_path: Path) -> None:
    path = _seed(tmp_path)
    codec = ChatCursorCodec(b"k" * 32)
    completed = read_turn_page(
        path,
        conversation_id=CONVERSATION_ID,
        subject="agent",
        codec=codec,
    )
    completed_head = completed.view.head_turn_id
    with connect_database(path) as connection:
        connection.execute(
            "INSERT INTO chat_turns("
            "turn_id, parent_turn_id, client_request_id, "
            "state, lineage_depth, write_seq, created_at_us, updated_at_us) "
            "VALUES ('live-head', ?, 'live-request', 'running', 3, "
            "100, 2, 2)",
            (completed_head,),
        )
        connection.execute(
            "UPDATE chat_meta SET head_turn_id = 'live-head' "
            "WHERE singleton_id = 1",
        )
        connection.commit()

    refreshed = read_turn_page(
        path,
        conversation_id=CONVERSATION_ID,
        subject="agent",
        codec=codec,
    )

    assert refreshed.view.head_turn_id == completed_head
    assert all(item["turn_id"] != "live-head" for item in refreshed.items)


def test_same_head_with_changed_version_invalidates_the_view(
    tmp_path: Path,
) -> None:
    path = _seed(tmp_path)
    codec = ChatCursorCodec(b"k" * 32)
    initial = read_turn_page(
        path,
        conversation_id=CONVERSATION_ID,
        subject="agent",
        codec=codec,
        limit=1,
    )
    with connect_database(path) as connection:
        connection.execute(
            "UPDATE chat_turns SET updated_at_us = updated_at_us + 1 "
            "WHERE turn_id = ?",
            (initial.view.head_turn_id,),
        )
        connection.commit()

    with pytest.raises(ChatViewInvalidatedError):
        read_turn_page(
            path,
            conversation_id=CONVERSATION_ID,
            subject="agent",
            codec=codec,
            head_turn_id=initial.view.head_turn_id,
            view_version=initial.view.view_version,
            cursor=initial.older_cursor,
            limit=1,
        )


def test_turn_page_reports_restored_lineage_as_invalidated(
    tmp_path: Path,
) -> None:
    path = _seed(tmp_path)
    codec = ChatCursorCodec(b"k" * 32)
    tail = read_turn_page(
        path,
        conversation_id=CONVERSATION_ID,
        subject="agent",
        codec=codec,
        limit=1,
    )
    with connect_database(path) as connection:
        connection.execute(
            "INSERT INTO chat_turns("
            "turn_id, client_request_id, state, "
            "lineage_depth, write_seq, created_at_us, updated_at_us) "
            "VALUES ('replacement', 'replacement-request', "
            "'completed', 0, 100, 2, 2)",
        )
        connection.execute(
            "UPDATE chat_meta SET head_turn_id = 'replacement' "
            "WHERE singleton_id = 1",
        )
        connection.commit()

    with pytest.raises(ChatViewInvalidatedError) as raised:
        read_turn_page(
            path,
            conversation_id=CONVERSATION_ID,
            subject="agent",
            codec=codec,
            head_turn_id=tail.view.head_turn_id,
            cursor=tail.older_cursor,
            limit=1,
        )

    assert raised.value.current_head_turn_id == "replacement"


def test_message_page_rejects_turn_outside_the_fixed_lineage(
    tmp_path: Path,
) -> None:
    path = _seed(tmp_path)
    codec = ChatCursorCodec(b"k" * 32)
    tail = read_turn_page(
        path,
        conversation_id=CONVERSATION_ID,
        subject="agent",
        codec=codec,
    )
    turn_id = str(tail.items[-1]["turn_id"])

    page = read_message_page(
        path,
        conversation_id=CONVERSATION_ID,
        turn_id=turn_id,
        subject="agent",
        codec=codec,
        head_turn_id=tail.view.head_turn_id,
    )

    assert [item["role"] for item in page.items] == ["user", "assistant"]
    assert all(item["children_complete"] for item in page.items)
    assert all(item["blocks"][0]["content_loaded"] for item in page.items)


def test_large_block_uses_fragments_and_reconstructs_exact_utf8(
    tmp_path: Path,
) -> None:
    huge_text = "长对话" * 100_000
    path = _seed(tmp_path, huge_text=huge_text)
    codec = ChatCursorCodec(b"k" * 32)
    turns = read_turn_page(
        path,
        conversation_id=CONVERSATION_ID,
        subject="agent",
        codec=codec,
    )
    middle = next(item for item in turns.items if item["lineage_depth"] == 1)
    middle_turn = str(middle["turn_id"])
    messages = read_message_page(
        path,
        conversation_id=CONVERSATION_ID,
        turn_id=middle_turn,
        subject="agent",
        codec=codec,
        head_turn_id=turns.view.head_turn_id,
    )
    assistant = messages.items[1]
    blocks = read_block_page(
        path,
        conversation_id=CONVERSATION_ID,
        message_id=str(assistant["message_id"]),
        subject="agent",
        codec=codec,
        head_turn_id=turns.view.head_turn_id,
        max_bytes=128 * 1024,
    )
    block = blocks.items[0]
    assert not block["content_loaded"]
    assert blocks.truncated_entity

    pieces: list[bytes] = []
    cursor = None
    while True:
        page = read_block_fragment(
            path,
            conversation_id=CONVERSATION_ID,
            block_id=str(block["block_id"]),
            subject="agent",
            codec=codec,
            head_turn_id=turns.view.head_turn_id,
            cursor=cursor,
        )
        fragment = page.items[0]
        assert fragment["payload_encoding"] == "base64"
        assert page.bytes <= 256 * 1024
        pieces.append(base64.b64decode(fragment["payload_base64"]))
        cursor = page.newer_cursor
        if cursor is None:
            break

    restored = b"".join(pieces)
    with connect_database(path, readonly=True) as connection:
        expected = b"".join(
            bytes(row["payload"])
            for row in connection.execute(
                "SELECT payload FROM chat_block_segments "
                "WHERE block_id = ? ORDER BY segment_ordinal",
                (block["block_id"],),
            ).fetchall()
        )
    assert restored == expected


def test_turn_page_embeds_normal_children_and_drills_large_block(
    tmp_path: Path,
) -> None:
    path = _seed(tmp_path, huge_text="large" * 600_000)
    codec = ChatCursorCodec(b"k" * 32)

    page = read_turn_page(
        path,
        conversation_id=CONVERSATION_ID,
        subject="agent",
        codec=codec,
    )

    assert page.bytes <= 2 * 1024 * 1024
    assert page.truncated_entity
    large_turn = next(
        item for item in page.items if item["lineage_depth"] == 1
    )
    assert not large_turn["children_complete"]
    large_block = large_turn["messages"][1]["blocks"][0]
    assert not large_block["content_loaded"]
    assert large_block["content_cursor"] is not None

    fragment = read_block_fragment(
        path,
        conversation_id=CONVERSATION_ID,
        block_id=str(large_block["block_id"]),
        subject="agent",
        codec=codec,
        head_turn_id=page.view.head_turn_id,
        cursor=str(large_block["content_cursor"]),
    )
    assert fragment.items[0]["start_byte"] == 0


def test_fragment_cursor_rejects_changed_entity_version(
    tmp_path: Path,
) -> None:
    path = _seed(tmp_path, huge_text="large" * 600_000)
    codec = ChatCursorCodec(b"k" * 32)
    turns = read_turn_page(
        path,
        conversation_id=CONVERSATION_ID,
        subject="agent",
        codec=codec,
    )
    large_turn = next(
        item for item in turns.items if item["lineage_depth"] == 1
    )
    block = large_turn["messages"][1]["blocks"][0]
    first = read_block_fragment(
        path,
        conversation_id=CONVERSATION_ID,
        block_id=str(block["block_id"]),
        subject="agent",
        codec=codec,
        head_turn_id=turns.view.head_turn_id,
        cursor=str(block["content_cursor"]),
    )
    assert first.newer_cursor is not None
    with connect_database(path) as connection:
        connection.execute(
            "UPDATE chat_blocks SET entity_version = entity_version + 1 "
            "WHERE block_id = ?",
            (block["block_id"],),
        )
        connection.commit()

    with pytest.raises(ChatEntityVersionError):
        read_block_fragment(
            path,
            conversation_id=CONVERSATION_ID,
            block_id=str(block["block_id"]),
            subject="agent",
            codec=codec,
            head_turn_id=turns.view.head_turn_id,
            cursor=first.newer_cursor,
        )


def test_tool_result_distinguishes_not_loaded_from_absent(
    tmp_path: Path,
) -> None:
    path = _seed(tmp_path)
    codec = ChatCursorCodec(b"k" * 32)
    initial = read_turn_page(
        path,
        conversation_id=CONVERSATION_ID,
        subject="agent",
        codec=codec,
    )
    first_turn = next(
        item for item in initial.items if item["lineage_depth"] == 0
    )
    call_message = first_turn["messages"][0]
    result_message = first_turn["messages"][1]
    call_block = call_message["blocks"][0]
    result_block = result_message["blocks"][0]
    with connect_database(path) as connection:
        connection.execute(
            "INSERT INTO chat_tool_links("
            "tool_link_id, provider_call_id, call_block_id, "
            "result_block_id, state, tool_name, created_at_us, "
            "updated_at_us) VALUES ('link', 'provider-call', ?, ?, "
            "'completed', 'shell', 2, 2)",
            (call_block["block_id"], result_block["block_id"]),
        )
        connection.commit()

    call_page = read_block_page(
        path,
        conversation_id=CONVERSATION_ID,
        message_id=str(call_message["message_id"]),
        subject="agent",
        codec=codec,
        head_turn_id=initial.view.head_turn_id,
    )
    assert call_page.items[0]["tool_link"]["result_load_state"] == "not_loaded"

    complete_turn = read_turn_page(
        path,
        conversation_id=CONVERSATION_ID,
        subject="agent",
        codec=codec,
        head_turn_id=initial.view.head_turn_id,
    )
    reloaded_first = next(
        item for item in complete_turn.items if item["lineage_depth"] == 0
    )
    assert (
        reloaded_first["messages"][0]["blocks"][0]["tool_link"][
            "result_load_state"
        ]
        == "loaded"
    )


@pytest.mark.slow
def test_64_mib_block_is_bounded_resumable_and_hash_exact(
    tmp_path: Path,
) -> None:
    path = _seed(
        tmp_path,
        huge_text="x" * (64 * 1024 * 1024),
    )
    codec = ChatCursorCodec(b"k" * 32)
    turns = read_turn_page(
        path,
        conversation_id=CONVERSATION_ID,
        subject="agent",
        codec=codec,
    )
    large_turn = next(
        item for item in turns.items if item["lineage_depth"] == 1
    )
    block = large_turn["messages"][1]["blocks"][0]
    assert not block["content_loaded"]
    assert turns.bytes <= 2 * 1024 * 1024

    first = read_block_fragment(
        path,
        conversation_id=CONVERSATION_ID,
        block_id=str(block["block_id"]),
        subject="agent",
        codec=codec,
        head_turn_id=turns.view.head_turn_id,
        cursor=str(block["content_cursor"]),
    )
    assert first.newer_cursor is not None

    digest = hashlib.sha256()
    cursor = str(block["content_cursor"])
    final_hash = None
    expected_start = 0
    while cursor is not None:
        fragment_page = read_block_fragment(
            path,
            conversation_id=CONVERSATION_ID,
            block_id=str(block["block_id"]),
            subject="agent",
            codec=codec,
            head_turn_id=turns.view.head_turn_id,
            cursor=cursor,
        )
        fragment = fragment_page.items[0]
        assert fragment["start_byte"] == expected_start
        assert fragment_page.bytes <= 256 * 1024
        payload = base64.b64decode(fragment["payload_base64"])
        digest.update(payload)
        expected_start = int(fragment["end_byte"])
        final_hash = fragment["content_sha256"]
        cursor = fragment_page.newer_cursor

    assert expected_start == block["total_bytes"]
    assert digest.hexdigest() == final_hash
