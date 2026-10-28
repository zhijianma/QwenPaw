# -*- coding: utf-8 -*-
"""Workspace Chat recall projection schema tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from qwenpaw.app.chats.storage import (
    RECALL_SCHEMA_VERSION,
    ChatStoragePaths,
    ChatStorageSchemaError,
    initialize_recall_database,
    validate_recall_database,
)
from qwenpaw.app.chats.storage.common import connect_database


def _recall_path(tmp_path: Path) -> Path:
    paths = ChatStoragePaths(tmp_path / "workspace")
    paths.ensure_layout()
    return paths.recall


def test_recall_initialization_is_idempotent_and_configured(
    tmp_path: Path,
) -> None:
    path = _recall_path(tmp_path)

    initialize_recall_database(path)
    initialize_recall_database(path)

    assert validate_recall_database(path) == RECALL_SCHEMA_VERSION
    with connect_database(path, readonly=True) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        fts_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'chat_recall_fts'",
        ).fetchone()[0]
        assert "chat_recall_documents" in fts_sql


def test_recall_enforces_stable_sequence_and_source_identity(
    tmp_path: Path,
) -> None:
    path = _recall_path(tmp_path)
    initialize_recall_database(path)
    values = (
        1,
        "conversation",
        "turn",
        "message",
        "block",
        "text",
        "hello",
        1,
        1,
        1,
    )
    with connect_database(path) as connection:
        connection.execute(
            "INSERT INTO chat_recall_documents("
            "recall_seq, conversation_id, turn_id, message_id, "
            "block_id, block_kind, content, source_generation, "
            "created_at_us, updated_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO chat_recall_documents("
                "recall_seq, conversation_id, turn_id, message_id, "
                "block_id, block_kind, content, source_generation, "
                "created_at_us, updated_at_us) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (2, *values[1:]),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO chat_recall_documents("
                "recall_seq, conversation_id, turn_id, message_id, "
                "block_id, block_kind, content, source_generation, "
                "created_at_us, updated_at_us) "
                "VALUES (0, 'c', 't', 'm', 'x', 'text', 'bad', 1, 1, 1)",
            )


def test_recall_fts_projection_can_be_rebuilt(tmp_path: Path) -> None:
    path = _recall_path(tmp_path)
    initialize_recall_database(path)
    with connect_database(path) as connection:
        connection.execute(
            "INSERT INTO chat_recall_documents("
            "recall_seq, conversation_id, turn_id, message_id, "
            "block_id, block_kind, content, source_generation, "
            "created_at_us, updated_at_us) "
            "VALUES (1, 'c', 't', 'm', 'x', 'text', "
            "'searchable phrase', 1, 1, 1)",
        )
        connection.execute(
            "INSERT INTO chat_recall_fts(chat_recall_fts) VALUES ('rebuild')",
        )
        result = connection.execute(
            "SELECT rowid FROM chat_recall_fts "
            "WHERE chat_recall_fts MATCH 'searchable'",
        ).fetchall()
        assert len(result) == 1


def test_recall_validator_rejects_wrong_projection_index(
    tmp_path: Path,
) -> None:
    path = _recall_path(tmp_path)
    initialize_recall_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX ix_chat_recall_projection_state")
        connection.execute(
            "CREATE INDEX ix_chat_recall_projection_state "
            "ON chat_recall_projection_state(updated_at_us, state)",
        )

    with pytest.raises(ChatStorageSchemaError, match="has columns"):
        validate_recall_database(path)


def test_recall_rejects_nonempty_unversioned_database(tmp_path: Path) -> None:
    path = _recall_path(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")

    with pytest.raises(ChatStorageSchemaError, match="non-empty unversioned"):
        initialize_recall_database(path)
