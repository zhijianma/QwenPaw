# -*- coding: utf-8 -*-
"""Immutable runtime revisions, independent from display history."""

from __future__ import annotations

import hashlib
import sqlite3
from uuid import uuid4


def record_revision(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    turn_id: str | None,
    payload: str,
    generation: int,
    usage_json: str | None,
    created_at: str,
) -> str:
    """Record a revision inside the caller's runtime write transaction."""
    if turn_id is not None:
        turn = connection.execute(
            "SELECT status FROM transcript_turns "
            "WHERE session_id = ? AND turn_id = ?",
            (session_id, turn_id),
        ).fetchone()
        if turn is None or turn[0] != "running":
            raise ValueError("context revision requires a running turn")
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    connection.execute(
        "INSERT OR IGNORE INTO context_blobs VALUES "
        "(?, 'session-json-v1', ?)",
        (digest, payload),
    )
    parent = connection.execute(
        "SELECT revision_id FROM current_context_revision "
        "WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    revision_id = uuid4().hex
    connection.execute(
        "INSERT INTO context_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            revision_id,
            parent[0] if parent else None,
            turn_id,
            "turn_commit" if turn_id is not None else "clear",
            digest,
            generation,
            usage_json,
            created_at,
        ),
    )
    connection.execute(
        "INSERT INTO current_context_revision VALUES (?, ?) "
        "ON CONFLICT(session_id) DO UPDATE SET "
        "revision_id = excluded.revision_id",
        (session_id, revision_id),
    )
    return revision_id


def commit_turn_revision(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    turn_id: str,
) -> None:
    """Bind only a context saved explicitly for this turn, never an old one."""
    connection.execute(
        "INSERT OR IGNORE INTO turn_context_revision "
        "SELECT r.session_id, r.anchor_turn_id, r.revision_id "
        "FROM context_revisions r JOIN current_context_revision c "
        "ON c.session_id = r.session_id AND c.revision_id = r.revision_id "
        "WHERE r.session_id = ? AND r.anchor_turn_id = ?",
        (session_id, turn_id),
    )
