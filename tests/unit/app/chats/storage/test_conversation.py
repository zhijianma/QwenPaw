# -*- coding: utf-8 -*-
"""Per-conversation Chat database contract tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from qwenpaw.app.chats.storage import (
    CONVERSATION_SCHEMA_VERSION,
    ChatStoragePaths,
    ChatStorageSchemaError,
    initialize_conversation_database,
    validate_conversation_database,
)
from qwenpaw.app.chats.storage.common import connect_database


CONVERSATION_ID = "11111111-1111-4111-8111-111111111111"
CONTEXT_ID = "33333333-3333-4333-8333-333333333333"


def _conversation_path(tmp_path: Path) -> Path:
    paths = ChatStoragePaths(tmp_path / "workspace")
    paths.ensure_layout()
    return paths.conversation(CONVERSATION_ID)


def _initialize(path: Path) -> None:
    initialize_conversation_database(
        path,
        conversation_id=CONVERSATION_ID,
        agent_id="agent",
        owner_scope="owner",
        backend="qwenpaw",
        created_at_us=1,
    )


def _insert_initial_timeline(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO chat_context_revisions("
        "context_revision_id, kind, write_seq, created_at_us) "
        "VALUES (?, 'initial', 1, 1)",
        (CONTEXT_ID,),
    )
    connection.execute(
        "UPDATE chat_meta SET current_context_revision_id = ?, write_seq = 1",
        (CONTEXT_ID,),
    )


def test_conversation_initialization_is_idempotent_and_identified(
    tmp_path: Path,
) -> None:
    path = _conversation_path(tmp_path)

    _initialize(path)
    _initialize(path)

    assert (
        validate_conversation_database(
            path,
            expected_conversation_id=CONVERSATION_ID,
        )
        == CONVERSATION_SCHEMA_VERSION
    )
    with connect_database(path, readonly=True) as connection:
        row = connection.execute(
            "SELECT conversation_id, backend FROM chat_meta",
        ).fetchone()
        assert tuple(row) == (CONVERSATION_ID, "qwenpaw")
        fts = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'chat_blocks_fts'",
        ).fetchone()[0]
        assert "fts5" in fts.lower()


def test_conversation_rejects_identity_mismatch(tmp_path: Path) -> None:
    path = _conversation_path(tmp_path)
    _initialize(path)

    with pytest.raises(ChatStorageSchemaError, match="identity"):
        validate_conversation_database(
            path,
            expected_conversation_id="99999999-9999-4999-8999-999999999999",
        )
    with pytest.raises(ChatStorageSchemaError, match="backend"):
        validate_conversation_database(path, expected_backend="other")


def test_conversation_enforces_one_active_run_per_chat(
    tmp_path: Path,
) -> None:
    path = _conversation_path(tmp_path)
    _initialize(path)
    with connect_database(path) as connection:
        _insert_initial_timeline(connection)
        for ordinal in range(2):
            turn_id = f"44444444-4444-4444-8444-44444444444{ordinal}"
            run_id = f"55555555-5555-4555-8555-55555555555{ordinal}"
            connection.execute(
                "INSERT INTO chat_turns("
                "turn_id, client_request_id, state, "
                "lineage_depth, write_seq, created_at_us, updated_at_us) "
                "VALUES (?, ?, 'accepted', ?, ?, 1, 1)",
                (
                    turn_id,
                    f"request-{ordinal}",
                    ordinal,
                    ordinal + 2,
                ),
            )
            if ordinal == 0:
                connection.execute(
                    "INSERT INTO chat_runs("
                    "run_id, turn_id, client_request_id, state, "
                    "fencing_token, created_at_us, updated_at_us) "
                    "VALUES (?, ?, ?, 'running', 1, 1, 1)",
                    (run_id, turn_id, f"request-{ordinal}"),
                )
            else:
                with pytest.raises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO chat_runs("
                        "run_id, turn_id, client_request_id, "
                        "state, fencing_token, created_at_us, updated_at_us) "
                        "VALUES (?, ?, ?, 'accepted', 2, 1, 1)",
                        (run_id, turn_id, f"request-{ordinal}"),
                    )


def test_conversation_enforces_segment_ranges_and_foreign_keys(
    tmp_path: Path,
) -> None:
    path = _conversation_path(tmp_path)
    _initialize(path)
    with connect_database(path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO chat_block_segments("
                "block_id, segment_ordinal, start_byte, end_byte, payload, "
                "segment_sha256, created_at_us) "
                "VALUES ('missing', 0, 0, 0, X'00', 'hash', 1)",
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE chat_meta SET head_turn_id = 'missing' "
                "WHERE singleton_id = 1",
            )


def test_validator_rejects_missing_active_run_partial_index(
    tmp_path: Path,
) -> None:
    path = _conversation_path(tmp_path)
    _initialize(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX ux_chat_runs_active")
        connection.execute(
            "CREATE UNIQUE INDEX ux_chat_runs_active "
            "ON chat_runs(active_slot)",
        )

    with pytest.raises(ChatStorageSchemaError, match="partial flag"):
        validate_conversation_database(path)


def test_validator_rejects_name_complete_wrong_column_type(
    tmp_path: Path,
) -> None:
    path = _conversation_path(tmp_path)
    _initialize(path)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            ALTER TABLE chat_migration_record RENAME TO old_migration_record;
            CREATE TABLE chat_migration_record (
                singleton_id INTEGER PRIMARY KEY NOT NULL
                    CHECK (singleton_id = 1),
                source_locator TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL,
                migration_version TEXT NOT NULL
                    CHECK (migration_version > 0),
                transcript_manifest_json TEXT NOT NULL,
                source_metadata_json TEXT NOT NULL DEFAULT '{}',
                completed_at_us INTEGER NOT NULL
            );
            DROP TABLE old_migration_record;
            """,
        )

    with pytest.raises(
        ChatStorageSchemaError,
        match="migration_version has type TEXT",
    ):
        validate_conversation_database(path)


def test_conversation_rejects_nonempty_unversioned_database(
    tmp_path: Path,
) -> None:
    path = _conversation_path(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")

    with pytest.raises(ChatStorageSchemaError, match="non-empty unversioned"):
        _initialize(path)
