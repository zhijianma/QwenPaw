# -*- coding: utf-8 -*-
"""Canonical block writes and message envelope serialization."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from uuid import NAMESPACE_URL, uuid5

from ...schemas import Message


def message_envelope(message: Message) -> str:
    """Serialize message metadata without duplicating block bodies."""
    return message.model_dump_json(exclude={"content"})


def block_identity(session_id: str, message_id: str, ordinal: int) -> str:
    """Use session-scoped identity, independent of database row numbers."""
    key = json.dumps([session_id, message_id, ordinal], ensure_ascii=False)
    return uuid5(NAMESPACE_URL, f"qwenpaw:content:{key}").hex


def write_message_blocks(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    message_id: str,
    content: list[dict],
) -> None:
    """Replace materialized content within the message write transaction."""
    for ordinal, block in enumerate(content):
        payload = json.dumps(
            block,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        encoded = payload.encode("utf-8")
        connection.execute(
            "INSERT INTO transcript_blocks VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, 1) "
            "ON CONFLICT(session_id, message_id, ordinal) DO UPDATE SET "
            "kind = excluded.kind, content_hash = excluded.content_hash, "
            "payload_json = excluded.payload_json, "
            "byte_length = excluded.byte_length, version = "
            "transcript_blocks.version + 1 "
            "WHERE transcript_blocks.content_hash != excluded.content_hash",
            (
                session_id,
                message_id,
                block_identity(session_id, message_id, ordinal),
                ordinal,
                str(block.get("type") or "unknown"),
                hashlib.sha256(encoded).hexdigest(),
                payload,
                len(encoded),
            ),
        )
    connection.execute(
        "DELETE FROM transcript_blocks WHERE session_id = ? "
        "AND message_id = ? AND ordinal >= ?",
        (session_id, message_id, len(content)),
    )


def copy_message_blocks(
    connection: sqlite3.Connection,
    *,
    source_session_id: str,
    target_session_id: str,
) -> None:
    """Copy only blocks whose envelopes were selected into the new session."""
    rows = connection.execute(
        "SELECT b.* FROM parent.transcript_blocks b "
        "JOIN transcript_messages m ON m.message_id = b.message_id "
        "WHERE b.session_id = ? AND m.session_id = ?",
        (source_session_id, target_session_id),
    )
    for row in rows:
        connection.execute(
            "INSERT INTO transcript_blocks VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                target_session_id,
                row["message_id"],
                block_identity(
                    target_session_id,
                    row["message_id"],
                    row["ordinal"],
                ),
                row["ordinal"],
                row["kind"],
                row["content_hash"],
                row["payload_json"],
                row["byte_length"],
                row["version"],
            ),
        )
