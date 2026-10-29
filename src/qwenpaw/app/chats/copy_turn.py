# -*- coding: utf-8 -*-
"""Copy a completed turn boundary without replaying model or tool calls."""

from __future__ import annotations

import sqlite3

from .transcript_blocks import copy_message_blocks


def copy_turn(
    connection: sqlite3.Connection,
    *,
    source_session_id: str,
    target_session_id: str,
    target_user_id: str,
    target_channel: str,
    turn_id: str,
) -> None:
    """Copy from attached source within the caller's single transaction."""
    busy = connection.execute(
        "SELECT 1 FROM parent.transcript_turns "
        "WHERE session_id = ? AND status = 'running' LIMIT 1",
        (source_session_id,),
    ).fetchone()
    if busy:
        raise ValueError("chat_busy")
    turn = connection.execute(
        "SELECT turn_seq, status FROM parent.transcript_turns "
        "WHERE session_id = ? AND turn_id = ?",
        (source_session_id, turn_id),
    ).fetchone()
    if turn is None or turn["status"] != "completed":
        raise ValueError("copy_turn_not_completed")
    revision = connection.execute(
        "SELECT r.* FROM parent.turn_context_revision t "
        "JOIN parent.context_revisions r ON r.session_id = t.session_id "
        "AND r.revision_id = t.revision_id "
        "WHERE t.session_id = ? AND t.turn_id = ?",
        (source_session_id, turn_id),
    ).fetchone()
    if revision is None:
        raise ValueError("copy_context_missing")
    boundary = int(turn["turn_seq"])
    connection.execute(
        "INSERT INTO transcript_sessions VALUES (?, ?, ?, ?)",
        (target_session_id, target_user_id, target_channel, boundary + 1),
    )
    connection.execute(
        "INSERT INTO transcript_turns "
        "SELECT ?, turn_seq, turn_id, status, error_json, "
        "replaces_turn_id, created_at, finished_at "
        "FROM parent.transcript_turns "
        "WHERE session_id = ? AND turn_seq <= ?",
        (target_session_id, source_session_id, boundary),
    )
    connection.execute(
        "INSERT INTO transcript_messages "
        "SELECT ?, m.turn_id, m.message_id, m.ordinal, m.role, "
        "m.envelope_json, m.client_message_id, CASE WHEN EXISTS ("
        "SELECT 1 FROM parent.transcript_turns replacement "
        "WHERE replacement.session_id = m.session_id "
        "AND replacement.replaces_turn_id = m.turn_id "
        "AND replacement.status = 'completed' "
        "AND replacement.turn_seq <= ?) THEN m.superseded_at ELSE NULL END, "
        "m.created_at, m.finished_at "
        "FROM parent.transcript_messages m JOIN parent.transcript_turns t "
        "ON t.session_id = m.session_id AND t.turn_id = m.turn_id "
        "WHERE m.session_id = ? AND t.turn_seq <= ?",
        (target_session_id, boundary, source_session_id, boundary),
    )
    copy_message_blocks(
        connection,
        source_session_id=source_session_id,
        target_session_id=target_session_id,
    )
    connection.execute(
        "INSERT OR IGNORE INTO context_blobs "
        "SELECT b.* FROM parent.context_blobs b "
        "JOIN parent.context_revisions r ON r.content_hash = b.content_hash "
        "JOIN parent.turn_context_revision c "
        "ON c.session_id = r.session_id AND c.revision_id = r.revision_id "
        "JOIN transcript_turns t ON t.turn_id = c.turn_id "
        "WHERE c.session_id = ? AND t.session_id = ?",
        (source_session_id, target_session_id),
    )
    # Each copied revision is a complete snapshot, not a delta chain.
    connection.execute(
        "INSERT INTO context_revisions "
        "SELECT ?, r.revision_id, NULL, r.anchor_turn_id, r.kind, "
        "r.content_hash, r.context_generation, r.current_usage_json, "
        "r.created_at FROM parent.context_revisions r "
        "JOIN parent.turn_context_revision c "
        "ON c.session_id = r.session_id AND c.revision_id = r.revision_id "
        "JOIN transcript_turns t ON t.turn_id = c.turn_id "
        "WHERE c.session_id = ? AND t.session_id = ?",
        (target_session_id, source_session_id, target_session_id),
    )
    connection.execute(
        "INSERT INTO turn_context_revision "
        "SELECT session_id, anchor_turn_id, revision_id "
        "FROM context_revisions WHERE session_id = ?",
        (target_session_id,),
    )
    connection.execute(
        "INSERT INTO current_context_revision VALUES (?, ?)",
        (target_session_id, revision["revision_id"]),
    )
    connection.execute(
        "INSERT INTO session_runtime "
        "SELECT ?, ?, payload, ?, ? FROM context_blobs "
        "WHERE content_hash = ?",
        (
            target_session_id,
            revision["context_generation"],
            revision["current_usage_json"],
            revision["created_at"],
            revision["content_hash"],
        ),
    )
