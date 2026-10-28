# -*- coding: utf-8 -*-
"""Transcript materialization and verification for one Chat database."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Iterator, Mapping
from uuid import UUID, uuid5

from ....schemas import Message, MessageType, Role, RunStatus
from ..integrity import TranscriptManifest, build_transcript_manifest
from .common import (
    ChatStorageSchemaError,
    connect_database,
    immediate_transaction,
)
from .conversation import initialize_conversation_database

if TYPE_CHECKING:
    from ..migration.normalize import NormalizedTranscript
    from ..migration.snapshot import LegacySnapshotManifest


SEGMENT_TARGET_BYTES = 64 * 1024


@dataclass(frozen=True)
class MaterializationResult:
    """Identity and correctness evidence for a migrated transcript."""

    turns: int
    messages: int
    blocks: int
    source_manifest: TranscriptManifest
    stored_manifest: TranscriptManifest


def initialize_timeline(
    path: Path,
    *,
    conversation_id: str,
    now_us: int,
) -> None:
    """Create the initial context for one single-timeline Chat."""
    context_id = _stable_id(conversation_id, "context:initial")
    with connect_database(path) as connection:
        existing = connection.execute(
            "SELECT current_context_revision_id FROM chat_meta "
            "WHERE singleton_id = 1",
        ).fetchone()
        if existing is None:
            raise ChatStorageSchemaError("Chat metadata is missing")
        if existing["current_context_revision_id"] is not None:
            return
        count = int(
            connection.execute(
                "SELECT COUNT(*) FROM chat_context_revisions",
            ).fetchone()[0],
        )
        if count:
            raise ChatStorageSchemaError(
                "Cannot initialize a non-empty Chat timeline",
            )
        with immediate_transaction(connection):
            connection.execute(
                "INSERT INTO chat_context_revisions("
                "context_revision_id, kind, write_seq, created_at_us) "
                "VALUES (?, 'initial', 1, ?)",
                (context_id, now_us),
            )
            connection.execute(
                "UPDATE chat_meta SET current_context_revision_id = ?, "
                "source_generation = 1, write_seq = 1, updated_at_us = ? "
                "WHERE singleton_id = 1",
                (context_id, now_us),
            )
            payload = _canonical_json(
                {
                    "conversation_id": conversation_id,
                    "source_generation": 1,
                },
            )
            connection.executemany(
                "INSERT INTO chat_projection_outbox("
                "projection, operation, source_generation, payload_json, "
                "created_at_us, updated_at_us) "
                "VALUES (?, 'conversation_created', 1, ?, ?, ?)",
                (
                    ("catalog", payload, now_us, now_us),
                    ("recall", payload, now_us, now_us),
                ),
            )


def _stable_id(conversation_id: str, label: str) -> str:
    return str(uuid5(UUID(conversation_id), label))


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _utf8_segments(payload: bytes) -> Iterable[tuple[int, int, bytes]]:
    start = 0
    while start < len(payload):
        end = min(start + SEGMENT_TARGET_BYTES, len(payload))
        if end < len(payload):
            while end > start and payload[end] & 0xC0 == 0x80:
                end -= 1
        if end == start:
            end = min(start + SEGMENT_TARGET_BYTES, len(payload))
            while end < len(payload) and payload[end] & 0xC0 == 0x80:
                end += 1
        yield start, end, payload[start:end]
        start = end


def _turn_groups(messages: tuple[Message, ...]) -> list[list[Message]]:
    groups: list[list[Message]] = []
    current: list[Message] = []
    for message in messages:
        if message.role == Role.USER:
            if current:
                groups.append(current)
            current = [message]
        else:
            current.append(message)
    if current:
        groups.append(current)
    return groups


def _block_payload(block: Any) -> bytes:
    value = (
        block.model_dump(mode="json")
        if hasattr(block, "model_dump")
        else block
    )
    return _canonical_json(value).encode("utf-8")


def _block_kind(block: Any) -> str:
    value = getattr(block, "type", None)
    return str(getattr(value, "value", value) or "unknown")


def _search_text(block: Any) -> str:
    text = getattr(block, "text", None)
    if isinstance(text, str):
        return text
    data = getattr(block, "data", None)
    if isinstance(data, Mapping):
        for key in ("output", "arguments", "name"):
            value = data.get(key)
            if value is not None:
                return str(value)
    return ""


def _tool_payload(message: Message) -> Mapping[str, Any]:
    for block in message.content:
        data = getattr(block, "data", None)
        if isinstance(data, Mapping):
            return data
    return {}


def _message_envelope(message: Message) -> str:
    return _canonical_json(
        message.model_dump(
            mode="json",
            exclude={"id", "content"},
            exclude_none=False,
        ),
    )


def _turn_usage(messages: list[Message]) -> str | None:
    for message in reversed(messages):
        metadata = message.metadata
        if not isinstance(metadata, Mapping):
            continue
        usage = metadata.get("qwenpaw_turn_usage")
        nested = metadata.get("metadata")
        if usage is None and isinstance(nested, Mapping):
            usage = nested.get("qwenpaw_turn_usage")
        if usage is not None:
            return _canonical_json(usage)
    return None


def _insert_message(
    connection: sqlite3.Connection,
    *,
    conversation_id: str,
    turn_id: str,
    turn_index: int,
    message_index: int,
    message: Message,
    now_us: int,
    tool_calls: dict[str, tuple[str, str]],
    tool_results: dict[str, str],
    recall_sequences: Iterator[int],
) -> int:
    message_id = _stable_id(
        conversation_id,
        f"turn:{turn_index}:message:{message_index}",
    )
    connection.execute(
        "INSERT INTO chat_messages("
        "message_id, turn_id, role, ordinal, state, metadata_json, "
        "created_at_us, updated_at_us) "
        "VALUES (?, ?, ?, ?, 'completed', ?, ?, ?)",
        (
            message_id,
            turn_id,
            (message.role or Role.ASSISTANT).value,
            message_index,
            _message_envelope(message),
            now_us,
            now_us,
        ),
    )
    block_ids: list[str] = []
    for block_index, block in enumerate(message.content):
        block_id = _stable_id(
            conversation_id,
            f"turn:{turn_index}:message:{message_index}:block:{block_index}",
        )
        block_ids.append(block_id)
        payload = _block_payload(block)
        content_hash = hashlib.sha256(payload).hexdigest()
        legacy_sequence = None
        if block_index == 0 and isinstance(message.metadata, Mapping):
            legacy_sequence = message.metadata.get("legacy_recall_seq")
        try:
            recall_sequence = (
                int(legacy_sequence)
                if legacy_sequence is not None
                else next(recall_sequences)
            )
        except StopIteration:
            recall_sequence = None
        connection.execute(
            "INSERT INTO chat_blocks("
            "block_id, message_id, recall_seq, kind, ordinal, state, "
            "total_bytes, content_sha256, entity_version, created_at_us, "
            "updated_at_us) VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, 1, "
            "?, ?)",
            (
                block_id,
                message_id,
                recall_sequence,
                _block_kind(block),
                block_index,
                len(payload),
                content_hash,
                now_us,
                now_us,
            ),
        )
        for segment_index, (start, end, segment) in enumerate(
            _utf8_segments(payload),
        ):
            connection.execute(
                "INSERT INTO chat_block_segments("
                "block_id, segment_ordinal, start_byte, end_byte, payload, "
                "segment_sha256, created_at_us) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    block_id,
                    segment_index,
                    start,
                    end,
                    segment,
                    hashlib.sha256(segment).hexdigest(),
                    now_us,
                ),
            )
        connection.execute(
            "INSERT INTO chat_blocks_fts(block_id, content) VALUES (?, ?)",
            (block_id, _search_text(block)),
        )
    payload = _tool_payload(message)
    call_id = payload.get("call_id")
    if call_id is not None and block_ids:
        call_key = str(call_id)
        if message.type in {
            MessageType.PLUGIN_CALL,
            MessageType.FUNCTION_CALL,
            MessageType.MCP_TOOL_CALL,
        }:
            tool_calls[call_key] = (
                block_ids[0],
                str(payload.get("name") or ""),
            )
        elif message.type in {
            MessageType.PLUGIN_CALL_OUTPUT,
            MessageType.FUNCTION_CALL_OUTPUT,
            MessageType.MCP_TOOL_CALL_OUTPUT,
        }:
            tool_results[call_key] = block_ids[0]
    return len(block_ids)


def materialize_normalized_transcript(
    path: Path,
    *,
    conversation_id: str,
    agent_id: str,
    owner_scope: str,
    backend: str,
    normalized: NormalizedTranscript,
    snapshot: LegacySnapshotManifest,
    now_us: int,
    allocated_recall_sequences: Iterable[int] = (),
) -> MaterializationResult:
    """Write one normalized legacy transcript into an empty Chat database."""
    initialize_conversation_database(
        path,
        conversation_id=conversation_id,
        agent_id=agent_id,
        owner_scope=owner_scope,
        backend=backend,
        created_at_us=now_us,
    )
    initial_context_id = _stable_id(conversation_id, "context:initial")
    groups = _turn_groups(normalized.messages)
    tool_calls: dict[str, tuple[str, str]] = {}
    tool_results: dict[str, str] = {}
    block_count = 0
    parent_turn_id: str | None = None
    write_seq = 1
    recall_sequences = iter(allocated_recall_sequences)
    with connect_database(path) as connection:
        existing = connection.execute(
            "SELECT COUNT(*) FROM chat_context_revisions",
        ).fetchone()[0]
        if int(existing) != 0:
            raise ChatStorageSchemaError(
                "Refusing to materialize into a non-empty Chat database",
            )
        with immediate_transaction(connection):
            connection.execute(
                "INSERT INTO chat_context_revisions("
                "context_revision_id, kind, write_seq, created_at_us) "
                "VALUES (?, 'initial', ?, ?)",
                (initial_context_id, write_seq, now_us),
            )
            connection.execute(
                "UPDATE chat_meta SET current_context_revision_id = ?, "
                "write_seq = ?, updated_at_us = ? WHERE singleton_id = 1",
                (initial_context_id, write_seq, now_us),
            )
        for turn_index, turn_messages in enumerate(groups):
            turn_id = _stable_id(conversation_id, f"turn:{turn_index}")
            write_seq += 1
            with immediate_transaction(connection):
                connection.execute(
                    "INSERT INTO chat_turns("
                    "turn_id, parent_turn_id, client_request_id, state, "
                    "lineage_depth, usage_json, "
                    "write_seq, created_at_us, updated_at_us, "
                    "terminal_at_us) VALUES "
                    "(?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?)",
                    (
                        turn_id,
                        parent_turn_id,
                        f"legacy:{turn_index}",
                        turn_index,
                        _turn_usage(turn_messages),
                        write_seq,
                        now_us,
                        now_us,
                        now_us,
                    ),
                )
                for message_index, message in enumerate(turn_messages):
                    block_count += _insert_message(
                        connection,
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        turn_index=turn_index,
                        message_index=message_index,
                        message=message,
                        now_us=now_us,
                        tool_calls=tool_calls,
                        tool_results=tool_results,
                        recall_sequences=recall_sequences,
                    )
                write_seq += 1
                context_id = _stable_id(
                    conversation_id,
                    f"context:turn:{turn_index}",
                )
                connection.execute(
                    "INSERT INTO chat_context_revisions("
                    "context_revision_id, parent_context_revision_id, "
                    "anchor_turn_id, kind, write_seq, created_at_us) "
                    "VALUES (?, ?, ?, 'turn_commit', ?, ?)",
                    (
                        context_id,
                        initial_context_id
                        if turn_index == 0
                        else _stable_id(
                            conversation_id,
                            f"context:turn:{turn_index - 1}",
                        ),
                        turn_id,
                        write_seq,
                        now_us,
                    ),
                )
                connection.execute(
                    "UPDATE chat_turns SET context_revision_id = ? "
                    "WHERE turn_id = ?",
                    (context_id, turn_id),
                )
                parent_turn_id = turn_id
        with immediate_transaction(connection):
            for call_id, (call_block_id, tool_name) in tool_calls.items():
                result_block_id = tool_results.get(call_id)
                connection.execute(
                    "INSERT INTO chat_tool_links("
                    "tool_link_id, provider_call_id, call_block_id, "
                    "result_block_id, state, tool_name, created_at_us, "
                    "updated_at_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        _stable_id(conversation_id, f"tool:{call_id}"),
                        call_id,
                        call_block_id,
                        result_block_id,
                        "completed" if result_block_id else "unknown",
                        tool_name,
                        now_us,
                        now_us,
                    ),
                )
            final_context_id = (
                _stable_id(
                    conversation_id,
                    f"context:turn:{len(groups) - 1}",
                )
                if groups
                else initial_context_id
            )
            if normalized.runtime_context is not None:
                runtime_hash = hashlib.sha256(
                    normalized.runtime_context,
                ).hexdigest()
                connection.execute(
                    "INSERT OR IGNORE INTO chat_runtime_blobs("
                    "blob_hash, codec, payload, uncompressed_bytes, "
                    "created_at_us) VALUES (?, 'agentscope-json', ?, ?, ?)",
                    (
                        runtime_hash,
                        normalized.runtime_context,
                        len(normalized.runtime_context),
                        now_us,
                    ),
                )
                connection.execute(
                    "UPDATE chat_context_revisions SET runtime_blob_hash = ? "
                    "WHERE context_revision_id = ?",
                    (runtime_hash, final_context_id),
                )
            connection.execute(
                "UPDATE chat_meta SET head_turn_id = ?, "
                "current_context_revision_id = ?, updated_at_us = ? "
                "WHERE singleton_id = 1",
                (parent_turn_id, final_context_id, now_us),
            )
            connection.execute(
                "INSERT INTO chat_aliases("
                "alias_namespace, external_key, owner_scope, created_at_us, "
                "updated_at_us) VALUES ('legacy_session', ?, ?, ?, ?)",
                (
                    snapshot.session_id,
                    owner_scope,
                    now_us,
                    now_us,
                ),
            )
            connection.execute(
                "UPDATE chat_meta SET source_generation = 1, write_seq = ?, "
                "updated_at_us = ? WHERE singleton_id = 1",
                (write_seq, now_us),
            )
            connection.execute(
                "INSERT INTO chat_migration_record("
                "singleton_id, source_locator, source_fingerprint, "
                "migration_version, transcript_manifest_json, "
                "source_metadata_json, completed_at_us) "
                "VALUES (1, ?, ?, 1, ?, ?, ?)",
                (
                    f"legacy:{snapshot.session_id}",
                    snapshot.manifest_sha256,
                    _canonical_json(normalized.manifest.to_dict()),
                    _canonical_json(
                        {
                            "diagnostics": list(normalized.diagnostics),
                            "history_parts": normalized.history_parts,
                            "session_parts": normalized.session_parts,
                        },
                    ),
                    now_us,
                ),
            )
            projection_payload = _canonical_json(
                {
                    "conversation_id": conversation_id,
                    "source_generation": 1,
                },
            )
            connection.executemany(
                "INSERT INTO chat_projection_outbox("
                "projection, operation, source_generation, payload_json, "
                "created_at_us, updated_at_us) "
                "VALUES (?, 'migration_ready', 1, ?, ?, ?)",
                (
                    ("catalog", projection_payload, now_us, now_us),
                    ("recall", projection_payload, now_us, now_us),
                ),
            )
    stored_messages = read_transcript(path)
    stored_manifest = build_transcript_manifest(stored_messages)
    if stored_manifest != normalized.manifest:
        raise ChatStorageSchemaError(
            "Materialized Chat transcript does not match its source manifest",
        )
    return MaterializationResult(
        turns=len(groups),
        messages=len(normalized.messages),
        blocks=block_count,
        source_manifest=normalized.manifest,
        stored_manifest=stored_manifest,
    )


def read_transcript(path: Path) -> list[Message]:
    """Read the single Chat timeline in chronological order."""
    with connect_database(path, readonly=True) as connection:
        rows = connection.execute(
            "WITH RECURSIVE lineage(turn_id, parent_turn_id, depth) AS ("
            "SELECT t.turn_id, t.parent_turn_id, t.lineage_depth FROM "
            "chat_meta AS meta JOIN chat_turns AS t "
            "ON t.turn_id = meta.head_turn_id WHERE meta.singleton_id = 1 "
            "UNION ALL SELECT t.turn_id, "
            "t.parent_turn_id, t.lineage_depth FROM chat_turns AS t "
            "JOIN lineage AS l ON t.turn_id = l.parent_turn_id) "
            "SELECT m.message_id, m.metadata_json, m.state, "
            "b.block_id, b.ordinal AS block_ordinal, "
            "b.metadata_json AS block_metadata_json, "
            "s.segment_ordinal, s.payload "
            "FROM lineage AS l JOIN chat_messages AS m "
            "ON m.turn_id = l.turn_id "
            "LEFT JOIN chat_blocks AS b ON b.message_id = m.message_id "
            "LEFT JOIN chat_block_segments AS s "
            "ON s.block_id = b.block_id "
            "ORDER BY l.depth, m.ordinal, b.ordinal, "
            "s.segment_ordinal",
        ).fetchall()
    messages: dict[str, dict[str, Any]] = {}
    for row in rows:
        message_id = str(row["message_id"])
        record = messages.setdefault(
            message_id,
            {
                "metadata_json": str(row["metadata_json"]),
                "state": str(row["state"]),
                "blocks": {},
            },
        )
        if row["block_id"] is None:
            continue
        block_id = str(row["block_id"])
        block = record["blocks"].setdefault(
            block_id,
            {
                "metadata_json": str(row["block_metadata_json"]),
                "ordinal": int(row["block_ordinal"]),
                "segments": [],
            },
        )
        if row["payload"] is not None:
            block["segments"].append(bytes(row["payload"]))

    result: list[Message] = []
    for message_id, record in messages.items():
        content: list[Any] = []
        blocks = sorted(
            record["blocks"].values(),
            key=lambda value: value["ordinal"],
        )
        for block in blocks:
            payload = b"".join(block["segments"])
            block_metadata = json.loads(block["metadata_json"])
            if block_metadata.get("storage_codec") == "utf8_text_delta":
                content.append(
                    {
                        "type": "text",
                        "text": payload.decode("utf-8", errors="replace"),
                    },
                )
            else:
                content.append(json.loads(payload.decode("utf-8")))
        envelope = json.loads(record["metadata_json"])
        envelope["status"] = {
            "partial": RunStatus.InProgress.value,
            "completed": RunStatus.Completed.value,
            "failed": RunStatus.Failed.value,
            "interrupted": RunStatus.Cancelled.value,
        }[record["state"]]
        result.append(
            Message.model_validate(
                {
                    **envelope,
                    "id": message_id,
                    "content": content,
                },
            ),
        )
    return result
