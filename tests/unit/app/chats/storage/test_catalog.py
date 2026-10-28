# -*- coding: utf-8 -*-
"""Catalog schema, path, and structural validation tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from qwenpaw.app.chats.storage import (
    CATALOG_SCHEMA_VERSION,
    ChatStoragePaths,
    ChatStorageSchemaError,
    initialize_catalog_database,
    validate_catalog_database,
)
from qwenpaw.app.chats.storage.common import connect_database


CONVERSATION_ID = "11111111-1111-4111-8111-111111111111"


def _catalog_path(tmp_path: Path) -> Path:
    paths = ChatStoragePaths(tmp_path / "workspace")
    paths.ensure_layout()
    return paths.catalog


def test_storage_paths_use_reserved_cross_platform_layout(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    paths = ChatStoragePaths(workspace)

    paths.ensure_layout()

    assert paths.root == workspace / ".qwenpaw" / "chat"
    assert paths.catalog == paths.root / "chat_catalog.db"
    assert paths.recall == paths.root / "chat_recall.db"
    assert paths.conversation(CONVERSATION_ID) == (
        paths.root / "conversations" / f"{CONVERSATION_ID}.db"
    )
    assert paths.conversations.is_dir()
    assert not paths.catalog.exists()


@pytest.mark.parametrize(
    "invalid",
    [
        "../escape",
        "not-a-uuid",
        "11111111-1111-4111-8111-111111111111.db",
        "11111111-1111-4111-8111-111111111111/child",
        "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
    ],
)
def test_conversation_path_rejects_noncanonical_identity(
    tmp_path: Path,
    invalid: str,
) -> None:
    with pytest.raises(ValueError):
        ChatStoragePaths(tmp_path).conversation(invalid)


def test_catalog_initialization_is_idempotent_and_configured(
    tmp_path: Path,
) -> None:
    path = _catalog_path(tmp_path)

    initialize_catalog_database(path)
    initialize_catalog_database(path)

    assert validate_catalog_database(path) == CATALOG_SCHEMA_VERSION
    with connect_database(path, readonly=True) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_catalog_enforces_foreign_keys_and_unique_physical_paths(
    tmp_path: Path,
) -> None:
    path = _catalog_path(tmp_path)
    initialize_catalog_database(path)
    with connect_database(path) as connection:
        values = (
            CONVERSATION_ID,
            "agent",
            "owner",
            "chat",
            f"conversations/{CONVERSATION_ID}.db",
            "ready",
            1,
            1,
        )
        connection.execute(
            "INSERT INTO chat_catalog_entries("
            "conversation_id, agent_id, owner_scope, display_name, "
            "physical_relpath, lifecycle, created_at_us, updated_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE chat_catalog_entries SET group_id = 'missing' "
                "WHERE conversation_id = ?",
                (CONVERSATION_ID,),
            )
        connection.rollback()
        second_id = "22222222-2222-4222-8222-222222222222"
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO chat_catalog_entries("
                "conversation_id, agent_id, owner_scope, display_name, "
                "physical_relpath, lifecycle, created_at_us, updated_at_us) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (second_id, *values[1:]),
            )


def test_catalog_rejects_nonempty_unversioned_database(tmp_path: Path) -> None:
    path = _catalog_path(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")

    with pytest.raises(ChatStorageSchemaError, match="non-empty unversioned"):
        initialize_catalog_database(path)


def test_catalog_rejects_future_schema_version(tmp_path: Path) -> None:
    path = _catalog_path(tmp_path)
    initialize_catalog_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 999")

    with pytest.raises(ChatStorageSchemaError, match="version 999"):
        validate_catalog_database(path)


def test_validator_rejects_missing_unique_index(tmp_path: Path) -> None:
    path = _catalog_path(tmp_path)
    initialize_catalog_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX ux_chat_catalog_physical_relpath")

    with pytest.raises(ChatStorageSchemaError, match="Missing required index"):
        validate_catalog_database(path)


def test_validator_rejects_wrong_column_type_with_all_names_present(
    tmp_path: Path,
) -> None:
    path = _catalog_path(tmp_path)
    initialize_catalog_database(path)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            ALTER TABLE chat_recall_sequences RENAME TO old_sequences;
            CREATE TABLE chat_recall_sequences (
                workspace_id TEXT PRIMARY KEY NOT NULL,
                high_water TEXT NOT NULL CHECK (high_water >= 0),
                lease_owner TEXT,
                lease_end INTEGER,
                lease_expires_at_us INTEGER,
                updated_at_us INTEGER NOT NULL
            );
            DROP TABLE old_sequences;
            """,
        )

    with pytest.raises(
        ChatStorageSchemaError,
        match="high_water has type TEXT",
    ):
        validate_catalog_database(path)


def test_validator_rejects_missing_check_constraint(tmp_path: Path) -> None:
    path = _catalog_path(tmp_path)
    initialize_catalog_database(path)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            DROP INDEX ix_chat_deletion_jobs_schedule;
            ALTER TABLE chat_deletion_jobs RENAME TO old_deletion_jobs;
            CREATE TABLE chat_deletion_jobs (
                conversation_id TEXT PRIMARY KEY NOT NULL,
                state TEXT NOT NULL,
                completed_steps INTEGER NOT NULL DEFAULT 0
                    CHECK (completed_steps >= 0),
                attempt INTEGER NOT NULL DEFAULT 0,
                lease_owner TEXT,
                lease_expires_at_us INTEGER,
                last_error_digest TEXT,
                created_at_us INTEGER NOT NULL,
                updated_at_us INTEGER NOT NULL
            );
            DROP TABLE old_deletion_jobs;
            CREATE INDEX ix_chat_deletion_jobs_schedule
            ON chat_deletion_jobs(state, updated_at_us);
            """,
        )

    with pytest.raises(ChatStorageSchemaError, match="missing constraint"):
        validate_catalog_database(path)


def test_validator_rejects_missing_foreign_key(tmp_path: Path) -> None:
    path = _catalog_path(tmp_path)
    initialize_catalog_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.executescript(
            """
            DROP INDEX ix_chat_runtime_intents_nonterminal;
            ALTER TABLE chat_runtime_intents RENAME TO old_runtime_intents;
            CREATE TABLE chat_runtime_intents (
                conversation_id TEXT NOT NULL,
                client_request_id TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('run', 'queue')),
                run_or_queue_id TEXT,
                state TEXT NOT NULL
                    CHECK (state IN ('starting', 'recorded', 'terminal')),
                created_at_us INTEGER NOT NULL,
                updated_at_us INTEGER NOT NULL,
                    PRIMARY KEY(conversation_id, client_request_id)
            );
            DROP TABLE old_runtime_intents;
            CREATE INDEX ix_chat_runtime_intents_nonterminal
            ON chat_runtime_intents(state, updated_at_us)
            WHERE state != 'terminal';
            """,
        )

    with pytest.raises(ChatStorageSchemaError, match="Missing foreign key"):
        validate_catalog_database(path)
