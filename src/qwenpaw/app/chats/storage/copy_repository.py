# -*- coding: utf-8 -*-
"""Copy a completed turn lineage into an independent Chat database."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from uuid import UUID, uuid5

from .common import ChatStorageSchemaError, connect_database
from .conversation import initialize_conversation_database
from .conversation_repository import initialize_timeline


class ChatCopyError(RuntimeError):
    """The requested independent Chat copy cannot be created safely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ChatCopyResult:
    """Identity and entity counts for one copied Chat."""

    conversation_id: str
    turns: int
    messages: int
    blocks: int


def _target_id(conversation_id: str, kind: str, source_id: str) -> str:
    return str(uuid5(UUID(conversation_id), f"copy:{kind}:{source_id}"))


def _lineage_rows(connection, source_turn_id: str):
    source = connection.execute(
        "SELECT head_turn_id FROM chat_meta WHERE singleton_id = 1",
    ).fetchone()
    if source is None or source["head_turn_id"] is None:
        raise ChatCopyError("copy_source_empty", "Source Chat is empty")
    reachable = connection.execute(
        "WITH RECURSIVE lineage(turn_id, parent_turn_id) AS ("
        "SELECT turn_id, parent_turn_id FROM chat_turns WHERE turn_id = ? "
        "UNION ALL SELECT t.turn_id, t.parent_turn_id FROM chat_turns AS t "
        "JOIN lineage AS l ON t.turn_id = l.parent_turn_id) "
        "SELECT 1 FROM lineage WHERE turn_id = ? LIMIT 1",
        (source["head_turn_id"], source_turn_id),
    ).fetchone()
    if reachable is None:
        raise ChatCopyError(
            "copy_turn_unreachable",
            "Copy turn is not reachable from the source Chat",
        )
    rows = connection.execute(
        "WITH RECURSIVE lineage AS ("
        "SELECT * FROM chat_turns WHERE turn_id = ? UNION ALL "
        "SELECT t.* FROM chat_turns AS t JOIN lineage AS l "
        "ON t.turn_id = l.parent_turn_id) "
        "SELECT * FROM lineage ORDER BY lineage_depth",
        (source_turn_id,),
    ).fetchall()
    if not rows or str(rows[-1]["state"]) != "completed":
        raise ChatCopyError(
            "copy_turn_not_completed",
            "Only completed turns can be copied",
        )
    if rows[-1]["context_revision_id"] is None:
        raise ChatCopyError(
            "copy_context_missing",
            "Copy turn does not have a committed context",
        )
    return rows


def inspect_copy(
    source_path: Path,
    *,
    source_turn_id: str,
) -> tuple[int, str, str, str]:
    """Validate a copy point and return block/storage ownership metadata."""
    with connect_database(source_path, readonly=True) as connection:
        busy = connection.execute(
            "SELECT EXISTS(SELECT 1 FROM chat_runs WHERE state IN "
            "('accepted', 'running', 'stopping')) OR EXISTS(SELECT 1 "
            "FROM chat_queue_items WHERE state IN ('pending', 'starting'))",
        ).fetchone()[0]
        if busy:
            raise ChatCopyError(
                "chat_busy",
                "A Chat with active or queued work cannot be copied",
            )
        turns = _lineage_rows(connection, source_turn_id)
        turn_ids = [str(row["turn_id"]) for row in turns]
        placeholders = ",".join("?" for _ in turn_ids)
        blocks = int(
            connection.execute(
                "SELECT COUNT(*) FROM chat_blocks AS b JOIN "
                "chat_messages AS m ON m.message_id = b.message_id "
                f"WHERE m.turn_id IN ({placeholders})",
                turn_ids,
            ).fetchone()[0],
        )
        meta = connection.execute(
            "SELECT agent_id, owner_scope, backend FROM chat_meta "
            "WHERE singleton_id = 1",
        ).fetchone()
        if meta is None:
            raise ChatStorageSchemaError("Source Chat metadata is missing")
        return (
            blocks,
            str(meta["agent_id"]),
            str(meta["owner_scope"]),
            str(meta["backend"]),
        )


# The copy is intentionally one transaction-shaped routine: splitting entity
# writes across public helpers would obscure ordering and rollback boundaries.
# pylint: disable=too-many-branches,too-many-statements
def copy_conversation_at_turn(
    source_path: Path,
    target_path: Path,
    *,
    conversation_id: str,
    source_conversation_id: str,
    source_turn_id: str,
    session_id: str,
    recall_sequences: Iterable[int],
    now_us: int,
) -> ChatCopyResult:
    """Materialize an independent transcript/context copy through one turn."""
    with connect_database(source_path, readonly=True) as source:
        turns = _lineage_rows(source, source_turn_id)
        meta = source.execute(
            "SELECT agent_id, owner_scope, backend FROM chat_meta "
            "WHERE singleton_id = 1",
        ).fetchone()
        if meta is None:
            raise ChatStorageSchemaError("Source Chat metadata is missing")
        initialize_conversation_database(
            target_path,
            conversation_id=conversation_id,
            agent_id=str(meta["agent_id"]),
            owner_scope=str(meta["owner_scope"]),
            backend=str(meta["backend"]),
            created_at_us=now_us,
        )
        initialize_timeline(
            target_path,
            conversation_id=conversation_id,
            now_us=now_us,
        )
        sequence_iter = iter(recall_sequences)
        turn_map: dict[str, str] = {}
        block_map: dict[str, str] = {}
        write_seq = 1
        message_count = 0
        block_count = 0
        with connect_database(target_path) as target:
            initial = target.execute(
                "SELECT current_context_revision_id FROM chat_meta "
                "WHERE singleton_id = 1",
            ).fetchone()
            if initial is None:
                raise ChatStorageSchemaError("Copy target context is missing")
            context_parent = str(initial["current_context_revision_id"])
            for turn in turns:
                source_row_turn_id = str(turn["turn_id"])
                turn_id = _target_id(
                    conversation_id,
                    "turn",
                    source_row_turn_id,
                )
                turn_map[source_row_turn_id] = turn_id
                write_seq += 1
                target.execute(
                    "INSERT INTO chat_turns(turn_id, parent_turn_id, "
                    "client_request_id, state, lineage_depth, usage_json, "
                    "error_digest, write_seq, "
                    "created_at_us, updated_at_us, terminal_at_us) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        turn_id,
                        turn_map.get(str(turn["parent_turn_id"])),
                        f"copy:{source_conversation_id}:"
                        f"{turn['client_request_id']}",
                        str(turn["state"]),
                        int(turn["lineage_depth"]),
                        turn["usage_json"],
                        turn["error_digest"],
                        write_seq,
                        int(turn["created_at_us"]),
                        int(turn["updated_at_us"]),
                        turn["terminal_at_us"],
                    ),
                )
                messages = source.execute(
                    "SELECT * FROM chat_messages WHERE turn_id = ? "
                    "ORDER BY ordinal",
                    (source_row_turn_id,),
                ).fetchall()
                for message in messages:
                    source_message_id = str(message["message_id"])
                    message_id = _target_id(
                        conversation_id,
                        "message",
                        source_message_id,
                    )
                    target.execute(
                        "INSERT INTO chat_messages VALUES (?, ?, ?, ?, ?, "
                        "?, ?, ?)",
                        (
                            message_id,
                            turn_id,
                            str(message["role"]),
                            int(message["ordinal"]),
                            str(message["state"]),
                            str(message["metadata_json"]),
                            int(message["created_at_us"]),
                            int(message["updated_at_us"]),
                        ),
                    )
                    message_count += 1
                    blocks = source.execute(
                        "SELECT * FROM chat_blocks WHERE message_id = ? "
                        "ORDER BY ordinal",
                        (source_message_id,),
                    ).fetchall()
                    for block in blocks:
                        source_block_id = str(block["block_id"])
                        block_id = _target_id(
                            conversation_id,
                            "block",
                            source_block_id,
                        )
                        block_map[source_block_id] = block_id
                        try:
                            recall_seq = next(sequence_iter)
                        except StopIteration as exc:
                            raise ChatStorageSchemaError(
                                "Copy recall sequence lease is too small",
                            ) from exc
                        target.execute(
                            "INSERT INTO chat_blocks VALUES (?, ?, ?, ?, ?, "
                            "?, ?, ?, ?, ?, ?, ?)",
                            (
                                block_id,
                                message_id,
                                recall_seq,
                                str(block["kind"]),
                                int(block["ordinal"]),
                                str(block["state"]),
                                int(block["total_bytes"]),
                                block["content_sha256"],
                                int(block["entity_version"]),
                                str(block["metadata_json"]),
                                int(block["created_at_us"]),
                                int(block["updated_at_us"]),
                            ),
                        )
                        segments = source.execute(
                            "SELECT * FROM chat_block_segments WHERE "
                            "block_id = ? ORDER BY segment_ordinal",
                            (source_block_id,),
                        ).fetchall()
                        for segment in segments:
                            target.execute(
                                "INSERT INTO chat_block_segments VALUES "
                                "(?, ?, ?, ?, ?, ?, ?)",
                                (
                                    block_id,
                                    int(segment["segment_ordinal"]),
                                    int(segment["start_byte"]),
                                    int(segment["end_byte"]),
                                    bytes(segment["payload"]),
                                    str(segment["segment_sha256"]),
                                    int(segment["created_at_us"]),
                                ),
                            )
                        fts = source.execute(
                            "SELECT content FROM chat_blocks_fts WHERE "
                            "block_id = ?",
                            (source_block_id,),
                        ).fetchone()
                        target.execute(
                            "INSERT INTO chat_blocks_fts(block_id, content) "
                            "VALUES (?, ?)",
                            (block_id, str(fts["content"]) if fts else ""),
                        )
                        block_count += 1
                source_context_id = str(turn["context_revision_id"])
                context = source.execute(
                    "SELECT * FROM chat_context_revisions WHERE "
                    "context_revision_id = ?",
                    (source_context_id,),
                ).fetchone()
                if context is None:
                    raise ChatCopyError(
                        "copy_context_missing",
                        "A copied turn does not have a committed context",
                    )
                blob_hash = context["runtime_blob_hash"]
                if blob_hash is not None:
                    blob = source.execute(
                        "SELECT * FROM chat_runtime_blobs WHERE blob_hash = ?",
                        (blob_hash,),
                    ).fetchone()
                    if blob is None:
                        raise ChatStorageSchemaError(
                            "Copy context runtime blob is missing",
                        )
                    target.execute(
                        "INSERT OR IGNORE INTO chat_runtime_blobs VALUES "
                        "(?, ?, ?, ?, ?)",
                        tuple(blob),
                    )
                context_id = _target_id(
                    conversation_id,
                    "context",
                    source_context_id,
                )
                write_seq += 1
                target.execute(
                    "INSERT INTO chat_context_revisions VALUES "
                    "(?, ?, ?, 'turn_commit', ?, ?, ?, ?)",
                    (
                        context_id,
                        context_parent,
                        turn_id,
                        blob_hash,
                        str(context["metadata_json"]),
                        write_seq,
                        int(context["created_at_us"]),
                    ),
                )
                target.execute(
                    "UPDATE chat_turns SET context_revision_id = ? "
                    "WHERE turn_id = ?",
                    (context_id, turn_id),
                )
                context_parent = context_id

            links = source.execute("SELECT * FROM chat_tool_links").fetchall()
            for link in links:
                call_id = block_map.get(str(link["call_block_id"]))
                if call_id is None:
                    continue
                result_id = (
                    block_map.get(str(link["result_block_id"]))
                    if link["result_block_id"] is not None
                    else None
                )
                state = str(link["state"]) if result_id else "unknown"
                target.execute(
                    "INSERT INTO chat_tool_links VALUES (?, ?, ?, ?, ?, ?, "
                    "?, ?, ?)",
                    (
                        _target_id(
                            conversation_id,
                            "tool",
                            str(link["tool_link_id"]),
                        ),
                        str(link["provider_call_id"]),
                        call_id,
                        result_id,
                        state,
                        link["tool_name"],
                        str(link["metadata_json"]),
                        int(link["created_at_us"]),
                        int(link["updated_at_us"]),
                    ),
                )

            head_turn_id = turn_map[source_turn_id]
            target.execute(
                "UPDATE chat_meta SET head_turn_id = ?, "
                "current_context_revision_id = ?, updated_at_us = ? "
                "WHERE singleton_id = 1",
                (head_turn_id, context_parent, now_us),
            )
            target.execute(
                "INSERT INTO chat_aliases(alias_namespace, external_key, "
                "owner_scope, metadata_json, created_at_us, updated_at_us) "
                "VALUES ('legacy_session', ?, ?, '{}', ?, ?)",
                (
                    session_id,
                    str(meta["owner_scope"]),
                    now_us,
                    now_us,
                ),
            )
            display_metadata = json.dumps(
                {
                    "copy_source_conversation_id": source_conversation_id,
                    "copy_source_turn_id": source_turn_id,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            target.execute(
                "UPDATE chat_meta SET source_generation = 1, write_seq = ?, "
                "display_metadata_json = ?, updated_at_us = ? WHERE "
                "singleton_id = 1",
                (write_seq, display_metadata, now_us),
            )
            payload = json.dumps(
                {
                    "conversation_id": conversation_id,
                    "source_generation": 1,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            target.executemany(
                "INSERT INTO chat_projection_outbox(projection, operation, "
                "source_generation, payload_json, created_at_us, "
                "updated_at_us) VALUES (?, 'conversation_copied', 1, ?, ?, ?)",
                (
                    ("catalog", payload, now_us, now_us),
                    ("recall", payload, now_us, now_us),
                ),
            )
            target.commit()
            violations = target.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                digest = hashlib.sha256(repr(violations).encode()).hexdigest()
                raise ChatStorageSchemaError(
                    f"Copied Chat foreign key validation failed: {digest}",
                )
    return ChatCopyResult(
        conversation_id=conversation_id,
        turns=len(turns),
        messages=message_count,
        blocks=block_count,
    )
