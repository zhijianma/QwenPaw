# -*- coding: utf-8 -*-
"""Per-conversation Chat database schema and structural validation."""

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


CONVERSATION_SCHEMA_VERSION = 2

_CONVERSATION_SCHEMA_SQL = """
CREATE TABLE chat_meta (
    singleton_id INTEGER PRIMARY KEY NOT NULL CHECK (singleton_id = 1),
    conversation_id TEXT NOT NULL UNIQUE,
    agent_id TEXT NOT NULL,
    owner_scope TEXT NOT NULL,
    backend TEXT NOT NULL,
    head_turn_id TEXT,
    current_context_revision_id TEXT,
    lifecycle TEXT NOT NULL CHECK (
        lifecycle IN ('ready', 'deleting', 'quarantined')
    ),
    source_generation INTEGER NOT NULL DEFAULT 0
        CHECK (source_generation >= 0),
    write_seq INTEGER NOT NULL DEFAULT 0 CHECK (write_seq >= 0),
    display_metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at_us INTEGER NOT NULL,
    updated_at_us INTEGER NOT NULL,
    FOREIGN KEY(head_turn_id) REFERENCES chat_turns(turn_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(current_context_revision_id) REFERENCES chat_context_revisions(
        context_revision_id
    ) ON DELETE RESTRICT
);

CREATE TABLE chat_runtime_blobs (
    blob_hash TEXT PRIMARY KEY NOT NULL,
    codec TEXT NOT NULL,
    payload BLOB NOT NULL,
    uncompressed_bytes INTEGER NOT NULL CHECK (uncompressed_bytes >= 0),
    created_at_us INTEGER NOT NULL
);

CREATE TABLE chat_context_revisions (
    context_revision_id TEXT PRIMARY KEY NOT NULL,
    parent_context_revision_id TEXT,
    anchor_turn_id TEXT,
    kind TEXT NOT NULL CHECK (
        kind IN ('initial', 'turn_commit', 'clear', 'compact', 'eviction')
    ),
    runtime_blob_hash TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    write_seq INTEGER NOT NULL UNIQUE CHECK (write_seq > 0),
    created_at_us INTEGER NOT NULL,
    FOREIGN KEY(parent_context_revision_id) REFERENCES chat_context_revisions(
        context_revision_id
    ) ON DELETE RESTRICT,
    FOREIGN KEY(anchor_turn_id) REFERENCES chat_turns(turn_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(runtime_blob_hash) REFERENCES chat_runtime_blobs(blob_hash)
        ON DELETE RESTRICT
);

CREATE TABLE chat_turns (
    turn_id TEXT PRIMARY KEY NOT NULL,
    parent_turn_id TEXT,
    context_revision_id TEXT,
    client_request_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN (
            'accepted', 'running', 'completed', 'failed', 'canceled',
            'interrupted'
        )
    ),
    lineage_depth INTEGER NOT NULL CHECK (lineage_depth >= 0),
    usage_json TEXT,
    error_digest TEXT,
    write_seq INTEGER NOT NULL UNIQUE CHECK (write_seq > 0),
    created_at_us INTEGER NOT NULL,
    updated_at_us INTEGER NOT NULL,
    terminal_at_us INTEGER,
    FOREIGN KEY(parent_turn_id) REFERENCES chat_turns(turn_id)
        ON DELETE RESTRICT,
    FOREIGN KEY(context_revision_id) REFERENCES chat_context_revisions(
        context_revision_id
    ) ON DELETE RESTRICT,
    UNIQUE(client_request_id)
);
CREATE INDEX ix_chat_turns_parent
ON chat_turns(parent_turn_id);
CREATE INDEX ix_chat_turns_depth
ON chat_turns(lineage_depth);

CREATE TABLE chat_messages (
    message_id TEXT PRIMARY KEY NOT NULL,
    turn_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (
        role IN ('system', 'user', 'assistant', 'tool', 'developer')
    ),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    state TEXT NOT NULL CHECK (
        state IN ('partial', 'completed', 'failed', 'interrupted')
    ),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at_us INTEGER NOT NULL,
    updated_at_us INTEGER NOT NULL,
    FOREIGN KEY(turn_id) REFERENCES chat_turns(turn_id) ON DELETE CASCADE,
    UNIQUE(turn_id, ordinal)
);
CREATE INDEX ix_chat_messages_turn
ON chat_messages(turn_id, ordinal);

CREATE TABLE chat_blocks (
    block_id TEXT PRIMARY KEY NOT NULL,
    message_id TEXT NOT NULL,
    recall_seq INTEGER UNIQUE CHECK (recall_seq > 0),
    kind TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    state TEXT NOT NULL CHECK (
        state IN ('partial', 'completed', 'failed', 'interrupted')
    ),
    total_bytes INTEGER NOT NULL DEFAULT 0 CHECK (total_bytes >= 0),
    content_sha256 TEXT,
    entity_version INTEGER NOT NULL DEFAULT 0 CHECK (entity_version >= 0),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at_us INTEGER NOT NULL,
    updated_at_us INTEGER NOT NULL,
    FOREIGN KEY(message_id) REFERENCES chat_messages(message_id)
        ON DELETE CASCADE,
    UNIQUE(message_id, ordinal)
);
CREATE INDEX ix_chat_blocks_message
ON chat_blocks(message_id, ordinal);

CREATE TABLE chat_block_segments (
    block_id TEXT NOT NULL,
    segment_ordinal INTEGER NOT NULL CHECK (segment_ordinal >= 0),
    start_byte INTEGER NOT NULL CHECK (start_byte >= 0),
    end_byte INTEGER NOT NULL CHECK (end_byte > start_byte),
    payload BLOB NOT NULL,
    segment_sha256 TEXT NOT NULL,
    created_at_us INTEGER NOT NULL,
    PRIMARY KEY(block_id, segment_ordinal),
    FOREIGN KEY(block_id) REFERENCES chat_blocks(block_id) ON DELETE CASCADE,
    UNIQUE(block_id, start_byte),
    UNIQUE(block_id, end_byte)
);
CREATE INDEX ix_chat_block_segments_range
ON chat_block_segments(block_id, start_byte, end_byte);

CREATE TABLE chat_tool_links (
    tool_link_id TEXT PRIMARY KEY NOT NULL,
    provider_call_id TEXT NOT NULL UNIQUE,
    call_block_id TEXT NOT NULL UNIQUE,
    result_block_id TEXT UNIQUE,
    state TEXT NOT NULL CHECK (
        state IN ('running', 'completed', 'failed', 'interrupted', 'unknown')
    ),
    tool_name TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at_us INTEGER NOT NULL,
    updated_at_us INTEGER NOT NULL,
    FOREIGN KEY(call_block_id) REFERENCES chat_blocks(block_id)
        ON DELETE CASCADE,
    FOREIGN KEY(result_block_id) REFERENCES chat_blocks(block_id)
        ON DELETE SET NULL,
    CHECK (result_block_id IS NULL OR result_block_id != call_block_id)
);

CREATE TABLE chat_runs (
    run_id TEXT PRIMARY KEY NOT NULL,
    active_slot INTEGER NOT NULL DEFAULT 1 CHECK (active_slot = 1),
    turn_id TEXT NOT NULL UNIQUE,
    client_request_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN (
            'accepted', 'running', 'stopping', 'completed', 'failed',
            'canceled', 'interrupted'
        )
    ),
    process_instance_id TEXT,
    fencing_token INTEGER NOT NULL CHECK (fencing_token > 0),
    durable_event_seq INTEGER NOT NULL DEFAULT 0
        CHECK (durable_event_seq >= 0),
    durable_bytes INTEGER NOT NULL DEFAULT 0 CHECK (durable_bytes >= 0),
    error_digest TEXT,
    created_at_us INTEGER NOT NULL,
    updated_at_us INTEGER NOT NULL,
    terminal_at_us INTEGER,
    FOREIGN KEY(turn_id) REFERENCES chat_turns(turn_id) ON DELETE CASCADE,
    UNIQUE(client_request_id)
);
CREATE UNIQUE INDEX ux_chat_runs_active
ON chat_runs(active_slot)
WHERE state IN ('accepted', 'running', 'stopping');
CREATE INDEX ix_chat_runs_state
ON chat_runs(state, updated_at_us);

CREATE TABLE chat_queue_items (
    queue_item_id TEXT PRIMARY KEY NOT NULL,
    turn_id TEXT NOT NULL UNIQUE,
    client_request_id TEXT NOT NULL,
    queue_ordinal INTEGER NOT NULL CHECK (queue_ordinal >= 0),
    input_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN (
            'pending', 'starting', 'completed', 'canceled', 'interrupted'
        )
    ),
    created_at_us INTEGER NOT NULL,
    updated_at_us INTEGER NOT NULL,
    FOREIGN KEY(turn_id) REFERENCES chat_turns(turn_id) ON DELETE CASCADE,
    UNIQUE(client_request_id),
    UNIQUE(queue_ordinal)
);
CREATE INDEX ix_chat_queue_pending
ON chat_queue_items(queue_ordinal)
WHERE state IN ('pending', 'starting');

CREATE TABLE chat_aliases (
    alias_namespace TEXT NOT NULL,
    external_key TEXT NOT NULL,
    owner_scope TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at_us INTEGER NOT NULL,
    updated_at_us INTEGER NOT NULL,
    PRIMARY KEY(alias_namespace, external_key, owner_scope)
);

CREATE TABLE chat_projection_outbox (
    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
    projection TEXT NOT NULL CHECK (projection IN ('catalog', 'recall')),
    operation TEXT NOT NULL,
    source_generation INTEGER NOT NULL CHECK (source_generation > 0),
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (
        state IN ('pending', 'delivered')
    ),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    created_at_us INTEGER NOT NULL,
    updated_at_us INTEGER NOT NULL,
    UNIQUE(projection, source_generation, operation)
);
CREATE INDEX ix_chat_projection_outbox_pending
ON chat_projection_outbox(projection, outbox_id)
WHERE state = 'pending';

CREATE TABLE chat_migration_record (
    singleton_id INTEGER PRIMARY KEY NOT NULL CHECK (singleton_id = 1),
    source_locator TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    migration_version INTEGER NOT NULL CHECK (migration_version > 0),
    transcript_manifest_json TEXT NOT NULL,
    source_metadata_json TEXT NOT NULL DEFAULT '{}',
    completed_at_us INTEGER NOT NULL
);

CREATE VIRTUAL TABLE chat_blocks_fts USING fts5(
    block_id UNINDEXED,
    content,
    tokenize = 'unicode61'
);
"""


def _column(
    storage_type: str,
    *,
    not_null: bool = True,
    primary_key: int = 0,
) -> ColumnContract:
    return ColumnContract(storage_type, not_null, primary_key)


_TABLE_CONTRACTS = {
    "chat_meta": {
        "singleton_id": _column("INTEGER", primary_key=1),
        "conversation_id": _column("TEXT"),
        "agent_id": _column("TEXT"),
        "owner_scope": _column("TEXT"),
        "backend": _column("TEXT"),
        "head_turn_id": _column("TEXT", not_null=False),
        "current_context_revision_id": _column("TEXT", not_null=False),
        "lifecycle": _column("TEXT"),
        "source_generation": _column("INTEGER"),
        "write_seq": _column("INTEGER"),
        "display_metadata_json": _column("TEXT"),
        "created_at_us": _column("INTEGER"),
        "updated_at_us": _column("INTEGER"),
    },
    "chat_runtime_blobs": {
        "blob_hash": _column("TEXT", primary_key=1),
        "codec": _column("TEXT"),
        "payload": _column("BLOB"),
        "uncompressed_bytes": _column("INTEGER"),
        "created_at_us": _column("INTEGER"),
    },
    "chat_context_revisions": {
        "context_revision_id": _column("TEXT", primary_key=1),
        "parent_context_revision_id": _column("TEXT", not_null=False),
        "anchor_turn_id": _column("TEXT", not_null=False),
        "kind": _column("TEXT"),
        "runtime_blob_hash": _column("TEXT", not_null=False),
        "metadata_json": _column("TEXT"),
        "write_seq": _column("INTEGER"),
        "created_at_us": _column("INTEGER"),
    },
    "chat_turns": {
        "turn_id": _column("TEXT", primary_key=1),
        "parent_turn_id": _column("TEXT", not_null=False),
        "context_revision_id": _column("TEXT", not_null=False),
        "client_request_id": _column("TEXT"),
        "state": _column("TEXT"),
        "lineage_depth": _column("INTEGER"),
        "usage_json": _column("TEXT", not_null=False),
        "error_digest": _column("TEXT", not_null=False),
        "write_seq": _column("INTEGER"),
        "created_at_us": _column("INTEGER"),
        "updated_at_us": _column("INTEGER"),
        "terminal_at_us": _column("INTEGER", not_null=False),
    },
    "chat_messages": {
        "message_id": _column("TEXT", primary_key=1),
        "turn_id": _column("TEXT"),
        "role": _column("TEXT"),
        "ordinal": _column("INTEGER"),
        "state": _column("TEXT"),
        "metadata_json": _column("TEXT"),
        "created_at_us": _column("INTEGER"),
        "updated_at_us": _column("INTEGER"),
    },
    "chat_blocks": {
        "block_id": _column("TEXT", primary_key=1),
        "message_id": _column("TEXT"),
        "recall_seq": _column("INTEGER", not_null=False),
        "kind": _column("TEXT"),
        "ordinal": _column("INTEGER"),
        "state": _column("TEXT"),
        "total_bytes": _column("INTEGER"),
        "content_sha256": _column("TEXT", not_null=False),
        "entity_version": _column("INTEGER"),
        "metadata_json": _column("TEXT"),
        "created_at_us": _column("INTEGER"),
        "updated_at_us": _column("INTEGER"),
    },
    "chat_block_segments": {
        "block_id": _column("TEXT", primary_key=1),
        "segment_ordinal": _column("INTEGER", primary_key=2),
        "start_byte": _column("INTEGER"),
        "end_byte": _column("INTEGER"),
        "payload": _column("BLOB"),
        "segment_sha256": _column("TEXT"),
        "created_at_us": _column("INTEGER"),
    },
    "chat_tool_links": {
        "tool_link_id": _column("TEXT", primary_key=1),
        "provider_call_id": _column("TEXT"),
        "call_block_id": _column("TEXT"),
        "result_block_id": _column("TEXT", not_null=False),
        "state": _column("TEXT"),
        "tool_name": _column("TEXT", not_null=False),
        "metadata_json": _column("TEXT"),
        "created_at_us": _column("INTEGER"),
        "updated_at_us": _column("INTEGER"),
    },
    "chat_runs": {
        "run_id": _column("TEXT", primary_key=1),
        "active_slot": _column("INTEGER"),
        "turn_id": _column("TEXT"),
        "client_request_id": _column("TEXT"),
        "state": _column("TEXT"),
        "process_instance_id": _column("TEXT", not_null=False),
        "fencing_token": _column("INTEGER"),
        "durable_event_seq": _column("INTEGER"),
        "durable_bytes": _column("INTEGER"),
        "error_digest": _column("TEXT", not_null=False),
        "created_at_us": _column("INTEGER"),
        "updated_at_us": _column("INTEGER"),
        "terminal_at_us": _column("INTEGER", not_null=False),
    },
    "chat_queue_items": {
        "queue_item_id": _column("TEXT", primary_key=1),
        "turn_id": _column("TEXT"),
        "client_request_id": _column("TEXT"),
        "queue_ordinal": _column("INTEGER"),
        "input_json": _column("TEXT"),
        "state": _column("TEXT"),
        "created_at_us": _column("INTEGER"),
        "updated_at_us": _column("INTEGER"),
    },
    "chat_aliases": {
        "alias_namespace": _column("TEXT", primary_key=1),
        "external_key": _column("TEXT", primary_key=2),
        "owner_scope": _column("TEXT", primary_key=3),
        "metadata_json": _column("TEXT"),
        "created_at_us": _column("INTEGER"),
        "updated_at_us": _column("INTEGER"),
    },
    "chat_projection_outbox": {
        "outbox_id": _column("INTEGER", not_null=False, primary_key=1),
        "projection": _column("TEXT"),
        "operation": _column("TEXT"),
        "source_generation": _column("INTEGER"),
        "payload_json": _column("TEXT"),
        "state": _column("TEXT"),
        "attempt": _column("INTEGER"),
        "created_at_us": _column("INTEGER"),
        "updated_at_us": _column("INTEGER"),
    },
    "chat_migration_record": {
        "singleton_id": _column("INTEGER", primary_key=1),
        "source_locator": _column("TEXT"),
        "source_fingerprint": _column("TEXT"),
        "migration_version": _column("INTEGER"),
        "transcript_manifest_json": _column("TEXT"),
        "source_metadata_json": _column("TEXT"),
        "completed_at_us": _column("INTEGER"),
    },
}

_INDEX_CONTRACTS = {
    "ix_chat_turns_parent": IndexContract("chat_turns", ("parent_turn_id",)),
    "ix_chat_turns_depth": IndexContract(
        "chat_turns",
        ("lineage_depth",),
    ),
    "ix_chat_messages_turn": IndexContract(
        "chat_messages",
        ("turn_id", "ordinal"),
    ),
    "ix_chat_blocks_message": IndexContract(
        "chat_blocks",
        ("message_id", "ordinal"),
    ),
    "ix_chat_block_segments_range": IndexContract(
        "chat_block_segments",
        ("block_id", "start_byte", "end_byte"),
    ),
    "ux_chat_runs_active": IndexContract(
        "chat_runs",
        ("active_slot",),
        unique=True,
        partial=True,
    ),
    "ix_chat_runs_state": IndexContract(
        "chat_runs",
        ("state", "updated_at_us"),
    ),
    "ix_chat_queue_pending": IndexContract(
        "chat_queue_items",
        ("queue_ordinal",),
        partial=True,
    ),
    "ix_chat_projection_outbox_pending": IndexContract(
        "chat_projection_outbox",
        ("projection", "outbox_id"),
        partial=True,
    ),
}

_FOREIGN_KEY_CONTRACTS = (
    ForeignKeyContract(
        "chat_meta",
        "head_turn_id",
        "chat_turns",
        "turn_id",
        "RESTRICT",
    ),
    ForeignKeyContract(
        "chat_meta",
        "current_context_revision_id",
        "chat_context_revisions",
        "context_revision_id",
        "RESTRICT",
    ),
    ForeignKeyContract(
        "chat_context_revisions",
        "parent_context_revision_id",
        "chat_context_revisions",
        "context_revision_id",
        "RESTRICT",
    ),
    ForeignKeyContract(
        "chat_context_revisions",
        "anchor_turn_id",
        "chat_turns",
        "turn_id",
        "RESTRICT",
    ),
    ForeignKeyContract(
        "chat_context_revisions",
        "runtime_blob_hash",
        "chat_runtime_blobs",
        "blob_hash",
        "RESTRICT",
    ),
    ForeignKeyContract(
        "chat_turns",
        "parent_turn_id",
        "chat_turns",
        "turn_id",
        "RESTRICT",
    ),
    ForeignKeyContract(
        "chat_messages",
        "turn_id",
        "chat_turns",
        "turn_id",
        "CASCADE",
    ),
    ForeignKeyContract(
        "chat_blocks",
        "message_id",
        "chat_messages",
        "message_id",
        "CASCADE",
    ),
    ForeignKeyContract(
        "chat_block_segments",
        "block_id",
        "chat_blocks",
        "block_id",
        "CASCADE",
    ),
    ForeignKeyContract(
        "chat_tool_links",
        "call_block_id",
        "chat_blocks",
        "block_id",
        "CASCADE",
    ),
    ForeignKeyContract(
        "chat_tool_links",
        "result_block_id",
        "chat_blocks",
        "block_id",
        "SET NULL",
    ),
    ForeignKeyContract(
        "chat_runs",
        "turn_id",
        "chat_turns",
        "turn_id",
        "CASCADE",
    ),
    ForeignKeyContract(
        "chat_queue_items",
        "turn_id",
        "chat_turns",
        "turn_id",
        "CASCADE",
    ),
)

_SQL_FRAGMENT_CONTRACTS = {
    "chat_meta": (
        "CHECK (singleton_id = 1)",
        "CHECK (source_generation >= 0)",
        "CHECK (write_seq >= 0)",
        "conversation_id TEXT NOT NULL UNIQUE",
    ),
    "chat_context_revisions": (
        "CHECK (kind IN",
        "CHECK (write_seq > 0)",
        "write_seq INTEGER NOT NULL UNIQUE",
    ),
    "chat_turns": (
        "CHECK (state IN",
        "CHECK (lineage_depth >= 0)",
        "CHECK (write_seq > 0)",
        "UNIQUE(client_request_id)",
    ),
    "chat_messages": (
        "CHECK (role IN",
        "CHECK (ordinal >= 0)",
        "CHECK (state IN",
        "UNIQUE(turn_id, ordinal)",
    ),
    "chat_blocks": (
        "recall_seq INTEGER UNIQUE",
        "CHECK (recall_seq > 0)",
        "CHECK (ordinal >= 0)",
        "CHECK (total_bytes >= 0)",
        "CHECK (entity_version >= 0)",
        "UNIQUE(message_id, ordinal)",
    ),
    "chat_block_segments": (
        "CHECK (segment_ordinal >= 0)",
        "CHECK (start_byte >= 0)",
        "CHECK (end_byte > start_byte)",
        "UNIQUE(block_id, start_byte)",
        "UNIQUE(block_id, end_byte)",
    ),
    "chat_tool_links": (
        "CHECK (state IN",
        "CHECK (result_block_id IS NULL OR result_block_id != call_block_id)",
        "provider_call_id TEXT NOT NULL UNIQUE",
        "call_block_id TEXT NOT NULL UNIQUE",
        "result_block_id TEXT UNIQUE",
    ),
    "chat_runs": (
        "CHECK (state IN",
        "CHECK (active_slot = 1)",
        "CHECK (fencing_token > 0)",
        "CHECK (durable_event_seq >= 0)",
        "CHECK (durable_bytes >= 0)",
        "turn_id TEXT NOT NULL UNIQUE",
        "UNIQUE(client_request_id)",
    ),
    "chat_queue_items": (
        "CHECK (queue_ordinal >= 0)",
        "CHECK (state IN",
        "turn_id TEXT NOT NULL UNIQUE",
        "UNIQUE(client_request_id)",
        "UNIQUE(queue_ordinal)",
    ),
    "chat_projection_outbox": (
        "CHECK (projection IN ('catalog', 'recall'))",
        "CHECK (source_generation > 0)",
        "CHECK (attempt >= 0)",
        "UNIQUE(projection, source_generation, operation)",
    ),
    "chat_migration_record": (
        "CHECK (singleton_id = 1)",
        "CHECK (migration_version > 0)",
    ),
}


def _validate_conversation_connection(
    connection: sqlite3.Connection,
    *,
    expected_conversation_id: str | None = None,
    expected_agent_id: str | None = None,
    expected_owner_scope: str | None = None,
    expected_backend: str | None = None,
) -> None:
    validate_integrity(connection)
    validate_user_version(
        connection,
        expected=CONVERSATION_SCHEMA_VERSION,
        database_name="Chat conversation",
    )
    for table, columns in _TABLE_CONTRACTS.items():
        validate_table(connection, table, columns)
    for table, fragments in _SQL_FRAGMENT_CONTRACTS.items():
        validate_table_sql(connection, table, fragments)
    for name, index in _INDEX_CONTRACTS.items():
        validate_index(connection, index, name=name)
    for foreign_key in _FOREIGN_KEY_CONTRACTS:
        validate_foreign_key(connection, foreign_key)
    fts_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' "
        "AND name = 'chat_blocks_fts'",
    ).fetchone()
    if fts_sql is None or "using fts5" not in str(fts_sql[0]).lower():
        raise ChatStorageSchemaError(
            "Missing required chat_blocks_fts FTS5 table",
        )
    meta = connection.execute(
        "SELECT singleton_id, conversation_id, agent_id, owner_scope, "
        "backend FROM chat_meta",
    ).fetchall()
    if len(meta) != 1 or int(meta[0]["singleton_id"]) != 1:
        raise ChatStorageSchemaError(
            "Chat conversation must contain one meta row",
        )
    if (
        expected_conversation_id is not None
        and str(meta[0]["conversation_id"]) != expected_conversation_id
    ):
        raise ChatStorageSchemaError(
            "Chat conversation identity does not match its catalog entry",
        )
    expected = {
        "agent_id": expected_agent_id,
        "owner_scope": expected_owner_scope,
        "backend": expected_backend,
    }
    for field, value in expected.items():
        if value is not None and str(meta[0][field]) != value:
            raise ChatStorageSchemaError(
                f"Chat conversation {field} does not match its catalog entry",
            )


def initialize_conversation_database(
    path: Path,
    *,
    conversation_id: str,
    agent_id: str,
    owner_scope: str,
    backend: str,
    created_at_us: int,
) -> None:
    """Create an empty conversation database or validate its identity."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect_database(path) as connection:
        version_row = connection.execute("PRAGMA user_version").fetchone()
        version = int(version_row[0] if version_row else 0)
        tables = user_tables(connection)
        if version == 0 and not tables:
            connection.executescript(_CONVERSATION_SCHEMA_SQL)
            connection.execute(
                "INSERT INTO chat_meta("
                "singleton_id, conversation_id, agent_id, owner_scope, "
                "backend, lifecycle, created_at_us, updated_at_us) "
                "VALUES (1, ?, ?, ?, ?, 'ready', ?, ?)",
                (
                    conversation_id,
                    agent_id,
                    owner_scope,
                    backend,
                    created_at_us,
                    created_at_us,
                ),
            )
            connection.execute(
                f"PRAGMA user_version = {CONVERSATION_SCHEMA_VERSION}",
            )
            connection.commit()
        elif version == 0:
            raise ChatStorageSchemaError(
                "Refusing to initialize a non-empty unversioned "
                "Chat conversation",
            )
        _validate_conversation_connection(
            connection,
            expected_conversation_id=conversation_id,
            expected_agent_id=agent_id,
            expected_owner_scope=owner_scope,
            expected_backend=backend,
        )


def validate_conversation_database(
    path: Path,
    *,
    expected_conversation_id: str | None = None,
    expected_agent_id: str | None = None,
    expected_owner_scope: str | None = None,
    expected_backend: str | None = None,
) -> int:
    """Validate an existing per-conversation database read-only."""
    if not path.is_file():
        raise FileNotFoundError(path)
    with connect_database(path, readonly=True) as connection:
        _validate_conversation_connection(
            connection,
            expected_conversation_id=expected_conversation_id,
            expected_agent_id=expected_agent_id,
            expected_owner_scope=expected_owner_scope,
            expected_backend=expected_backend,
        )
    return CONVERSATION_SCHEMA_VERSION
