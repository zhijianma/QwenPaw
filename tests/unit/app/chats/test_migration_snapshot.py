# -*- coding: utf-8 -*-
"""Stable legacy Chat snapshot tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from qwenpaw.app.chats.migration import (
    LegacySnapshotError,
    create_legacy_snapshot,
    validate_legacy_snapshot,
)
from qwenpaw.app.chats.storage import ChatStoragePaths


CONVERSATION_ID = "11111111-1111-4111-8111-111111111111"


def _history_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
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
        connection.executemany(
            "INSERT INTO conversation_history("
            "session_id, kind, role, content, dedup_key) "
            "VALUES (?, 'model_turn', 'user', ?, ?)",
            (
                ("target", "first", "m1"),
                ("other", "private", "x1"),
                ("target", "second", "m2"),
            ),
        )


def test_snapshot_copies_bytes_and_only_target_history_rows(
    tmp_path: Path,
) -> None:
    session = tmp_path / "sessions" / "console" / "session.json"
    session.parent.mkdir(parents=True)
    source_bytes = b"{ invalid transcript bytes are still snapshotted"
    session.write_bytes(source_bytes)
    _history_database(tmp_path / "history.db")

    manifest = create_legacy_snapshot(
        tmp_path,
        conversation_id=CONVERSATION_ID,
        session_id="target",
        source_paths=["console/session.json"],
    )

    validate_legacy_snapshot(tmp_path, manifest)
    assert session.read_bytes() == source_bytes
    assert manifest.history_rows == 2
    assert manifest.history_min_seq == 1
    assert manifest.history_max_seq == 3
    root = (
        ChatStoragePaths(tmp_path).root
        / "migration"
        / CONVERSATION_ID
        / "snapshot"
    )
    copied = root / manifest.files[0].snapshot_path
    assert copied.read_bytes() == source_bytes
    history = root / str(manifest.history_rows_path)
    rows = [json.loads(line) for line in history.read_text().splitlines()]
    assert [row["content"] for row in rows] == ["first", "second"]
    assert {row["session_id"] for row in rows} == {"target"}


def test_snapshot_validation_detects_session_and_history_corruption(
    tmp_path: Path,
) -> None:
    session = tmp_path / "sessions" / "session.json"
    session.parent.mkdir(parents=True)
    session.write_text("{}", encoding="utf-8")
    _history_database(tmp_path / "history.db")
    manifest = create_legacy_snapshot(
        tmp_path,
        conversation_id=CONVERSATION_ID,
        session_id="target",
        source_paths=["session.json"],
    )
    root = (
        ChatStoragePaths(tmp_path).root
        / "migration"
        / CONVERSATION_ID
        / "snapshot"
    )
    copied = root / manifest.files[0].snapshot_path
    copied.write_text("changed", encoding="utf-8")
    with pytest.raises(LegacySnapshotError, match="file failed"):
        validate_legacy_snapshot(tmp_path, manifest)

    copied.write_text("{}", encoding="utf-8")
    history = root / str(manifest.history_rows_path)
    history.write_text("changed", encoding="utf-8")
    with pytest.raises(LegacySnapshotError, match="history snapshot"):
        validate_legacy_snapshot(tmp_path, manifest)


@pytest.mark.parametrize(
    "unsafe",
    ["../outside.json", "/absolute.json", "nested/../../outside.json"],
)
def test_snapshot_rejects_unsafe_source_paths(
    tmp_path: Path,
    unsafe: str,
) -> None:
    with pytest.raises(LegacySnapshotError, match="not safe"):
        create_legacy_snapshot(
            tmp_path,
            conversation_id=CONVERSATION_ID,
            session_id="target",
            source_paths=[unsafe],
        )


def test_snapshot_without_history_does_not_create_fake_rows(
    tmp_path: Path,
) -> None:
    session = tmp_path / "sessions" / "session.json"
    session.parent.mkdir(parents=True)
    session.write_text("{}", encoding="utf-8")

    manifest = create_legacy_snapshot(
        tmp_path,
        conversation_id=CONVERSATION_ID,
        session_id="target",
        source_paths=["session.json"],
    )

    assert manifest.history_rows == 0
    assert manifest.history_rows_path is None
    assert manifest.history_sha256 is None
    validate_legacy_snapshot(tmp_path, manifest)


def test_snapshot_rejects_noncanonical_conversation_identity(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="Invalid conversation UUID"):
        create_legacy_snapshot(
            tmp_path,
            conversation_id="../escape",
            session_id="target",
            source_paths=[],
        )


def test_snapshot_accepts_legacy_history_without_optional_columns(
    tmp_path: Path,
) -> None:
    with sqlite3.connect(tmp_path / "history.db") as connection:
        connection.execute(
            "CREATE TABLE conversation_history("
            "seq INTEGER PRIMARY KEY, session_id TEXT NOT NULL, "
            "kind TEXT NOT NULL)",
        )
        connection.execute(
            "INSERT INTO conversation_history VALUES (7, 'target', 'event')",
        )

    manifest = create_legacy_snapshot(
        tmp_path,
        conversation_id=CONVERSATION_ID,
        session_id="target",
        source_paths=[],
    )

    assert manifest.history_rows == 1
    root = (
        ChatStoragePaths(tmp_path).root
        / "migration"
        / CONVERSATION_ID
        / "snapshot"
    )
    row = json.loads(
        (root / str(manifest.history_rows_path)).read_text().strip(),
    )
    assert row["seq"] == 7
    assert row["content"] is None
