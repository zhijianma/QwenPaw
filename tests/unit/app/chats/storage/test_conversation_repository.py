# -*- coding: utf-8 -*-
"""Normalized transcript materialization and read-back tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from qwenpaw.app.chats.integrity import build_transcript_manifest
from qwenpaw.app.chats.migration import (
    NormalizedTranscript,
    create_legacy_snapshot,
)
from qwenpaw.app.chats.storage import (
    ChatStoragePaths,
    ChatStorageSchemaError,
    materialize_normalized_transcript,
    read_transcript,
)
from qwenpaw.app.chats.storage.common import connect_database
from qwenpaw.schemas import (
    DataContent,
    Message,
    MessageType,
    Role,
    RunStatus,
    TextContent,
)


CONVERSATION_ID = "11111111-1111-4111-8111-111111111111"


def _message(
    role: Role,
    text: str,
    *,
    message_type: MessageType = MessageType.MESSAGE,
) -> Message:
    return Message(
        type=message_type,
        role=role,
        content=[TextContent(text=text).completed()],
        status=RunStatus.Completed,
        metadata={"source": "test"},
    )


def _normalized(messages: list[Message]) -> NormalizedTranscript:
    return NormalizedTranscript(
        messages=tuple(messages),
        manifest=build_transcript_manifest(messages),
        history_parts=0,
        session_parts=len(messages),
        deduplicated_parts=0,
    )


def _snapshot(tmp_path: Path):
    return create_legacy_snapshot(
        tmp_path,
        conversation_id=CONVERSATION_ID,
        session_id="session",
        source_paths=[],
    )


def test_materialize_round_trips_turns_tools_and_large_utf8_blocks(
    tmp_path: Path,
) -> None:
    huge_text = "长对话" * 30_000
    messages = [
        _message(Role.USER, "first"),
        Message(
            type=MessageType.PLUGIN_CALL,
            role=Role.ASSISTANT,
            content=[
                DataContent(
                    data={
                        "call_id": "call-1",
                        "name": "shell",
                        "arguments": '{"cmd":"pwd"}',
                    },
                ).completed(),
            ],
            status=RunStatus.Completed,
        ),
        Message(
            type=MessageType.PLUGIN_CALL_OUTPUT,
            role=Role.ASSISTANT,
            content=[
                DataContent(
                    data={
                        "call_id": "call-1",
                        "name": "shell",
                        "output": "ok",
                    },
                ).completed(),
            ],
            status=RunStatus.Completed,
        ),
        _message(Role.ASSISTANT, huge_text),
        _message(Role.USER, "second"),
        Message(
            type=MessageType.MESSAGE,
            role=Role.ASSISTANT,
            content=[TextContent(text="done").completed()],
            status=RunStatus.Completed,
            metadata={"qwenpaw_turn_usage": {"total_tokens": 42}},
        ),
    ]
    normalized = _normalized(messages)
    paths = ChatStoragePaths(tmp_path)
    paths.ensure_layout()
    target = paths.conversation(CONVERSATION_ID)

    result = materialize_normalized_transcript(
        target,
        conversation_id=CONVERSATION_ID,
        agent_id="agent",
        owner_scope="console:user",
        backend="qwenpaw",
        normalized=normalized,
        snapshot=_snapshot(tmp_path),
        now_us=1,
        allocated_recall_sequences=range(100, 106),
    )

    assert result.turns == 2
    assert result.messages == 6
    assert result.blocks == 6
    assert result.source_manifest == result.stored_manifest
    restored = read_transcript(target)
    assert build_transcript_manifest(restored) == normalized.manifest
    with connect_database(target, readonly=True) as connection:
        link = connection.execute(
            "SELECT provider_call_id, state FROM chat_tool_links",
        ).fetchone()
        assert tuple(link) == ("call-1", "completed")
        segments = connection.execute(
            "SELECT payload FROM chat_block_segments AS s "
            "JOIN chat_blocks AS b ON b.block_id = s.block_id "
            "JOIN chat_messages AS m ON m.message_id = b.message_id "
            "WHERE m.ordinal = 3 ORDER BY s.segment_ordinal",
        ).fetchall()
        assert len(segments) > 1
        for segment in segments:
            bytes(segment["payload"]).decode("utf-8")
        turns = connection.execute(
            "SELECT lineage_depth, usage_json FROM chat_turns "
            "ORDER BY lineage_depth",
        ).fetchall()
        assert turns[0]["usage_json"] is None
        assert turns[1]["usage_json"] == '{"total_tokens":42}'
        outbox = connection.execute(
            "SELECT projection, operation FROM chat_projection_outbox "
            "ORDER BY projection",
        ).fetchall()
        assert [tuple(row) for row in outbox] == [
            ("catalog", "migration_ready"),
            ("recall", "migration_ready"),
        ]
        recall_sequences = connection.execute(
            "SELECT recall_seq FROM chat_blocks ORDER BY recall_seq",
        ).fetchall()
        assert [row["recall_seq"] for row in recall_sequences] == list(
            range(100, 106),
        )


def test_materialize_rejects_nonempty_conversation_database(
    tmp_path: Path,
) -> None:
    messages = [_message(Role.USER, "first")]
    normalized = _normalized(messages)
    paths = ChatStoragePaths(tmp_path)
    paths.ensure_layout()
    target = paths.conversation(CONVERSATION_ID)
    snapshot = _snapshot(tmp_path)
    kwargs = {
        "conversation_id": CONVERSATION_ID,
        "agent_id": "agent",
        "owner_scope": "console:user",
        "backend": "qwenpaw",
        "normalized": normalized,
        "snapshot": snapshot,
        "now_us": 1,
    }
    materialize_normalized_transcript(target, **kwargs)

    with pytest.raises(ChatStorageSchemaError, match="non-empty"):
        materialize_normalized_transcript(target, **kwargs)
