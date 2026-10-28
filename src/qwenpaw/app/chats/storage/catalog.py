# -*- coding: utf-8 -*-
"""Workspace-level Chat catalog schema and validation."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .common import (
    ChatStorageSchemaError,
    ColumnContract,
    ForeignKeyContract,
    IndexContract,
    connect_database,
    user_tables,
    validate_foreign_key,
    validate_index,
    validate_integrity,
    validate_table,
    validate_table_sql,
    validate_user_version,
)


CATALOG_SCHEMA_VERSION = 2

_CATALOG_SCHEMA_SQL = """
CREATE TABLE chat_groups (
    group_id TEXT PRIMARY KEY NOT NULL,
    agent_id TEXT NOT NULL,
    name TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    kind TEXT NOT NULL CHECK (
        kind IN ('default', 'cron', 'subagents', 'custom')
    ),
    pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
    created_at_us INTEGER NOT NULL,
    updated_at_us INTEGER NOT NULL
);
CREATE UNIQUE INDEX ux_chat_groups_agent_ordinal
ON chat_groups(agent_id, ordinal);

CREATE TABLE chat_catalog_entries (
    conversation_id TEXT PRIMARY KEY NOT NULL,
    agent_id TEXT NOT NULL,
    owner_scope TEXT NOT NULL,
    display_name TEXT NOT NULL,
    spec_json TEXT NOT NULL DEFAULT '{}',
    physical_relpath TEXT NOT NULL,
    lifecycle TEXT NOT NULL CHECK (
        lifecycle IN (
            'creating', 'ready', 'deleting', 'deleted', 'quarantined'
        )
    ),
    pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
    archived_at_us INTEGER,
    group_id TEXT,
    active_run_id TEXT,
    run_state TEXT,
    source_conversation_id TEXT,
    source_turn_id TEXT,
    root_conversation_id TEXT,
    copied_at_us INTEGER,
    source_generation INTEGER NOT NULL DEFAULT 0
        CHECK (source_generation >= 0),
    catalog_generation INTEGER NOT NULL DEFAULT 0
        CHECK (catalog_generation >= 0),
    created_at_us INTEGER NOT NULL,
    updated_at_us INTEGER NOT NULL,
    FOREIGN KEY(group_id) REFERENCES chat_groups(group_id) ON DELETE SET NULL
);
CREATE UNIQUE INDEX ux_chat_catalog_physical_relpath
ON chat_catalog_entries(physical_relpath);
CREATE INDEX ix_chat_catalog_list
ON chat_catalog_entries(agent_id, archived_at_us, pinned, updated_at_us);
CREATE INDEX ix_chat_catalog_nonterminal
ON chat_catalog_entries(run_state)
WHERE run_state IN ('accepted', 'running', 'stopping');

CREATE TABLE chat_alias_index (
    agent_id TEXT NOT NULL,
    alias_namespace TEXT NOT NULL,
    external_key TEXT NOT NULL,
    owner_scope TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    source_generation INTEGER NOT NULL DEFAULT 0,
    created_at_us INTEGER NOT NULL,
    updated_at_us INTEGER NOT NULL,
    PRIMARY KEY(agent_id, alias_namespace, external_key, owner_scope),
    FOREIGN KEY(conversation_id) REFERENCES chat_catalog_entries(
        conversation_id
    ) ON DELETE CASCADE
);
CREATE INDEX ix_chat_alias_conversation
ON chat_alias_index(conversation_id);

CREATE TABLE chat_migrations (
    source_locator TEXT PRIMARY KEY NOT NULL,
    conversation_id TEXT,
    state TEXT NOT NULL CHECK (
        state IN (
            'legacy_only', 'queued', 'snapshotting', 'normalizing',
            'validating', 'ready_v2', 'failed_retryable', 'failed_terminal'
        )
    ),
    priority INTEGER NOT NULL DEFAULT 0,
    source_fingerprint TEXT,
    migration_version INTEGER NOT NULL,
    progress_json TEXT NOT NULL DEFAULT '{}',
    attempt INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_expires_at_us INTEGER,
    error_digest TEXT,
    created_at_us INTEGER NOT NULL,
    updated_at_us INTEGER NOT NULL
);
CREATE INDEX ix_chat_migrations_schedule
ON chat_migrations(state, priority, updated_at_us);

CREATE TABLE chat_deletion_jobs (
    conversation_id TEXT PRIMARY KEY NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'running', 'completed')),
    completed_steps INTEGER NOT NULL DEFAULT 0 CHECK (completed_steps >= 0),
    attempt INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_expires_at_us INTEGER,
    last_error_digest TEXT,
    created_at_us INTEGER NOT NULL,
    updated_at_us INTEGER NOT NULL
);
CREATE INDEX ix_chat_deletion_jobs_schedule
ON chat_deletion_jobs(state, updated_at_us);

CREATE TABLE chat_runtime_intents (
    conversation_id TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('run', 'queue')),
    run_or_queue_id TEXT,
    state TEXT NOT NULL CHECK (state IN ('starting', 'recorded', 'terminal')),
    created_at_us INTEGER NOT NULL,
    updated_at_us INTEGER NOT NULL,
    PRIMARY KEY(conversation_id, client_request_id),
    FOREIGN KEY(conversation_id) REFERENCES chat_catalog_entries(
        conversation_id
    ) ON DELETE CASCADE
);
CREATE INDEX ix_chat_runtime_intents_nonterminal
ON chat_runtime_intents(state, updated_at_us)
WHERE state != 'terminal';

CREATE TABLE chat_recall_sequences (
    workspace_id TEXT PRIMARY KEY NOT NULL,
    high_water INTEGER NOT NULL CHECK (high_water >= 0),
    lease_owner TEXT,
    lease_end INTEGER,
    lease_expires_at_us INTEGER,
    updated_at_us INTEGER NOT NULL
);

CREATE TABLE chat_catalog_repairs (
    repair_id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    expected_generation INTEGER NOT NULL CHECK (expected_generation >= 0),
    state TEXT NOT NULL CHECK (state IN ('pending', 'running', 'completed')),
    lease_owner TEXT,
    lease_expires_at_us INTEGER,
    attempt INTEGER NOT NULL DEFAULT 0,
    created_at_us INTEGER NOT NULL,
    updated_at_us INTEGER NOT NULL,
    FOREIGN KEY(conversation_id) REFERENCES chat_catalog_entries(
        conversation_id
    ) ON DELETE CASCADE
);
CREATE UNIQUE INDEX ux_chat_catalog_repairs_idempotency
ON chat_catalog_repairs(conversation_id, kind, expected_generation);
CREATE INDEX ix_chat_catalog_repairs_schedule
ON chat_catalog_repairs(state, updated_at_us);
"""


def _column(
    storage_type: str,
    *,
    not_null: bool = True,
    primary_key: int = 0,
) -> ColumnContract:
    return ColumnContract(storage_type, not_null, primary_key)


_TABLE_CONTRACTS = {
    "chat_groups": {
        "group_id": _column("TEXT", primary_key=1),
        "agent_id": _column("TEXT"),
        "name": _column("TEXT"),
        "ordinal": _column("INTEGER"),
        "kind": _column("TEXT"),
        "pinned": _column("INTEGER"),
        "created_at_us": _column("INTEGER"),
        "updated_at_us": _column("INTEGER"),
    },
    "chat_catalog_entries": {
        "conversation_id": _column("TEXT", primary_key=1),
        "agent_id": _column("TEXT"),
        "owner_scope": _column("TEXT"),
        "display_name": _column("TEXT"),
        "spec_json": _column("TEXT"),
        "physical_relpath": _column("TEXT"),
        "lifecycle": _column("TEXT"),
        "pinned": _column("INTEGER"),
        "archived_at_us": _column("INTEGER", not_null=False),
        "group_id": _column("TEXT", not_null=False),
        "active_run_id": _column("TEXT", not_null=False),
        "run_state": _column("TEXT", not_null=False),
        "source_conversation_id": _column("TEXT", not_null=False),
        "source_turn_id": _column("TEXT", not_null=False),
        "root_conversation_id": _column("TEXT", not_null=False),
        "copied_at_us": _column("INTEGER", not_null=False),
        "source_generation": _column("INTEGER"),
        "catalog_generation": _column("INTEGER"),
        "created_at_us": _column("INTEGER"),
        "updated_at_us": _column("INTEGER"),
    },
    "chat_alias_index": {
        "agent_id": _column("TEXT", primary_key=1),
        "alias_namespace": _column("TEXT", primary_key=2),
        "external_key": _column("TEXT", primary_key=3),
        "owner_scope": _column("TEXT", primary_key=4),
        "conversation_id": _column("TEXT"),
        "source_generation": _column("INTEGER"),
        "created_at_us": _column("INTEGER"),
        "updated_at_us": _column("INTEGER"),
    },
    "chat_migrations": {
        "source_locator": _column("TEXT", primary_key=1),
        "conversation_id": _column("TEXT", not_null=False),
        "state": _column("TEXT"),
        "priority": _column("INTEGER"),
        "source_fingerprint": _column("TEXT", not_null=False),
        "migration_version": _column("INTEGER"),
        "progress_json": _column("TEXT"),
        "attempt": _column("INTEGER"),
        "lease_owner": _column("TEXT", not_null=False),
        "lease_expires_at_us": _column("INTEGER", not_null=False),
        "error_digest": _column("TEXT", not_null=False),
        "created_at_us": _column("INTEGER"),
        "updated_at_us": _column("INTEGER"),
    },
    "chat_deletion_jobs": {
        "conversation_id": _column("TEXT", primary_key=1),
        "state": _column("TEXT"),
        "completed_steps": _column("INTEGER"),
        "attempt": _column("INTEGER"),
        "lease_owner": _column("TEXT", not_null=False),
        "lease_expires_at_us": _column("INTEGER", not_null=False),
        "last_error_digest": _column("TEXT", not_null=False),
        "created_at_us": _column("INTEGER"),
        "updated_at_us": _column("INTEGER"),
    },
    "chat_runtime_intents": {
        "conversation_id": _column("TEXT", primary_key=1),
        "client_request_id": _column("TEXT", primary_key=2),
        "kind": _column("TEXT"),
        "run_or_queue_id": _column("TEXT", not_null=False),
        "state": _column("TEXT"),
        "created_at_us": _column("INTEGER"),
        "updated_at_us": _column("INTEGER"),
    },
    "chat_recall_sequences": {
        "workspace_id": _column("TEXT", primary_key=1),
        "high_water": _column("INTEGER"),
        "lease_owner": _column("TEXT", not_null=False),
        "lease_end": _column("INTEGER", not_null=False),
        "lease_expires_at_us": _column("INTEGER", not_null=False),
        "updated_at_us": _column("INTEGER"),
    },
    "chat_catalog_repairs": {
        "repair_id": _column("INTEGER", not_null=False, primary_key=1),
        "conversation_id": _column("TEXT"),
        "kind": _column("TEXT"),
        "expected_generation": _column("INTEGER"),
        "state": _column("TEXT"),
        "lease_owner": _column("TEXT", not_null=False),
        "lease_expires_at_us": _column("INTEGER", not_null=False),
        "attempt": _column("INTEGER"),
        "created_at_us": _column("INTEGER"),
        "updated_at_us": _column("INTEGER"),
    },
}

_INDEX_CONTRACTS = {
    "ux_chat_groups_agent_ordinal": IndexContract(
        "chat_groups",
        ("agent_id", "ordinal"),
        unique=True,
    ),
    "ux_chat_catalog_physical_relpath": IndexContract(
        "chat_catalog_entries",
        ("physical_relpath",),
        unique=True,
    ),
    "ix_chat_catalog_list": IndexContract(
        "chat_catalog_entries",
        ("agent_id", "archived_at_us", "pinned", "updated_at_us"),
    ),
    "ix_chat_catalog_nonterminal": IndexContract(
        "chat_catalog_entries",
        ("run_state",),
        partial=True,
    ),
    "ix_chat_alias_conversation": IndexContract(
        "chat_alias_index",
        ("conversation_id",),
    ),
    "ix_chat_migrations_schedule": IndexContract(
        "chat_migrations",
        ("state", "priority", "updated_at_us"),
    ),
    "ix_chat_deletion_jobs_schedule": IndexContract(
        "chat_deletion_jobs",
        ("state", "updated_at_us"),
    ),
    "ix_chat_runtime_intents_nonterminal": IndexContract(
        "chat_runtime_intents",
        ("state", "updated_at_us"),
        partial=True,
    ),
    "ux_chat_catalog_repairs_idempotency": IndexContract(
        "chat_catalog_repairs",
        ("conversation_id", "kind", "expected_generation"),
        unique=True,
    ),
    "ix_chat_catalog_repairs_schedule": IndexContract(
        "chat_catalog_repairs",
        ("state", "updated_at_us"),
    ),
}

_FOREIGN_KEY_CONTRACTS = (
    ForeignKeyContract(
        "chat_catalog_entries",
        "group_id",
        "chat_groups",
        "group_id",
        "SET NULL",
    ),
    ForeignKeyContract(
        "chat_alias_index",
        "conversation_id",
        "chat_catalog_entries",
        "conversation_id",
        "CASCADE",
    ),
    ForeignKeyContract(
        "chat_runtime_intents",
        "conversation_id",
        "chat_catalog_entries",
        "conversation_id",
        "CASCADE",
    ),
    ForeignKeyContract(
        "chat_catalog_repairs",
        "conversation_id",
        "chat_catalog_entries",
        "conversation_id",
        "CASCADE",
    ),
)

_SQL_FRAGMENT_CONTRACTS = {
    "chat_groups": (
        "CHECK (ordinal >= 0)",
        "CHECK (pinned IN (0, 1))",
    ),
    "chat_catalog_entries": (
        "CHECK (pinned IN (0, 1))",
        "CHECK (source_generation >= 0)",
        "CHECK (catalog_generation >= 0)",
    ),
    "chat_migrations": ("CHECK (state IN",),
    "chat_deletion_jobs": (
        "CHECK (state IN ('pending', 'running', 'completed'))",
        "CHECK (completed_steps >= 0)",
    ),
    "chat_runtime_intents": (
        "CHECK (kind IN ('run', 'queue'))",
        "CHECK (state IN ('starting', 'recorded', 'terminal'))",
    ),
    "chat_recall_sequences": ("CHECK (high_water >= 0)",),
    "chat_catalog_repairs": (
        "CHECK (expected_generation >= 0)",
        "CHECK (state IN ('pending', 'running', 'completed'))",
    ),
}


def _validate_catalog_connection(connection: sqlite3.Connection) -> None:
    validate_integrity(connection)
    validate_user_version(
        connection,
        expected=CATALOG_SCHEMA_VERSION,
        database_name="Chat catalog",
    )
    for table, columns in _TABLE_CONTRACTS.items():
        validate_table(connection, table, columns)
    for table, fragments in _SQL_FRAGMENT_CONTRACTS.items():
        validate_table_sql(connection, table, fragments)
    for name, index in _INDEX_CONTRACTS.items():
        validate_index(connection, index, name=name)
    for foreign_key in _FOREIGN_KEY_CONTRACTS:
        validate_foreign_key(connection, foreign_key)


def initialize_catalog_database(path: Path) -> None:
    """Create or validate the workspace catalog without repairing drift."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    with connect_database(path) as connection:
        version_row = connection.execute("PRAGMA user_version").fetchone()
        version = int(version_row[0] if version_row else 0)
        tables = user_tables(connection)
        if version == 0 and not tables:
            connection.executescript(_CATALOG_SCHEMA_SQL)
            connection.execute(
                f"PRAGMA user_version = {CATALOG_SCHEMA_VERSION}",
            )
            connection.commit()
        elif version == 0:
            raise ChatStorageSchemaError(
                "Refusing to initialize a non-empty unversioned Chat catalog",
            )
        _validate_catalog_connection(connection)
    if not existed and not path.is_file():
        raise ChatStorageSchemaError("Chat catalog was not created")


def validate_catalog_database(path: Path) -> int:
    """Validate an existing catalog through a read-only connection."""
    if not path.is_file():
        raise FileNotFoundError(path)
    with connect_database(path, readonly=True) as connection:
        _validate_catalog_connection(connection)
    return CATALOG_SCHEMA_VERSION
