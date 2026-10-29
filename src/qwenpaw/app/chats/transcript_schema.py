# -*- coding: utf-8 -*-
"""Canonical conversation schema, independent of wire message encoding.

Turns own execution boundaries, messages own envelopes, and blocks own bodies.
The payload view is a read projection; no full message body is stored twice.
"""

import sqlite3

SCHEMA = """

CREATE TABLE IF NOT EXISTS transcript_sessions (
    session_id       TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL,
    channel          TEXT NOT NULL,
    next_turn_seq    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS transcript_turns (
    session_id       TEXT NOT NULL,
    turn_seq         INTEGER NOT NULL,
    turn_id          TEXT NOT NULL,
    status           TEXT NOT NULL
                     CHECK(status IN (
                         'running', 'completed',
                         'failed', 'cancelled'
    )),
    error_json       TEXT,
    replaces_turn_id TEXT,
    created_at       TEXT NOT NULL,
    finished_at      TEXT,
    PRIMARY KEY(session_id, turn_seq),
    UNIQUE(session_id, turn_id),
    FOREIGN KEY(session_id)
        REFERENCES transcript_sessions(session_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS transcript_messages (
    session_id          TEXT NOT NULL,
    turn_id             TEXT NOT NULL,
    message_id          TEXT NOT NULL,
    ordinal             INTEGER NOT NULL CHECK(ordinal >= 0),
    role                TEXT NOT NULL,
    envelope_json       TEXT NOT NULL CHECK(
        json_valid(envelope_json) AND
        json_type(envelope_json, '$.content') IS NULL
    ),
    client_message_id   TEXT,
    superseded_at       TEXT,
    created_at          TEXT NOT NULL,
    finished_at         TEXT,
    PRIMARY KEY(session_id, message_id),
    UNIQUE(session_id, turn_id, ordinal),
    FOREIGN KEY(session_id, turn_id)
        REFERENCES transcript_turns(session_id, turn_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS transcript_messages_client
    ON transcript_messages(
        session_id, client_message_id, created_at DESC
    );

CREATE TABLE IF NOT EXISTS session_runtime (
    session_id          TEXT PRIMARY KEY,
    context_generation  INTEGER NOT NULL DEFAULT 0,
    state_json          TEXT NOT NULL,
    current_usage_json  TEXT,
    updated_at          TEXT NOT NULL,
    FOREIGN KEY(session_id)
        REFERENCES transcript_sessions(session_id)
        ON DELETE CASCADE
);


CREATE TABLE IF NOT EXISTS transcript_blocks (
    session_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    block_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    kind TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    byte_length INTEGER NOT NULL CHECK(byte_length >= 0),
    version INTEGER NOT NULL CHECK(version > 0),
    PRIMARY KEY(session_id, block_id),
    UNIQUE(session_id, message_id, ordinal),
    FOREIGN KEY(session_id, message_id)
        REFERENCES transcript_messages(session_id, message_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS context_blobs (
    content_hash TEXT PRIMARY KEY NOT NULL,
    codec TEXT NOT NULL CHECK(codec = 'session-json-v1'),
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS context_revisions (
    session_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    parent_revision_id TEXT,
    anchor_turn_id TEXT,
    kind TEXT NOT NULL CHECK(kind IN ('turn_commit', 'clear')),
    content_hash TEXT NOT NULL REFERENCES context_blobs(content_hash),
    context_generation INTEGER NOT NULL,
    current_usage_json TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(session_id, revision_id),
    FOREIGN KEY(session_id) REFERENCES transcript_sessions(session_id)
        ON DELETE CASCADE,
    FOREIGN KEY(session_id, parent_revision_id)
        REFERENCES context_revisions(session_id, revision_id)
);
CREATE INDEX IF NOT EXISTS context_revisions_turn
    ON context_revisions(session_id, anchor_turn_id);
CREATE TABLE IF NOT EXISTS current_context_revision (
    session_id TEXT PRIMARY KEY NOT NULL,
    revision_id TEXT NOT NULL,
    FOREIGN KEY(session_id, revision_id)
        REFERENCES context_revisions(session_id, revision_id)
        ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS turn_context_revision (
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    PRIMARY KEY(session_id, turn_id),
    FOREIGN KEY(session_id, turn_id)
        REFERENCES transcript_turns(session_id, turn_id) ON DELETE CASCADE,
    FOREIGN KEY(session_id, revision_id)
        REFERENCES context_revisions(session_id, revision_id)
);

CREATE VIEW IF NOT EXISTS transcript_message_payloads AS
SELECT m.*, json_set(m.envelope_json, '$.content', json((
    SELECT COALESCE(json_group_array(json(payload_json)), '[]')
    FROM (
        SELECT b.payload_json FROM transcript_blocks b
        WHERE b.session_id = m.session_id AND b.message_id = m.message_id
        ORDER BY b.ordinal
    )
))) AS payload_json
FROM transcript_messages m;
PRAGMA user_version=1;
"""


def validate_database(connection: sqlite3.Connection) -> None:
    """Reject unsupported development layouts without modifying their data."""
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version != 1:
        raise ValueError("unsupported conversation database schema")
    # Resolve all canonical columns before accepting a checkpoint or store.
    connection.execute(
        "SELECT m.envelope_json, b.payload_json, b.version, r.content_hash "
        "FROM transcript_messages m, transcript_blocks b, "
        "context_revisions r LIMIT 0",
    )


def initialize_database(connection: sqlite3.Connection) -> None:
    """Create the final schema only for a fresh database; never migrate."""
    existing = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1",
    ).fetchone()
    if existing is None:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.executescript(SCHEMA)
    validate_database(connection)
