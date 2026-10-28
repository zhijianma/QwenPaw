# -*- coding: utf-8 -*-
"""Bounded partial-block persistence and atomic run finalization."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Literal
from uuid import uuid4

from ....schemas import MessageType, Role, RunStatus
from .common import connect_database, immediate_transaction


PARTIAL_FLUSH_INTERVAL_SECONDS = 0.250
PARTIAL_FLUSH_BYTES = 64 * 1024
ContextRevisionKind = Literal["turn_commit", "clear"]


class RunFencingError(RuntimeError):
    """A stale process attempted to mutate a run it no longer owns."""


def _segments(payload: bytes) -> Iterator[tuple[int, int, bytes]]:
    start = 0
    while start < len(payload):
        end = min(start + PARTIAL_FLUSH_BYTES, len(payload))
        if end < len(payload):
            while end > start and payload[end] & 0xC0 == 0x80:
                end -= 1
        if end == start:
            end = min(start + PARTIAL_FLUSH_BYTES, len(payload))
            while end < len(payload) and payload[end] & 0xC0 == 0x80:
                end += 1
        yield start, end, payload[start:end]
        start = end


@dataclass(frozen=True)
class RuntimeRunHandle:
    """Stable source coordinates for one accepted runtime execution."""

    conversation_id: str
    turn_id: str
    run_id: str
    fencing_token: int


@dataclass(frozen=True)
class PartialBlockHandle(RuntimeRunHandle):
    """Stable source coordinates for one streaming output block."""

    message_id: str
    block_id: str


@dataclass(frozen=True)
class PendingBlockSegment:
    """One immutable not-yet-calibrated byte range for live sync."""

    start_byte: int
    entity_version: int
    payload: bytes


@dataclass(frozen=True)
class PartialBlockSnapshot:
    """Copy-on-write view of durable and pending block boundaries."""

    handle: PartialBlockHandle
    durable_bytes: int
    total_bytes: int
    entity_version: int
    pending: tuple[PendingBlockSegment, ...]


@dataclass(frozen=True)
class PartialBlockAppend:
    """Coordinates for one live delta plus the resulting block view."""

    expected_offset: int
    entity_version: int
    payload: bytes
    snapshot: PartialBlockSnapshot


@dataclass(frozen=True)
class CompletedMessageAppend:
    """Canonical entities created for one completed runtime message."""

    message_id: str
    block_ids: tuple[str, ...]


def load_runtime_run_handle(
    path: Path,
    *,
    conversation_id: str,
    run_id: str,
) -> RuntimeRunHandle:
    """Load the immutable coordinates and fencing token for a run."""
    with connect_database(path, readonly=True) as connection:
        row = connection.execute(
            "SELECT turn_id, fencing_token FROM chat_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    if row is None:
        raise LookupError(f"Run not found: {run_id}")
    return RuntimeRunHandle(
        conversation_id=conversation_id,
        turn_id=str(row["turn_id"]),
        run_id=run_id,
        fencing_token=int(row["fencing_token"]),
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _require_run(connection, run_id: str, fencing_token: int):
    run = connection.execute(
        "SELECT turn_id, state, fencing_token "
        "FROM chat_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if run is None:
        raise LookupError(f"Run not found: {run_id}")
    if int(run["fencing_token"]) != fencing_token:
        raise RunFencingError(f"Stale fencing token for run: {run_id}")
    if str(run["state"]) not in {"accepted", "running", "stopping"}:
        raise RunFencingError(f"Run is already terminal: {run_id}")
    return run


def _next_generation(connection) -> tuple[int, int]:
    meta = connection.execute(
        "SELECT source_generation, write_seq FROM chat_meta "
        "WHERE singleton_id = 1",
    ).fetchone()
    return int(meta["source_generation"]) + 1, int(meta["write_seq"]) + 1


def _insert_outbox(
    connection,
    *,
    conversation_id: str,
    generation: int,
    operation: str,
    now_us: int,
) -> None:
    payload = _canonical_json(
        {
            "conversation_id": conversation_id,
            "source_generation": generation,
        },
    )
    connection.executemany(
        "INSERT INTO chat_projection_outbox("
        "projection, operation, source_generation, payload_json, "
        "created_at_us, updated_at_us) VALUES (?, ?, ?, ?, ?, ?)",
        (
            (
                projection,
                operation,
                generation,
                payload,
                now_us,
                now_us,
            )
            for projection in ("catalog", "recall")
        ),
    )


def _insert_completed_block(
    connection,
    *,
    message_id: str,
    ordinal: int,
    block: Any,
    recall_seq: int,
    now_us: int,
) -> tuple[str, int]:
    block_id = str(uuid4())
    value = (
        block.model_dump(mode="json")
        if hasattr(block, "model_dump")
        else block
    )
    payload = _canonical_json(value).encode("utf-8")
    kind = getattr(getattr(block, "type", None), "value", None)
    connection.execute(
        "INSERT INTO chat_blocks("
        "block_id, message_id, recall_seq, kind, ordinal, state, "
        "total_bytes, content_sha256, entity_version, metadata_json, "
        "created_at_us, updated_at_us) VALUES "
        "(?, ?, ?, ?, ?, 'completed', ?, ?, 1, '{}', ?, ?)",
        (
            block_id,
            message_id,
            recall_seq,
            str(kind or "unknown"),
            ordinal,
            len(payload),
            hashlib.sha256(payload).hexdigest(),
            now_us,
            now_us,
        ),
    )
    for segment_ordinal, (start, end, segment) in enumerate(
        _segments(payload),
    ):
        connection.execute(
            "INSERT INTO chat_block_segments("
            "block_id, segment_ordinal, start_byte, end_byte, payload, "
            "segment_sha256, created_at_us) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                block_id,
                segment_ordinal,
                start,
                end,
                segment,
                hashlib.sha256(segment).hexdigest(),
                now_us,
            ),
        )
    search_text = getattr(block, "text", None)
    if not isinstance(search_text, str):
        search_text = _canonical_json(value)
    connection.execute(
        "INSERT INTO chat_blocks_fts(block_id, content) VALUES (?, ?)",
        (block_id, search_text),
    )
    return block_id, len(payload)


def begin_partial_text_block(
    path: Path,
    *,
    conversation_id: str,
    run_id: str,
    fencing_token: int,
    recall_seq: int,
    message_type: MessageType = MessageType.MESSAGE,
    block_kind: str = "text",
    source_message_id: str | None = None,
    existing_message_id: str | None = None,
    now_us: int,
) -> PartialBlockHandle:
    """Create a visible partial assistant block before accepting deltas."""
    with connect_database(path) as connection:
        with immediate_transaction(connection):
            run = _require_run(connection, run_id, fencing_token)
            turn_id = str(run["turn_id"])
            message_id = existing_message_id or str(uuid4())
            block_id = str(uuid4())
            if existing_message_id is None:
                ordinal = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(ordinal), -1) + 1 "
                        "FROM chat_messages WHERE turn_id = ?",
                        (turn_id,),
                    ).fetchone()[0],
                )
                envelope = {
                    "id": source_message_id,
                    "metadata": None,
                    "role": Role.ASSISTANT.value,
                    "status": RunStatus.InProgress.value,
                    "type": message_type.value,
                }
                connection.execute(
                    "INSERT INTO chat_messages("
                    "message_id, turn_id, role, ordinal, state, "
                    "metadata_json, created_at_us, updated_at_us) "
                    "VALUES (?, ?, 'assistant', ?, 'partial', ?, ?, ?)",
                    (
                        message_id,
                        turn_id,
                        ordinal,
                        _canonical_json(envelope),
                        now_us,
                        now_us,
                    ),
                )
            else:
                existing = connection.execute(
                    "SELECT 1 FROM chat_messages WHERE message_id = ? "
                    "AND turn_id = ? AND state = 'partial'",
                    (message_id, turn_id),
                ).fetchone()
                if existing is None:
                    raise RunFencingError(
                        f"Partial message is not writable: {message_id}",
                    )
            block_ordinal = int(
                connection.execute(
                    "SELECT COALESCE(MAX(ordinal), -1) + 1 "
                    "FROM chat_blocks WHERE message_id = ?",
                    (message_id,),
                ).fetchone()[0],
            )
            connection.execute(
                "INSERT INTO chat_blocks("
                "block_id, message_id, recall_seq, kind, ordinal, state, "
                "metadata_json, created_at_us, updated_at_us) "
                "VALUES (?, ?, ?, ?, ?, 'partial', ?, ?, ?)",
                (
                    block_id,
                    message_id,
                    recall_seq,
                    block_kind,
                    block_ordinal,
                    _canonical_json({"storage_codec": "utf8_text_delta"}),
                    now_us,
                    now_us,
                ),
            )
            generation, write_seq = _next_generation(connection)
            connection.execute(
                "UPDATE chat_runs SET state = 'running', updated_at_us = ? "
                "WHERE run_id = ?",
                (now_us, run_id),
            )
            connection.execute(
                "UPDATE chat_turns SET state = 'running', updated_at_us = ? "
                "WHERE turn_id = ?",
                (now_us, turn_id),
            )
            connection.execute(
                "UPDATE chat_meta SET source_generation = ?, write_seq = ?, "
                "updated_at_us = ? WHERE singleton_id = 1",
                (generation, write_seq, now_us),
            )
            _insert_outbox(
                connection,
                conversation_id=conversation_id,
                generation=generation,
                operation="partial_started",
                now_us=now_us,
            )
    return PartialBlockHandle(
        conversation_id=conversation_id,
        turn_id=turn_id,
        run_id=run_id,
        message_id=message_id,
        block_id=block_id,
        fencing_token=fencing_token,
    )


def append_completed_message(
    path: Path,
    handle: RuntimeRunHandle,
    message: Any,
    *,
    recall_sequences: list[int],
    now_us: int,
) -> CompletedMessageAppend:
    """Persist one non-streamed completed message at an event boundary."""
    content = list(getattr(message, "content", None) or [])
    if len(content) != len(recall_sequences):
        raise ValueError("A recall sequence is required for every block")
    with connect_database(path) as connection:
        with immediate_transaction(connection):
            _require_run(connection, handle.run_id, handle.fencing_token)
            ordinal = int(
                connection.execute(
                    "SELECT COALESCE(MAX(ordinal), -1) + 1 "
                    "FROM chat_messages WHERE turn_id = ?",
                    (handle.turn_id,),
                ).fetchone()[0],
            )
            message_id = str(uuid4())
            message_value = (
                message.model_dump(mode="json", exclude={"content"})
                if hasattr(message, "model_dump")
                else {"value": message}
            )
            role = getattr(getattr(message, "role", None), "value", None)
            role = str(role or "assistant")
            if role not in {"system", "user", "assistant", "tool"}:
                role = "assistant"
            connection.execute(
                "INSERT INTO chat_messages("
                "message_id, turn_id, role, ordinal, state, metadata_json, "
                "created_at_us, updated_at_us) "
                "VALUES (?, ?, ?, ?, 'completed', ?, ?, ?)",
                (
                    message_id,
                    handle.turn_id,
                    role,
                    ordinal,
                    _canonical_json(message_value),
                    now_us,
                    now_us,
                ),
            )
            block_ids: list[str] = []
            durable_bytes = 0
            for block_ordinal, (block, recall_seq) in enumerate(
                zip(content, recall_sequences, strict=True),
            ):
                block_id, block_bytes = _insert_completed_block(
                    connection,
                    message_id=message_id,
                    ordinal=block_ordinal,
                    block=block,
                    recall_seq=recall_seq,
                    now_us=now_us,
                )
                block_ids.append(block_id)
                durable_bytes += block_bytes
            message_type = str(
                getattr(
                    getattr(message, "type", None),
                    "value",
                    getattr(message, "type", ""),
                ),
            )
            if block_ids and message_type in {
                MessageType.PLUGIN_CALL.value,
                MessageType.FUNCTION_CALL.value,
                MessageType.MCP_TOOL_CALL.value,
            }:
                data = getattr(content[0], "data", None)
                call_id = (
                    data.get("call_id") if isinstance(data, dict) else None
                )
                if call_id:
                    connection.execute(
                        "INSERT INTO chat_tool_links("
                        "tool_link_id, provider_call_id, call_block_id, "
                        "state, tool_name, created_at_us, updated_at_us) "
                        "VALUES (?, ?, ?, 'running', ?, ?, ?) "
                        "ON CONFLICT(provider_call_id) DO UPDATE SET "
                        "call_block_id = excluded.call_block_id, "
                        "tool_name = excluded.tool_name, "
                        "updated_at_us = excluded.updated_at_us",
                        (
                            str(uuid4()),
                            str(call_id),
                            block_ids[0],
                            str(data.get("name") or ""),
                            now_us,
                            now_us,
                        ),
                    )
            if block_ids and message_type in {
                MessageType.PLUGIN_CALL_OUTPUT.value,
                MessageType.FUNCTION_CALL_OUTPUT.value,
                MessageType.MCP_TOOL_CALL_OUTPUT.value,
            }:
                data = getattr(content[0], "data", None)
                call_id = (
                    data.get("call_id") if isinstance(data, dict) else None
                )
                if call_id:
                    connection.execute(
                        "UPDATE chat_tool_links SET result_block_id = ?, "
                        "state = ?, updated_at_us = ? "
                        "WHERE provider_call_id = ?",
                        (
                            block_ids[0],
                            (
                                "failed"
                                if str(data.get("state") or "") == "failed"
                                else "completed"
                            ),
                            now_us,
                            str(call_id),
                        ),
                    )
            generation, write_seq = _next_generation(connection)
            connection.execute(
                "UPDATE chat_runs SET state = 'running', "
                "durable_event_seq = durable_event_seq + 1, "
                "durable_bytes = durable_bytes + ?, updated_at_us = ? "
                "WHERE run_id = ?",
                (durable_bytes, now_us, handle.run_id),
            )
            connection.execute(
                "UPDATE chat_turns SET state = 'running', updated_at_us = ? "
                "WHERE turn_id = ?",
                (now_us, handle.turn_id),
            )
            connection.execute(
                "UPDATE chat_meta SET source_generation = ?, write_seq = ?, "
                "updated_at_us = ? WHERE singleton_id = 1",
                (generation, write_seq, now_us),
            )
            _insert_outbox(
                connection,
                conversation_id=handle.conversation_id,
                generation=generation,
                operation="message_completed",
                now_us=now_us,
            )
            return CompletedMessageAppend(message_id, tuple(block_ids))


def complete_partial_message(
    path: Path,
    handle: PartialBlockHandle,
    message: Any,
    *,
    extra_content: list[Any],
    recall_sequences: list[int],
    now_us: int,
) -> list[str]:
    """Complete one streamed message and append its remaining blocks."""
    if len(extra_content) != len(recall_sequences):
        raise ValueError("A recall sequence is required for every block")
    with connect_database(path) as connection:
        with immediate_transaction(connection):
            _require_run(connection, handle.run_id, handle.fencing_token)
            row = connection.execute(
                "SELECT COALESCE(MAX(ordinal), -1) AS ordinal "
                "FROM chat_blocks WHERE message_id = ?",
                (handle.message_id,),
            ).fetchone()
            next_ordinal = int(row["ordinal"]) + 1
            envelope = (
                message.model_dump(mode="json", exclude={"content"})
                if hasattr(message, "model_dump")
                else {"value": message}
            )
            connection.execute(
                "UPDATE chat_messages SET state = 'completed', "
                "metadata_json = ?, updated_at_us = ? WHERE message_id = ?",
                (
                    _canonical_json(envelope),
                    now_us,
                    handle.message_id,
                ),
            )
            partials = connection.execute(
                "SELECT block_id FROM chat_blocks WHERE message_id = ? "
                "AND state = 'partial'",
                (handle.message_id,),
            ).fetchall()
            for partial in partials:
                segments = connection.execute(
                    "SELECT payload FROM chat_block_segments "
                    "WHERE block_id = ? ORDER BY segment_ordinal",
                    (partial["block_id"],),
                ).fetchall()
                payload = b"".join(
                    bytes(segment["payload"]) for segment in segments
                )
                connection.execute(
                    "UPDATE chat_blocks SET state = 'completed', "
                    "content_sha256 = ?, updated_at_us = ? "
                    "WHERE block_id = ?",
                    (
                        hashlib.sha256(payload).hexdigest(),
                        now_us,
                        partial["block_id"],
                    ),
                )
                connection.execute(
                    "DELETE FROM chat_blocks_fts WHERE block_id = ?",
                    (partial["block_id"],),
                )
                connection.execute(
                    "INSERT INTO chat_blocks_fts(block_id, content) "
                    "VALUES (?, ?)",
                    (
                        partial["block_id"],
                        payload.decode("utf-8", errors="replace"),
                    ),
                )
            block_ids: list[str] = []
            durable_bytes = 0
            for offset, (block, recall_seq) in enumerate(
                zip(extra_content, recall_sequences, strict=True),
            ):
                block_id, block_bytes = _insert_completed_block(
                    connection,
                    message_id=handle.message_id,
                    ordinal=next_ordinal + offset,
                    block=block,
                    recall_seq=recall_seq,
                    now_us=now_us,
                )
                block_ids.append(block_id)
                durable_bytes += block_bytes
            generation, write_seq = _next_generation(connection)
            connection.execute(
                "UPDATE chat_runs SET durable_event_seq = "
                "durable_event_seq + 1, durable_bytes = durable_bytes + ?, "
                "updated_at_us = ? WHERE run_id = ?",
                (durable_bytes, now_us, handle.run_id),
            )
            connection.execute(
                "UPDATE chat_meta SET source_generation = ?, "
                "write_seq = ?, updated_at_us = ? WHERE singleton_id = 1",
                (generation, write_seq, now_us),
            )
            _insert_outbox(
                connection,
                conversation_id=handle.conversation_id,
                generation=generation,
                operation="message_completed",
                now_us=now_us,
            )
            return block_ids


def append_partial_block(
    path: Path,
    handle: PartialBlockHandle,
    payload: bytes,
    *,
    now_us: int,
) -> int:
    """Append bounded durable delta segments and return entity version."""
    if not payload:
        return 0
    with connect_database(path) as connection:
        with immediate_transaction(connection):
            _require_run(connection, handle.run_id, handle.fencing_token)
            block = connection.execute(
                "SELECT total_bytes, entity_version, state FROM chat_blocks "
                "WHERE block_id = ?",
                (handle.block_id,),
            ).fetchone()
            if block is None or str(block["state"]) != "partial":
                raise RunFencingError(
                    f"Partial block is not writable: {handle.block_id}",
                )
            start = int(block["total_bytes"])
            end = start + len(payload)
            ordinal = int(
                connection.execute(
                    "SELECT COALESCE(MAX(segment_ordinal), -1) + 1 "
                    "FROM chat_block_segments WHERE block_id = ?",
                    (handle.block_id,),
                ).fetchone()[0],
            )
            for offset, (relative_start, relative_end, segment) in enumerate(
                _segments(payload),
            ):
                connection.execute(
                    "INSERT INTO chat_block_segments("
                    "block_id, segment_ordinal, start_byte, end_byte, "
                    "payload, segment_sha256, created_at_us) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        handle.block_id,
                        ordinal + offset,
                        start + relative_start,
                        start + relative_end,
                        segment,
                        hashlib.sha256(segment).hexdigest(),
                        now_us,
                    ),
                )
            version = int(block["entity_version"]) + 1
            connection.execute(
                "UPDATE chat_blocks SET total_bytes = ?, entity_version = ?, "
                "updated_at_us = ? WHERE block_id = ?",
                (end, version, now_us, handle.block_id),
            )
            generation, write_seq = _next_generation(connection)
            connection.execute(
                "UPDATE chat_runs SET durable_event_seq = "
                "durable_event_seq + 1, durable_bytes = durable_bytes + ?, "
                "updated_at_us = ? WHERE run_id = ?",
                (len(payload), now_us, handle.run_id),
            )
            connection.execute(
                "UPDATE chat_meta SET source_generation = ?, write_seq = ?, "
                "updated_at_us = ? WHERE singleton_id = 1",
                (generation, write_seq, now_us),
            )
            _insert_outbox(
                connection,
                conversation_id=handle.conversation_id,
                generation=generation,
                operation="partial_flushed",
                now_us=now_us,
            )
            return version


def finalize_run(
    path: Path,
    handle: RuntimeRunHandle,
    *,
    state: Literal["completed", "failed", "canceled"],
    runtime_context: bytes | None,
    context_kind: ContextRevisionKind = "turn_commit",
    runtime_codec: str = "agentscope-json",
    usage: dict[str, Any] | None = None,
    error_digest: str | None = None,
    now_us: int,
) -> int:
    """Atomically finalize source entities and commit runtime context."""
    with connect_database(path) as connection:
        with immediate_transaction(connection):
            _require_run(connection, handle.run_id, handle.fencing_token)
            block_rows = connection.execute(
                "SELECT block_id FROM chat_blocks WHERE state = 'partial' "
                "AND message_id IN (SELECT message_id FROM chat_messages "
                "WHERE turn_id = ?)",
                (handle.turn_id,),
            ).fetchall()
            terminal_entity_state = {
                "completed": "completed",
                "failed": "failed",
                "canceled": "interrupted",
            }[state]
            for row in block_rows:
                segments = connection.execute(
                    "SELECT payload FROM chat_block_segments "
                    "WHERE block_id = ? ORDER BY segment_ordinal",
                    (row["block_id"],),
                ).fetchall()
                digest = hashlib.sha256(
                    b"".join(
                        bytes(segment["payload"]) for segment in segments
                    ),
                ).hexdigest()
                connection.execute(
                    "UPDATE chat_blocks SET state = ?, content_sha256 = ?, "
                    "updated_at_us = ? WHERE block_id = ?",
                    (
                        terminal_entity_state,
                        digest,
                        now_us,
                        row["block_id"],
                    ),
                )
                content = b"".join(
                    bytes(segment["payload"]) for segment in segments
                ).decode("utf-8", errors="replace")
                connection.execute(
                    "DELETE FROM chat_blocks_fts WHERE block_id = ?",
                    (row["block_id"],),
                )
                connection.execute(
                    "INSERT INTO chat_blocks_fts(block_id, content) "
                    "VALUES (?, ?)",
                    (row["block_id"], content),
                )
            connection.execute(
                "UPDATE chat_messages SET state = ?, updated_at_us = ? "
                "WHERE turn_id = ? AND state = 'partial'",
                (terminal_entity_state, now_us, handle.turn_id),
            )
            terminal_tool_state = {
                "completed": "unknown",
                "failed": "failed",
                "canceled": "interrupted",
            }[state]
            connection.execute(
                "UPDATE chat_tool_links SET state = ?, updated_at_us = ? "
                "WHERE state = 'running' AND call_block_id IN ("
                "SELECT b.block_id FROM chat_blocks AS b "
                "JOIN chat_messages AS m ON m.message_id = b.message_id "
                "WHERE m.turn_id = ?)",
                (terminal_tool_state, now_us, handle.turn_id),
            )
            generation, write_seq = _next_generation(connection)
            parent_context = connection.execute(
                "SELECT current_context_revision_id FROM chat_meta "
                "WHERE singleton_id = 1",
            ).fetchone()[0]
            blob_hash = None
            if runtime_context is not None:
                blob_hash = hashlib.sha256(runtime_context).hexdigest()
                connection.execute(
                    "INSERT OR IGNORE INTO chat_runtime_blobs("
                    "blob_hash, codec, payload, uncompressed_bytes, "
                    "created_at_us) VALUES (?, ?, ?, ?, ?)",
                    (
                        blob_hash,
                        runtime_codec,
                        runtime_context,
                        len(runtime_context),
                        now_us,
                    ),
                )
            context_id = str(uuid4())
            connection.execute(
                "INSERT INTO chat_context_revisions("
                "context_revision_id, parent_context_revision_id, "
                "anchor_turn_id, kind, runtime_blob_hash, write_seq, "
                "created_at_us) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    context_id,
                    parent_context,
                    handle.turn_id,
                    context_kind,
                    blob_hash,
                    write_seq,
                    now_us,
                ),
            )
            connection.execute(
                "UPDATE chat_turns SET context_revision_id = ?, state = ?, "
                "usage_json = ?, error_digest = ?, updated_at_us = ?, "
                "terminal_at_us = ? WHERE turn_id = ?",
                (
                    context_id,
                    state,
                    _canonical_json(usage) if usage is not None else None,
                    error_digest,
                    now_us,
                    now_us,
                    handle.turn_id,
                ),
            )
            connection.execute(
                "UPDATE chat_runs SET state = ?, error_digest = ?, "
                "updated_at_us = ?, terminal_at_us = ? WHERE run_id = ?",
                (state, error_digest, now_us, now_us, handle.run_id),
            )
            connection.execute(
                "UPDATE chat_meta SET current_context_revision_id = ?, "
                "updated_at_us = ? WHERE singleton_id = 1",
                (context_id, now_us),
            )
            connection.execute(
                "UPDATE chat_meta SET source_generation = ?, write_seq = ?, "
                "updated_at_us = ? WHERE singleton_id = 1",
                (generation, write_seq, now_us),
            )
            _insert_outbox(
                connection,
                conversation_id=handle.conversation_id,
                generation=generation,
                operation=f"run_{state}",
                now_us=now_us,
            )
            return generation


class PartialBlockMaterializer:
    """Flush streaming UTF-8 text on the first time-or-size threshold."""

    def __init__(
        self,
        path: Path,
        handle: PartialBlockHandle,
        *,
        flush_interval: float = PARTIAL_FLUSH_INTERVAL_SECONDS,
        flush_bytes: int = PARTIAL_FLUSH_BYTES,
    ) -> None:
        self._path = path
        self._handle = handle
        self._flush_interval = flush_interval
        self._flush_bytes = flush_bytes
        self._buffer = bytearray()
        self._lock = asyncio.Lock()
        self._flush_lock = asyncio.Lock()
        self._timer: asyncio.Task[None] | None = None
        self._inflight = b""
        self._durable_bytes = 0
        self._entity_version = 0

    @property
    def handle(self) -> PartialBlockHandle:
        """Return durable coordinates for completion boundaries."""
        return self._handle

    def _snapshot_locked(self) -> PartialBlockSnapshot:
        pending: list[PendingBlockSegment] = []
        start = self._durable_bytes
        version = self._entity_version
        if self._inflight:
            version += 1
            pending.append(
                PendingBlockSegment(start, version, self._inflight),
            )
            start += len(self._inflight)
        if self._buffer:
            version += 1
            pending.append(
                PendingBlockSegment(start, version, bytes(self._buffer)),
            )
            start += len(self._buffer)
        return PartialBlockSnapshot(
            handle=self._handle,
            durable_bytes=self._durable_bytes,
            total_bytes=start,
            entity_version=version,
            pending=tuple(pending),
        )

    async def snapshot(self) -> PartialBlockSnapshot:
        """Capture an immutable view without waiting for durable flush."""
        async with self._lock:
            return self._snapshot_locked()

    async def append(
        self,
        delta: str,
        *,
        now_us: int,
    ) -> PartialBlockAppend:
        """Buffer one delta and flush immediately once the byte cap wins."""
        encoded = delta.encode("utf-8", errors="replace")
        should_flush = False
        async with self._lock:
            before = self._snapshot_locked()
            expected_offset = before.total_bytes
            event_version = (
                before.entity_version
                if self._buffer
                else before.entity_version + 1
            )
            self._buffer.extend(encoded)
            should_flush = len(self._buffer) >= self._flush_bytes
            if self._timer is None and not should_flush:
                self._timer = asyncio.create_task(
                    self._flush_after_delay(now_us),
                )
        if should_flush:
            await self.flush(now_us=now_us)
        snapshot = await self.snapshot()
        return PartialBlockAppend(
            expected_offset=expected_offset,
            entity_version=event_version,
            payload=encoded,
            snapshot=snapshot,
        )

    async def _flush_after_delay(self, now_us: int) -> None:
        try:
            await asyncio.sleep(self._flush_interval)
            await self.flush(now_us=now_us)
        except asyncio.CancelledError:
            return

    async def flush(self, *, now_us: int) -> int:
        """Persist all buffered bytes; safe for immediate event boundaries."""
        async with self._flush_lock:
            async with self._lock:
                payload = bytes(self._buffer)
                self._buffer.clear()
                self._inflight = payload
                timer = self._timer
                self._timer = None
            current = asyncio.current_task()
            if timer is not None and timer is not current:
                timer.cancel()
            if not payload:
                async with self._lock:
                    self._inflight = b""
                return 0
            try:
                version = await asyncio.to_thread(
                    append_partial_block,
                    self._path,
                    self._handle,
                    payload,
                    now_us=now_us,
                )
            except BaseException:
                async with self._lock:
                    self._buffer = bytearray(payload) + self._buffer
                    self._inflight = b""
                raise
            async with self._lock:
                self._durable_bytes += len(payload)
                self._entity_version = version
                self._inflight = b""
            return version

    async def close(self, *, now_us: int) -> int:
        """Flush remaining bytes and cancel the pending timer."""
        return await self.flush(now_us=now_us)
