# -*- coding: utf-8 -*-
"""Transactional acceptance and restart recovery for Chat runtime work."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4

from ....schemas import Message, Role
from .common import connect_database, immediate_transaction
from .conversation_repository import SEGMENT_TARGET_BYTES


@dataclass(frozen=True)
class AcceptanceRequest:
    """Immutable input accepted for one Chat."""

    conversation_id: str
    client_request_id: str
    message: Message
    process_instance_id: str
    now_us: int


@dataclass(frozen=True)
class AcceptanceResult:
    """Durable acceptance identity, including idempotent replay status."""

    kind: str
    work_id: str
    client_request_id: str
    turn_id: str | None
    source_generation: int
    replayed: bool


@dataclass(frozen=True)
class RestartRecoveryResult:
    """Counts of nonterminal source entities marked interrupted."""

    runs: int
    turns: int
    messages: int
    blocks: int
    tools: int
    queue_items: int
    source_generation: int


def _recovery_result(
    counts: list[int],
    source_generation: int,
) -> RestartRecoveryResult:
    return RestartRecoveryResult(
        runs=counts[0],
        turns=counts[1],
        messages=counts[2],
        blocks=counts[3],
        tools=counts[4],
        queue_items=counts[5],
        source_generation=source_generation,
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _segments(payload: bytes) -> Iterable[tuple[int, int, bytes]]:
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


def _block_value(block: Any) -> Any:
    return (
        block.model_dump(mode="json")
        if hasattr(block, "model_dump")
        else block
    )


def _block_kind(block: Any) -> str:
    value = getattr(block, "type", None)
    return str(getattr(value, "value", value) or "unknown")


def _search_text(block: Any) -> str:
    text = getattr(block, "text", None)
    if isinstance(text, str):
        return text
    data = getattr(block, "data", None)
    if isinstance(data, Mapping):
        return "\n".join(
            str(data[key])
            for key in ("name", "arguments", "output")
            if data.get(key) is not None
        )
    return ""


def _existing_acceptance(
    connection,
    request: AcceptanceRequest,
) -> AcceptanceResult | None:
    run = connection.execute(
        "SELECT r.run_id, r.turn_id, m.source_generation "
        "FROM chat_runs AS r CROSS JOIN chat_meta AS m "
        "WHERE r.client_request_id = ?",
        (request.client_request_id,),
    ).fetchone()
    if run is not None:
        return AcceptanceResult(
            kind="run",
            work_id=str(run["run_id"]),
            client_request_id=request.client_request_id,
            turn_id=str(run["turn_id"]),
            source_generation=int(run["source_generation"]),
            replayed=True,
        )
    queued = connection.execute(
        "SELECT q.queue_item_id, q.turn_id, m.source_generation "
        "FROM chat_queue_items AS q CROSS JOIN chat_meta AS m "
        "WHERE q.client_request_id = ?",
        (request.client_request_id,),
    ).fetchone()
    if queued is None:
        return None
    return AcceptanceResult(
        kind="queue",
        work_id=str(queued["queue_item_id"]),
        client_request_id=request.client_request_id,
        turn_id=str(queued["turn_id"]),
        source_generation=int(queued["source_generation"]),
        replayed=True,
    )


def _insert_user_message(
    connection,
    *,
    turn_id: str,
    message: Message,
    recall_sequences: Iterable[int],
    now_us: int,
) -> None:
    message_id = str(uuid4())
    envelope = message.model_dump(
        mode="json",
        exclude={"content"},
        exclude_none=False,
    )
    connection.execute(
        "INSERT INTO chat_messages("
        "message_id, turn_id, role, ordinal, state, metadata_json, "
        "created_at_us, updated_at_us) "
        "VALUES (?, ?, 'user', 0, 'completed', ?, ?, ?)",
        (message_id, turn_id, _canonical_json(envelope), now_us, now_us),
    )
    sequence_iterator = iter(recall_sequences)
    for ordinal, block in enumerate(message.content):
        block_id = str(uuid4())
        payload = _canonical_json(_block_value(block)).encode("utf-8")
        try:
            recall_sequence = next(sequence_iterator)
        except StopIteration as exc:
            raise ValueError(
                "A recall sequence is required for every input block",
            ) from exc
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
        connection.execute(
            "INSERT INTO chat_blocks_fts(block_id, content) VALUES (?, ?)",
            (block_id, _search_text(block)),
        )


def _insert_outbox(
    connection,
    *,
    conversation_id: str,
    generation: int,
    operation: str,
    projections: tuple[str, ...],
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
            for projection in projections
        ),
    )


def accept_input(
    path: Path,
    request: AcceptanceRequest,
    *,
    recall_sequences: Iterable[int],
) -> AcceptanceResult:
    """Commit a user turn and run, or a durable Chat-local queue item."""
    if request.message.role != Role.USER:
        raise ValueError("Accepted input message must have the user role")
    with connect_database(path) as connection:
        with immediate_transaction(connection):
            existing = _existing_acceptance(connection, request)
            if existing is not None:
                return existing
            meta = connection.execute(
                "SELECT head_turn_id, source_generation, write_seq "
                "FROM chat_meta "
                "WHERE singleton_id = 1",
            ).fetchone()
            if meta is None:
                raise LookupError("Chat metadata is missing")
            generation = int(meta["source_generation"]) + 1
            write_seq = int(meta["write_seq"]) + 1
            active = connection.execute(
                "SELECT 1 FROM chat_runs WHERE state IN "
                "('accepted', 'running', 'stopping')",
            ).fetchone()
            if active is not None:
                queue_id = str(uuid4())
                queue_ordinal = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(queue_ordinal), -1) + 1 "
                        "FROM chat_queue_items",
                    ).fetchone()[0],
                )
                parent_turn_id = meta["head_turn_id"]
                depth = 0
                if parent_turn_id is not None:
                    depth = (
                        int(
                            connection.execute(
                                "SELECT lineage_depth FROM chat_turns "
                                "WHERE turn_id = ?",
                                (parent_turn_id,),
                            ).fetchone()[0],
                        )
                        + 1
                    )
                turn_id = str(uuid4())
                connection.execute(
                    "INSERT INTO chat_turns("
                    "turn_id, parent_turn_id, client_request_id, state, "
                    "lineage_depth, write_seq, "
                    "created_at_us, updated_at_us) "
                    "VALUES (?, ?, ?, 'accepted', ?, ?, ?, ?)",
                    (
                        turn_id,
                        parent_turn_id,
                        request.client_request_id,
                        depth,
                        write_seq,
                        request.now_us,
                        request.now_us,
                    ),
                )
                _insert_user_message(
                    connection,
                    turn_id=turn_id,
                    message=request.message,
                    recall_sequences=recall_sequences,
                    now_us=request.now_us,
                )
                connection.execute(
                    "INSERT INTO chat_queue_items("
                    "queue_item_id, turn_id, client_request_id, "
                    "queue_ordinal, input_json, state, created_at_us, "
                    "updated_at_us) VALUES "
                    "(?, ?, ?, ?, ?, 'pending', ?, ?)",
                    (
                        queue_id,
                        turn_id,
                        request.client_request_id,
                        queue_ordinal,
                        request.message.model_dump_json(),
                        request.now_us,
                        request.now_us,
                    ),
                )
                connection.execute(
                    "UPDATE chat_meta SET head_turn_id = ?, updated_at_us = ? "
                    "WHERE singleton_id = 1",
                    (turn_id, request.now_us),
                )
                connection.execute(
                    "UPDATE chat_meta SET source_generation = ?, "
                    "write_seq = ?, updated_at_us = ? WHERE singleton_id = 1",
                    (generation, write_seq, request.now_us),
                )
                _insert_outbox(
                    connection,
                    conversation_id=request.conversation_id,
                    generation=generation,
                    operation="queue_accepted",
                    projections=("catalog",),
                    now_us=request.now_us,
                )
                return AcceptanceResult(
                    kind="queue",
                    work_id=queue_id,
                    client_request_id=request.client_request_id,
                    turn_id=turn_id,
                    source_generation=generation,
                    replayed=False,
                )
            parent_turn_id = meta["head_turn_id"]
            depth = 0
            if parent_turn_id is not None:
                depth = (
                    int(
                        connection.execute(
                            "SELECT lineage_depth FROM chat_turns "
                            "WHERE turn_id = ?",
                            (parent_turn_id,),
                        ).fetchone()[0],
                    )
                    + 1
                )
            turn_id = str(uuid4())
            run_id = str(uuid4())
            fencing_token = int(
                connection.execute(
                    "SELECT COALESCE(MAX(fencing_token), 0) + 1 "
                    "FROM chat_runs",
                ).fetchone()[0],
            )
            connection.execute(
                "INSERT INTO chat_turns("
                "turn_id, parent_turn_id, client_request_id, state, "
                "lineage_depth, write_seq, "
                "created_at_us, updated_at_us) "
                "VALUES (?, ?, ?, 'accepted', ?, ?, ?, ?)",
                (
                    turn_id,
                    parent_turn_id,
                    request.client_request_id,
                    depth,
                    write_seq,
                    request.now_us,
                    request.now_us,
                ),
            )
            _insert_user_message(
                connection,
                turn_id=turn_id,
                message=request.message,
                recall_sequences=recall_sequences,
                now_us=request.now_us,
            )
            connection.execute(
                "INSERT INTO chat_runs("
                "run_id, turn_id, client_request_id, state, "
                "process_instance_id, fencing_token, created_at_us, "
                "updated_at_us) VALUES (?, ?, ?, 'accepted', ?, ?, ?, ?)",
                (
                    run_id,
                    turn_id,
                    request.client_request_id,
                    request.process_instance_id,
                    fencing_token,
                    request.now_us,
                    request.now_us,
                ),
            )
            connection.execute(
                "UPDATE chat_meta SET head_turn_id = ?, updated_at_us = ? "
                "WHERE singleton_id = 1",
                (turn_id, request.now_us),
            )
            connection.execute(
                "UPDATE chat_meta SET source_generation = ?, write_seq = ?, "
                "updated_at_us = ? WHERE singleton_id = 1",
                (generation, write_seq, request.now_us),
            )
            _insert_outbox(
                connection,
                conversation_id=request.conversation_id,
                generation=generation,
                operation="input_accepted",
                projections=("catalog", "recall"),
                now_us=request.now_us,
            )
            return AcceptanceResult(
                kind="run",
                work_id=run_id,
                client_request_id=request.client_request_id,
                turn_id=turn_id,
                source_generation=generation,
                replayed=False,
            )


def claim_queued_input(
    path: Path,
    *,
    conversation_id: str,
    queue_item_id: str,
    process_instance_id: str,
    now_us: int,
) -> AcceptanceResult | None:
    """Promote the oldest pending Chat input when its predecessor ends."""
    with connect_database(path) as connection:
        with immediate_transaction(connection):
            queued = connection.execute(
                "SELECT turn_id, client_request_id, "
                "queue_ordinal, state FROM chat_queue_items "
                "WHERE queue_item_id = ?",
                (queue_item_id,),
            ).fetchone()
            if queued is None:
                raise LookupError(f"Queue item not found: {queue_item_id}")
            existing = connection.execute(
                "SELECT run_id, turn_id FROM chat_runs "
                "WHERE client_request_id = ?",
                (queued["client_request_id"],),
            ).fetchone()
            meta = connection.execute(
                "SELECT source_generation, write_seq FROM chat_meta "
                "WHERE singleton_id = 1",
            ).fetchone()
            if existing is not None:
                return AcceptanceResult(
                    kind="run",
                    work_id=str(existing["run_id"]),
                    client_request_id=str(queued["client_request_id"]),
                    turn_id=str(existing["turn_id"]),
                    source_generation=int(meta["source_generation"]),
                    replayed=True,
                )
            if str(queued["state"]) != "pending":
                return None
            active = connection.execute(
                "SELECT 1 FROM chat_runs WHERE state IN "
                "('accepted', 'running', 'stopping')",
            ).fetchone()
            oldest = connection.execute(
                "SELECT queue_item_id FROM chat_queue_items "
                "WHERE state = 'pending' "
                "ORDER BY queue_ordinal LIMIT 1",
            ).fetchone()
            if (
                active is not None
                or oldest is None
                or str(oldest["queue_item_id"]) != queue_item_id
            ):
                return None
            fencing_token = int(
                connection.execute(
                    "SELECT COALESCE(MAX(fencing_token), 0) + 1 "
                    "FROM chat_runs",
                ).fetchone()[0],
            )
            run_id = str(uuid4())
            connection.execute(
                "INSERT INTO chat_runs("
                "run_id, turn_id, client_request_id, state, "
                "process_instance_id, fencing_token, created_at_us, "
                "updated_at_us) VALUES "
                "(?, ?, ?, 'accepted', ?, ?, ?, ?)",
                (
                    run_id,
                    queued["turn_id"],
                    queued["client_request_id"],
                    process_instance_id,
                    fencing_token,
                    now_us,
                    now_us,
                ),
            )
            connection.execute(
                "UPDATE chat_queue_items SET state = 'completed', "
                "updated_at_us = ? WHERE queue_item_id = ?",
                (now_us, queue_item_id),
            )
            generation = int(meta["source_generation"]) + 1
            write_seq = int(meta["write_seq"]) + 1
            connection.execute(
                "UPDATE chat_meta SET source_generation = ?, write_seq = ?, "
                "updated_at_us = ? WHERE singleton_id = 1",
                (generation, write_seq, now_us),
            )
            _insert_outbox(
                connection,
                conversation_id=conversation_id,
                generation=generation,
                operation="queue_claimed",
                projections=("catalog",),
                now_us=now_us,
            )
            return AcceptanceResult(
                kind="run",
                work_id=run_id,
                client_request_id=str(queued["client_request_id"]),
                turn_id=str(queued["turn_id"]),
                source_generation=generation,
                replayed=False,
            )


def cancel_accepted_input(
    path: Path,
    *,
    conversation_id: str,
    accepted: AcceptanceResult,
    process_instance_id: str,
    now_us: int,
) -> int | None:
    """Cancel accepted work before its runtime starts consuming events."""
    with connect_database(path) as connection:
        with immediate_transaction(connection):
            changed = False
            turn_id = accepted.turn_id
            operation = "input_canceled"
            if accepted.kind == "queue":
                queued = connection.execute(
                    "SELECT turn_id, state FROM chat_queue_items "
                    "WHERE queue_item_id = ? AND client_request_id = ?",
                    (
                        accepted.work_id,
                        accepted.client_request_id,
                    ),
                ).fetchone()
                if queued is None:
                    return None
                turn_id = str(queued["turn_id"])
                operation = "queue_canceled"
            else:
                queued = None
            if queued is not None and str(queued["state"]) in {
                "pending",
                "starting",
            }:
                cursor = connection.execute(
                    "UPDATE chat_queue_items SET state = 'canceled', "
                    "updated_at_us = ? WHERE queue_item_id = ? "
                    "AND state IN ('pending', 'starting')",
                    (now_us, accepted.work_id),
                )
                changed = cursor.rowcount > 0
            elif accepted.kind == "run" or (
                queued is not None and str(queued["state"]) == "completed"
            ):
                cursor = connection.execute(
                    "UPDATE chat_runs SET state = 'canceled', "
                    "updated_at_us = ?, terminal_at_us = ? "
                    "WHERE client_request_id = ? "
                    "AND process_instance_id = ? AND state = 'accepted'",
                    (
                        now_us,
                        now_us,
                        accepted.client_request_id,
                        process_instance_id,
                    ),
                )
                changed = cursor.rowcount > 0
            if not changed or turn_id is None:
                return None
            connection.execute(
                "UPDATE chat_turns SET state = 'canceled', "
                "updated_at_us = ?, terminal_at_us = ? "
                "WHERE turn_id = ? AND state = 'accepted'",
                (now_us, now_us, turn_id),
            )
            meta = connection.execute(
                "SELECT source_generation, write_seq FROM chat_meta "
                "WHERE singleton_id = 1",
            ).fetchone()
            generation = int(meta["source_generation"]) + 1
            write_seq = int(meta["write_seq"]) + 1
            connection.execute(
                "UPDATE chat_meta SET source_generation = ?, write_seq = ?, "
                "updated_at_us = ? WHERE singleton_id = 1",
                (generation, write_seq, now_us),
            )
            _insert_outbox(
                connection,
                conversation_id=conversation_id,
                generation=generation,
                operation=operation,
                projections=("catalog", "recall"),
                now_us=now_us,
            )
            return generation


def interrupt_nonterminal_work(
    path: Path,
    *,
    conversation_id: str,
    now_us: int,
) -> RestartRecoveryResult:
    """Mark persisted work interrupted without resuming external execution."""
    with connect_database(path) as connection:
        with immediate_transaction(connection):
            meta = connection.execute(
                "SELECT source_generation, write_seq "
                "FROM chat_meta WHERE singleton_id = 1",
            ).fetchone()
            updates = (
                (
                    "chat_runs",
                    "state IN ('accepted', 'running', 'stopping')",
                ),
                ("chat_turns", "state IN ('accepted', 'running')"),
                ("chat_messages", "state = 'partial'"),
                ("chat_blocks", "state = 'partial'"),
                ("chat_tool_links", "state = 'running'"),
                (
                    "chat_queue_items",
                    "state IN ('pending', 'starting')",
                ),
            )
            counts: list[int] = []
            for table, predicate in updates:
                terminal_column = (
                    ", terminal_at_us = ?"
                    if table in {"chat_runs", "chat_turns"}
                    else ""
                )
                parameters = (now_us, now_us) if terminal_column else (now_us,)
                cursor = connection.execute(
                    f"UPDATE {table} SET state = 'interrupted', "
                    f"updated_at_us = ?{terminal_column} WHERE {predicate}",
                    parameters,
                )
                counts.append(cursor.rowcount)
            if not any(counts):
                return _recovery_result(
                    counts,
                    int(meta["source_generation"]),
                )
            generation = int(meta["source_generation"]) + 1
            write_seq = int(meta["write_seq"]) + 1
            connection.execute(
                "UPDATE chat_meta SET source_generation = ?, write_seq = ?, "
                "updated_at_us = ? WHERE singleton_id = 1",
                (generation, write_seq, now_us),
            )
            _insert_outbox(
                connection,
                conversation_id=conversation_id,
                generation=generation,
                operation="restart_interrupted",
                projections=("catalog", "recall"),
                now_us=now_us,
            )
            return _recovery_result(counts, generation)
