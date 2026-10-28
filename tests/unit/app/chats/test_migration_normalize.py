# -*- coding: utf-8 -*-
"""Legacy snapshot normalization and deduplication tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from qwenpaw.app.chats import utils as chats_utils
from qwenpaw.app.chats.migration import (
    LegacyNormalizationError,
    create_legacy_snapshot,
    normalize_legacy_snapshot,
)
from qwenpaw.constant import QWENPAW_USER_CONTENT_KEY


CONVERSATION_ID = "11111111-1111-4111-8111-111111111111"
CREATED_AT = "2026-01-01T00:00:00"


@pytest.fixture(autouse=True)
def _fixed_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        chats_utils,
        "load_config",
        lambda: SimpleNamespace(user_timezone="UTC"),
    )


def _message(message_id: str, role: str, text: str) -> dict:
    return {
        "id": message_id,
        "name": role,
        "role": role,
        "content": [{"type": "text", "text": text}],
        "created_at": CREATED_AT,
    }


def _session(workspace: Path, messages: list[dict]) -> None:
    sessions = workspace / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "session.json").write_text(
        json.dumps({"agent": {"state": {"context": messages}}}),
        encoding="utf-8",
    )


def _history(workspace: Path, rows: list[tuple[str, str, str]]) -> None:
    with sqlite3.connect(workspace / "history.db") as connection:
        connection.execute(
            """
            CREATE TABLE conversation_history (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                agent_id TEXT,
                kind TEXT NOT NULL,
                role TEXT,
                name TEXT,
                content TEXT,
                tool_call_id TEXT,
                tool_input TEXT,
                tool_state TEXT,
                headline TEXT,
                blocks TEXT,
                metadata TEXT,
                created_at TEXT,
                dedup_key TEXT
            )
            """,
        )
        for message_id, role, text in rows:
            connection.execute(
                "INSERT INTO conversation_history("
                "session_id, kind, role, content, blocks, created_at, "
                "dedup_key) VALUES ('session', 'model_turn', ?, ?, ?, ?, ?)",
                (
                    role,
                    text,
                    json.dumps([{"type": "text", "text": text}]),
                    CREATED_AT,
                    message_id,
                ),
            )


def _snapshot(workspace: Path):
    return create_legacy_snapshot(
        workspace,
        conversation_id=CONVERSATION_ID,
        session_id="session",
        source_paths=["session.json"],
    )


def test_normalize_merges_history_prefix_and_session_tail_deterministically(
    tmp_path: Path,
) -> None:
    _history(
        tmp_path,
        [("u1", "user", "first"), ("a1", "assistant", "reply")],
    )
    _session(
        tmp_path,
        [
            _message("u1", "user", "first"),
            _message("a1", "assistant", "reply"),
            _message("u2", "user", "tail"),
        ],
    )
    manifest = _snapshot(tmp_path)

    first = normalize_legacy_snapshot(tmp_path, manifest)
    second = normalize_legacy_snapshot(tmp_path, manifest)

    assert first.history_parts == 2
    assert first.session_parts == 3
    assert first.deduplicated_parts == 2
    assert first.manifest.message_count == 3
    assert first.manifest.transcript_hash == second.manifest.transcript_hash
    texts = [message.content[0].text for message in first.messages]
    assert texts == ["first", "reply", "tail"]
    assert [
        message.metadata.get("legacy_recall_seq") for message in first.messages
    ] == [1, 2, None]


def test_normalize_rejects_conflicting_stable_message_identity(
    tmp_path: Path,
) -> None:
    _history(tmp_path, [("same", "assistant", "old")])
    _session(tmp_path, [_message("same", "assistant", "new")])
    manifest = _snapshot(tmp_path)

    with pytest.raises(LegacyNormalizationError, match="Conflicting"):
        normalize_legacy_snapshot(tmp_path, manifest)


def test_normalize_preserves_reused_session_message_id_in_session_order(
    tmp_path: Path,
) -> None:
    _history(
        tmp_path,
        [
            ("same", "assistant", "second reply"),
            ("middle", "assistant", "middle reply"),
        ],
    )
    _session(
        tmp_path,
        [
            _message("same", "assistant", "first reply"),
            _message("middle", "assistant", "middle reply"),
            _message("same", "assistant", "second reply"),
        ],
    )

    normalized = normalize_legacy_snapshot(tmp_path, _snapshot(tmp_path))

    assert [message.content[0].text for message in normalized.messages] == [
        "first reply",
        "middle reply",
        "second reply",
    ]
    assert normalized.deduplicated_parts == 2
    assert [
        message.metadata.get("legacy_recall_seq")
        for message in normalized.messages
    ] == [None, 2, 1]


def test_normalize_accepts_original_attachment_and_rendered_history(
    tmp_path: Path,
) -> None:
    rendered = "rendered file content"
    original = [
        {"type": "text", "text": "review this file"},
        {
            "type": "file",
            "file_url": "qwenpaw-file://uploads/review.txt",
            "file_name": "review.txt",
            "file_size": 21,
        },
    ]
    _history(tmp_path, [("with-file", "user", rendered)])
    session_message = _message("with-file", "user", rendered)
    session_message["metadata"] = {QWENPAW_USER_CONTENT_KEY: original}
    _session(tmp_path, [session_message])

    normalized = normalize_legacy_snapshot(tmp_path, _snapshot(tmp_path))

    assert normalized.deduplicated_parts == 1
    assert normalized.messages[0].metadata["legacy_recall_seq"] == 1
    assert [block.type.value for block in normalized.messages[0].content] == [
        "text",
        "file",
    ]
    assert normalized.messages[0].content[0].text == "review this file"


def test_normalize_preserves_unknown_block_as_raw_metadata(
    tmp_path: Path,
) -> None:
    _session(
        tmp_path,
        [
            {
                "id": "unknown",
                "name": "assistant",
                "role": "assistant",
                "content": [
                    {"type": "future_widget", "payload": {"answer": 42}},
                ],
                "created_at": CREATED_AT,
            },
        ],
    )
    manifest = _snapshot(tmp_path)

    normalized = normalize_legacy_snapshot(tmp_path, manifest)

    assert normalized.manifest.message_count == 1
    assert normalized.messages[0].metadata["legacy_unknown_blocks"] == [
        {"type": "future_widget", "payload": {"answer": 42}},
    ]
    assert "legacy_unknown_block" in normalized.messages[0].content[0].text


def test_normalize_rejects_corrupt_session_json(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "session.json").write_text("not-json", encoding="utf-8")
    manifest = _snapshot(tmp_path)

    with pytest.raises(LegacyNormalizationError, match="snapshot is invalid"):
        normalize_legacy_snapshot(tmp_path, manifest)


def test_normalize_replaces_missing_history_timestamp(tmp_path: Path) -> None:
    _history(tmp_path, [("u1", "user", "first")])
    _session(tmp_path, [])
    with sqlite3.connect(tmp_path / "history.db") as connection:
        connection.execute(
            "UPDATE conversation_history SET created_at = NULL",
        )
    manifest = _snapshot(tmp_path)

    normalized = normalize_legacy_snapshot(tmp_path, manifest)

    assert normalized.manifest.message_count == 1


def test_normalize_records_missing_eviction_index_ranges(
    tmp_path: Path,
) -> None:
    _history(
        tmp_path,
        [("u1", "user", "first"), ("u3", "user", "third")],
    )
    with sqlite3.connect(tmp_path / "history.db") as connection:
        connection.execute(
            "UPDATE conversation_history SET seq = 3 WHERE seq = 2",
        )
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "session.json").write_text(
        json.dumps(
            {
                "agent": {
                    "state": {"context": []},
                    "scroll": {
                        "index": {
                            "tiers": [
                                [
                                    {
                                        "seq_lo": 1,
                                        "seq_hi": 4,
                                        "lines": [],
                                    },
                                ],
                            ],
                        },
                    },
                },
            },
        ),
        encoding="utf-8",
    )

    normalized = normalize_legacy_snapshot(tmp_path, _snapshot(tmp_path))

    assert normalized.diagnostics == (
        {
            "code": "legacy_source_gap",
            "evidence": "scroll_eviction_index",
            "missing_ranges": [[2, 2], [4, 4]],
        },
    )


def test_normalize_preserves_and_reports_orphan_tool_result(
    tmp_path: Path,
) -> None:
    _session(
        tmp_path,
        [
            {
                "id": "orphan-result",
                "name": "assistant",
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_result",
                        "id": "call-without-request",
                        "name": "shell",
                        "output": "completed output",
                    },
                ],
                "created_at": CREATED_AT,
            },
        ],
    )

    normalized = normalize_legacy_snapshot(tmp_path, _snapshot(tmp_path))

    assert normalized.manifest.message_count == 1
    assert normalized.manifest.orphan_tool_results == ("call-without-request",)
    result = normalized.messages[0].content[0].data
    assert result["output"] == "completed output"
