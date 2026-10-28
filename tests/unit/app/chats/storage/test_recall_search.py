# -*- coding: utf-8 -*-
"""Tests for bounded UI search over the Chat recall projection."""

from pathlib import Path

from qwenpaw.app.chats.storage.common import connect_database
from qwenpaw.app.chats.storage.recall import initialize_recall_database
from qwenpaw.app.chats.storage.recall_search import search_chat_recall


def _insert_document(
    path: Path,
    *,
    recall_seq: int,
    content: str,
    role: str,
) -> None:
    conversation_id = f"conversation-{recall_seq}"
    turn_id = f"turn-{recall_seq}"
    message_id = f"message-{recall_seq}"
    block_id = f"block-{recall_seq}"
    with connect_database(path) as connection:
        cursor = connection.execute(
            "INSERT INTO chat_recall_documents("
            "recall_seq, conversation_id, turn_id, message_id, "
            "block_id, block_kind, content, source_generation, "
            "created_at_us, updated_at_us) "
            "VALUES (?, ?, ?, ?, ?, 'text', ?, 1, ?, ?)",
            (
                recall_seq,
                conversation_id,
                turn_id,
                message_id,
                block_id,
                content,
                recall_seq,
                recall_seq,
            ),
        )
        row_id = int(cursor.lastrowid)
        connection.execute(
            "INSERT INTO chat_recall_fts(rowid, content) VALUES (?, ?)",
            (row_id, content),
        )
        connection.execute(
            "INSERT INTO conversation_history("
            "seq, session_id, kind, role, content, conversation_id, "
            "turn_id, message_id, block_id) "
            "VALUES (?, ?, 'context_msg', ?, ?, ?, ?, ?, ?)",
            (
                recall_seq,
                f"session-{recall_seq}",
                role,
                content,
                conversation_id,
                turn_id,
                message_id,
                block_id,
            ),
        )
        connection.commit()


def test_recall_search_is_keyset_paginated_and_keeps_stable_targets(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chat_recall.db"
    initialize_recall_database(path)
    _insert_document(
        path,
        recall_seq=1,
        content="C++ rendering baseline",
        role="user",
    )
    _insert_document(
        path,
        recall_seq=2,
        content="C++ rendering follow-up",
        role="assistant",
    )

    first = search_chat_recall(path, query="C++", limit=1)
    second = search_chat_recall(
        path,
        query="C++",
        before_seq=first.next_before_seq,
        limit=1,
    )

    assert [item.recall_seq for item in first.items] == [2]
    assert first.next_before_seq == 2
    assert first.items[0].turn_id == "turn-2"
    assert first.items[0].role == "assistant"
    assert [item.recall_seq for item in second.items] == [1]
    assert second.next_before_seq is None


def test_recall_search_does_not_scan_for_empty_or_missing_projection(
    tmp_path: Path,
) -> None:
    missing = search_chat_recall(
        tmp_path / "missing.db",
        query="rendering",
    )
    assert not missing.items
    assert not search_chat_recall(
        tmp_path / "missing.db",
        query="++",
    ).items
