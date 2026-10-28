# -*- coding: utf-8 -*-
"""Workspace-level rebuildable Chat recall projection schema."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .common import (
    ChatStorageSchemaError,
    ColumnContract,
    IndexContract,
    connect_database,
    user_tables,
    validate_index,
    validate_integrity,
    validate_table,
    validate_table_sql,
    validate_user_version,
)


RECALL_SCHEMA_VERSION = 2

_RECALL_SCHEMA_SQL = """
CREATE TABLE chat_recall_documents (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    recall_seq INTEGER NOT NULL UNIQUE CHECK (recall_seq > 0),
    conversation_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    block_id TEXT NOT NULL,
    block_kind TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    source_generation INTEGER NOT NULL CHECK (source_generation > 0),
    created_at_us INTEGER NOT NULL,
    updated_at_us INTEGER NOT NULL,
    UNIQUE(conversation_id, turn_id, message_id, block_id)
);
CREATE INDEX ix_chat_recall_conversation_sequence
ON chat_recall_documents(conversation_id, recall_seq);
CREATE INDEX ix_chat_recall_source_generation
ON chat_recall_documents(conversation_id, source_generation);

CREATE TABLE chat_recall_projection_state (
    conversation_id TEXT PRIMARY KEY NOT NULL,
    source_generation INTEGER NOT NULL CHECK (source_generation >= 0),
    projected_recall_seq INTEGER NOT NULL DEFAULT 0
        CHECK (projected_recall_seq >= 0),
    state TEXT NOT NULL CHECK (
        state IN ('ready', 'dirty', 'rebuilding', 'failed')
    ),
    error_digest TEXT,
    updated_at_us INTEGER NOT NULL
);
CREATE INDEX ix_chat_recall_projection_state
ON chat_recall_projection_state(state, updated_at_us);

CREATE VIRTUAL TABLE chat_recall_fts USING fts5(
    content,
    content = 'chat_recall_documents',
    content_rowid = 'row_id',
    tokenize = 'unicode61'
);

CREATE TABLE conversation_history (
    seq INTEGER PRIMARY KEY NOT NULL CHECK (seq > 0),
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
    dedup_key TEXT,
    conversation_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    block_id TEXT NOT NULL,
    provisional INTEGER NOT NULL DEFAULT 0 CHECK (provisional IN (0, 1)),
    UNIQUE(session_id, dedup_key)
);
CREATE INDEX ix_conversation_history_session
ON conversation_history(session_id, seq);
CREATE INDEX ix_conversation_history_agent
ON conversation_history(agent_id, seq);
CREATE INDEX ix_conversation_history_conversation
ON conversation_history(conversation_id, seq);

CREATE VIRTUAL TABLE conversation_history_fts USING fts5(
    content,
    content = 'conversation_history',
    content_rowid = 'seq',
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
    "chat_recall_documents": {
        "row_id": _column("INTEGER", not_null=False, primary_key=1),
        "recall_seq": _column("INTEGER"),
        "conversation_id": _column("TEXT"),
        "turn_id": _column("TEXT"),
        "message_id": _column("TEXT"),
        "block_id": _column("TEXT"),
        "block_kind": _column("TEXT"),
        "content": _column("TEXT"),
        "metadata_json": _column("TEXT"),
        "source_generation": _column("INTEGER"),
        "created_at_us": _column("INTEGER"),
        "updated_at_us": _column("INTEGER"),
    },
    "chat_recall_projection_state": {
        "conversation_id": _column("TEXT", primary_key=1),
        "source_generation": _column("INTEGER"),
        "projected_recall_seq": _column("INTEGER"),
        "state": _column("TEXT"),
        "error_digest": _column("TEXT", not_null=False),
        "updated_at_us": _column("INTEGER"),
    },
    "conversation_history": {
        "seq": _column("INTEGER", primary_key=1),
        "session_id": _column("TEXT"),
        "agent_id": _column("TEXT", not_null=False),
        "kind": _column("TEXT"),
        "role": _column("TEXT", not_null=False),
        "name": _column("TEXT", not_null=False),
        "content": _column("TEXT", not_null=False),
        "tool_call_id": _column("TEXT", not_null=False),
        "tool_input": _column("TEXT", not_null=False),
        "tool_state": _column("TEXT", not_null=False),
        "headline": _column("TEXT", not_null=False),
        "blocks": _column("TEXT", not_null=False),
        "metadata": _column("TEXT", not_null=False),
        "created_at": _column("TEXT", not_null=False),
        "dedup_key": _column("TEXT", not_null=False),
        "conversation_id": _column("TEXT"),
        "turn_id": _column("TEXT"),
        "message_id": _column("TEXT"),
        "block_id": _column("TEXT"),
        "provisional": _column("INTEGER"),
    },
}

_INDEX_CONTRACTS = {
    "ix_chat_recall_conversation_sequence": IndexContract(
        "chat_recall_documents",
        ("conversation_id", "recall_seq"),
    ),
    "ix_chat_recall_source_generation": IndexContract(
        "chat_recall_documents",
        ("conversation_id", "source_generation"),
    ),
    "ix_chat_recall_projection_state": IndexContract(
        "chat_recall_projection_state",
        ("state", "updated_at_us"),
    ),
    "ix_conversation_history_session": IndexContract(
        "conversation_history",
        ("session_id", "seq"),
    ),
    "ix_conversation_history_agent": IndexContract(
        "conversation_history",
        ("agent_id", "seq"),
    ),
    "ix_conversation_history_conversation": IndexContract(
        "conversation_history",
        ("conversation_id", "seq"),
    ),
}

_SQL_FRAGMENT_CONTRACTS = {
    "chat_recall_documents": (
        "recall_seq INTEGER NOT NULL UNIQUE",
        "CHECK (recall_seq > 0)",
        "CHECK (source_generation > 0)",
        "UNIQUE(conversation_id, turn_id, message_id, block_id)",
    ),
    "chat_recall_projection_state": (
        "CHECK (source_generation >= 0)",
        "CHECK (projected_recall_seq >= 0)",
        "CHECK (state IN ('ready', 'dirty', 'rebuilding', 'failed'))",
    ),
    "conversation_history": (
        "CHECK (seq > 0)",
        "CHECK (provisional IN (0, 1))",
        "UNIQUE(session_id, dedup_key)",
    ),
}


def _validate_recall_connection(connection: sqlite3.Connection) -> None:
    validate_integrity(connection)
    validate_user_version(
        connection,
        expected=RECALL_SCHEMA_VERSION,
        database_name="Chat recall",
    )
    for table, columns in _TABLE_CONTRACTS.items():
        validate_table(connection, table, columns)
    for table, fragments in _SQL_FRAGMENT_CONTRACTS.items():
        validate_table_sql(connection, table, fragments)
    for name, index in _INDEX_CONTRACTS.items():
        validate_index(connection, index, name=name)
    for table, content_table, row_id in (
        ("chat_recall_fts", "chat_recall_documents", "row_id"),
        ("conversation_history_fts", "conversation_history", "seq"),
    ):
        fts_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if fts_sql is None:
            raise ChatStorageSchemaError(f"Missing required {table} table")
        normalized_fts_sql = " ".join(str(fts_sql[0]).lower().split())
        compact_fts_sql = normalized_fts_sql.replace(" ", "")
        required = (
            "using fts5",
            f"content='{content_table}'",
            f"content_rowid='{row_id}'",
        )
        if any(
            fragment.replace(" ", "") not in compact_fts_sql
            for fragment in required
        ):
            raise ChatStorageSchemaError(
                f"{table} has an invalid FTS5 contract",
            )


def initialize_recall_database(path: Path) -> None:
    """Create or validate the rebuildable workspace recall projection."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect_database(path) as connection:
        version_row = connection.execute("PRAGMA user_version").fetchone()
        version = int(version_row[0] if version_row else 0)
        tables = user_tables(connection)
        if version == 0 and not tables:
            connection.executescript(_RECALL_SCHEMA_SQL)
            connection.execute(
                f"PRAGMA user_version = {RECALL_SCHEMA_VERSION}",
            )
            connection.commit()
        elif version == 0:
            raise ChatStorageSchemaError(
                "Refusing to initialize a non-empty unversioned "
                "Chat recall DB",
            )
        _validate_recall_connection(connection)


def validate_recall_database(path: Path) -> int:
    """Validate an existing Chat recall projection read-only."""
    if not path.is_file():
        raise FileNotFoundError(path)
    with connect_database(path, readonly=True) as connection:
        _validate_recall_connection(connection)
    return RECALL_SCHEMA_VERSION
