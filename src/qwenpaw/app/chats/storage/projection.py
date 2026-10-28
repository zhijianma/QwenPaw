# -*- coding: utf-8 -*-
"""Idempotent catalog and recall projections from Chat source databases."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .common import connect_database, immediate_transaction
from .conversation import validate_conversation_database
from .paths import ChatStoragePaths
from .recall import initialize_recall_database


class ChatProjectionError(RuntimeError):
    """A derived projection could not safely represent source state."""


@dataclass(frozen=True)
class ProjectionResult:
    """Applied source generation and projected recall row count."""

    conversation_id: str
    source_generation: int
    recall_documents: int


@dataclass(frozen=True)
class _RecallDocument:
    recall_seq: int
    conversation_id: str
    turn_id: str
    message_id: str
    block_id: str
    block_kind: str
    content: str
    metadata_json: str
    source_generation: int
    created_at_us: int
    updated_at_us: int
    session_id: str
    agent_id: str
    role: str
    message_type: str
    block_json: str
    dedup_key: str


def _error_digest(exc: BaseException) -> str:
    payload = f"{type(exc).__name__}:{exc}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _searchable_content(payload: bytes) -> str:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return payload.decode("utf-8", errors="replace")
    if not isinstance(value, dict):
        return str(value)
    text = value.get("text")
    if isinstance(text, str):
        return text
    data = value.get("data")
    if isinstance(data, dict):
        selected = [
            str(data[key])
            for key in ("name", "arguments", "output")
            if data.get(key) is not None
        ]
        if selected:
            return "\n".join(selected)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _source_projection(
    path: Path,
    conversation_id: str,
) -> tuple[
    int,
    str | None,
    str | None,
    list[tuple[str, str, str]],
    list[_RecallDocument],
]:
    validate_conversation_database(
        path,
        expected_conversation_id=conversation_id,
    )
    with connect_database(path, readonly=True) as connection:
        meta = connection.execute(
            "SELECT head_turn_id, source_generation, agent_id "
            "FROM chat_meta WHERE singleton_id = 1",
        ).fetchone()
        if meta is None:
            raise ChatProjectionError(
                f"Chat metadata is missing: {conversation_id}",
            )
        source_generation = int(meta["source_generation"])
        agent_id = str(meta["agent_id"])
        if source_generation <= 0:
            raise ChatProjectionError(
                "Chat source generation is not publishable: "
                f"{conversation_id}",
            )
        run = connection.execute(
            "SELECT run_id, state FROM chat_runs WHERE state IN "
            "('accepted', 'running', 'stopping') "
            "ORDER BY updated_at_us DESC LIMIT 1",
        ).fetchone()
        aliases = [
            (
                str(row["alias_namespace"]),
                str(row["external_key"]),
                str(row["owner_scope"]),
            )
            for row in connection.execute(
                "SELECT alias_namespace, external_key, owner_scope "
                "FROM chat_aliases",
            )
        ]
        rows = connection.execute(
            "WITH RECURSIVE lineage(turn_id, parent_turn_id) AS ("
            "SELECT turn_id, parent_turn_id FROM chat_turns "
            "WHERE turn_id = ? UNION ALL "
            "SELECT p.turn_id, p.parent_turn_id FROM chat_turns AS p "
            "JOIN lineage AS l ON l.parent_turn_id = p.turn_id) "
            "SELECT t.turn_id, m.message_id, b.block_id, b.recall_seq, "
            "b.kind, b.ordinal AS block_ordinal, b.metadata_json, "
            "b.created_at_us, b.updated_at_us, "
            "m.role, m.metadata_json AS message_metadata_json, "
            "s.segment_ordinal, s.payload FROM lineage AS l "
            "JOIN chat_turns AS t ON t.turn_id = l.turn_id "
            "JOIN chat_messages AS m ON m.turn_id = t.turn_id "
            "JOIN chat_blocks AS b ON b.message_id = m.message_id "
            "JOIN chat_block_segments AS s ON s.block_id = b.block_id "
            "WHERE b.recall_seq IS NOT NULL "
            "ORDER BY b.recall_seq, s.segment_ordinal",
            (meta["head_turn_id"],),
        ).fetchall()
    grouped: dict[str, tuple[Any, list[bytes]]] = {}
    for row in rows:
        block_id = str(row["block_id"])
        if block_id not in grouped:
            grouped[block_id] = (row, [])
        grouped[block_id][1].append(bytes(row["payload"]))
    session_id = next(
        (
            external_key
            for namespace, external_key, _owner_scope in aliases
            if namespace == "legacy_session"
        ),
        conversation_id,
    )
    documents = []
    for row, segments in grouped.values():
        payload = b"".join(segments)
        try:
            block_value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            block_value = {
                "type": str(row["kind"]),
                "text": payload.decode("utf-8", errors="replace"),
            }
        try:
            message_metadata = json.loads(str(row["message_metadata_json"]))
        except json.JSONDecodeError:
            message_metadata = {}
        message_type = str(message_metadata.get("type") or "message")
        block_data = (
            block_value.get("data") if isinstance(block_value, dict) else None
        )
        call_id = (
            block_data.get("call_id") if isinstance(block_data, dict) else None
        )
        source_message_id = str(
            message_metadata.get("id") or row["message_id"],
        )
        block_ordinal = int(row["block_ordinal"])
        documents.append(
            _RecallDocument(
                recall_seq=int(row["recall_seq"]),
                conversation_id=conversation_id,
                turn_id=str(row["turn_id"]),
                message_id=str(row["message_id"]),
                block_id=str(row["block_id"]),
                block_kind=str(row["kind"]),
                content=_searchable_content(payload),
                metadata_json=str(row["metadata_json"]),
                source_generation=source_generation,
                created_at_us=int(row["created_at_us"]),
                updated_at_us=int(row["updated_at_us"]),
                session_id=session_id,
                agent_id=agent_id,
                role=str(row["role"]),
                message_type=message_type,
                block_json=json.dumps(
                    block_value,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                dedup_key=(
                    str(call_id)
                    if message_type.endswith("_output") and call_id
                    else source_message_id
                    if block_ordinal == 0
                    else f"{source_message_id}#block{block_ordinal}"
                ),
            ),
        )
    return (
        source_generation,
        str(run["run_id"]) if run else None,
        str(run["state"]) if run else None,
        aliases,
        documents,
    )


def _project_catalog(
    paths: ChatStoragePaths,
    conversation_id: str,
    source_generation: int,
    active_run_id: str | None,
    run_state: str | None,
    aliases: list[tuple[str, str, str]],
    *,
    now_us: int,
) -> None:
    with connect_database(paths.catalog) as connection:
        with immediate_transaction(connection):
            entry = connection.execute(
                "SELECT source_generation FROM chat_catalog_entries "
                "WHERE conversation_id = ? AND lifecycle = 'ready'",
                (conversation_id,),
            ).fetchone()
            if entry is None:
                raise ChatProjectionError(
                    f"Ready catalog entry is missing: {conversation_id}",
                )
            if int(entry["source_generation"]) <= source_generation:
                connection.execute(
                    "UPDATE chat_catalog_entries SET active_run_id = ?, "
                    "run_state = ?, "
                    "source_generation = ?, "
                    "updated_at_us = ? WHERE conversation_id = ?",
                    (
                        active_run_id,
                        run_state,
                        source_generation,
                        now_us,
                        conversation_id,
                    ),
                )
            for namespace, external_key, owner_scope in aliases:
                conflict = connection.execute(
                    "SELECT conversation_id FROM chat_alias_index "
                    "WHERE agent_id = (SELECT agent_id "
                    "FROM chat_catalog_entries WHERE conversation_id = ?) "
                    "AND alias_namespace = ? AND external_key = ? "
                    "AND owner_scope = ?",
                    (
                        conversation_id,
                        namespace,
                        external_key,
                        owner_scope,
                    ),
                ).fetchone()
                if (
                    conflict is not None
                    and str(conflict["conversation_id"]) != conversation_id
                ):
                    raise ChatProjectionError(
                        f"Alias belongs to another Chat: {namespace}",
                    )
                connection.execute(
                    "INSERT INTO chat_alias_index("
                    "agent_id, alias_namespace, external_key, owner_scope, "
                    "conversation_id, source_generation, "
                    "created_at_us, updated_at_us) "
                    "SELECT agent_id, ?, ?, ?, conversation_id, ?, ?, ? "
                    "FROM chat_catalog_entries WHERE conversation_id = ? "
                    "ON CONFLICT(agent_id, alias_namespace, external_key, "
                    "owner_scope) DO UPDATE SET "
                    "source_generation = excluded.source_generation, "
                    "updated_at_us = excluded.updated_at_us",
                    (
                        namespace,
                        external_key,
                        owner_scope,
                        source_generation,
                        now_us,
                        now_us,
                        conversation_id,
                    ),
                )


def _project_recall(
    paths: ChatStoragePaths,
    conversation_id: str,
    source_generation: int,
    documents: list[_RecallDocument],
    *,
    now_us: int,
) -> None:
    initialize_recall_database(paths.recall)
    with connect_database(paths.recall) as connection:
        with immediate_transaction(connection):
            previous = connection.execute(
                "SELECT row_id, content FROM chat_recall_documents "
                "WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchall()
            for row in previous:
                connection.execute(
                    "INSERT INTO chat_recall_fts("
                    "chat_recall_fts, rowid, content) "
                    "VALUES ('delete', ?, ?)",
                    (row["row_id"], row["content"]),
                )
            connection.execute(
                "DELETE FROM chat_recall_documents WHERE conversation_id = ?",
                (conversation_id,),
            )
            compatibility = connection.execute(
                "SELECT seq, content FROM conversation_history "
                "WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchall()
            for row in compatibility:
                connection.execute(
                    "INSERT INTO conversation_history_fts("
                    "conversation_history_fts, rowid, content) "
                    "VALUES ('delete', ?, ?)",
                    (row["seq"], row["content"] or ""),
                )
            connection.execute(
                "DELETE FROM conversation_history WHERE conversation_id = ?",
                (conversation_id,),
            )
            for document in documents:
                cursor = connection.execute(
                    "INSERT INTO chat_recall_documents("
                    "recall_seq, conversation_id, turn_id, "
                    "message_id, block_id, block_kind, content, "
                    "metadata_json, source_generation, created_at_us, "
                    "updated_at_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "?)",
                    (
                        document.recall_seq,
                        document.conversation_id,
                        document.turn_id,
                        document.message_id,
                        document.dedup_key,
                        document.block_kind,
                        document.content,
                        document.metadata_json,
                        document.source_generation,
                        document.created_at_us,
                        document.updated_at_us,
                    ),
                )
                connection.execute(
                    "INSERT INTO chat_recall_fts(rowid, content) "
                    "VALUES (?, ?)",
                    (cursor.lastrowid, document.content),
                )
                block = json.loads(document.block_json)
                data = block.get("data") if isinstance(block, dict) else None
                call_id = (
                    data.get("call_id") if isinstance(data, dict) else None
                )
                tool_input = (
                    data.get("arguments") if isinstance(data, dict) else None
                )
                tool_state = (
                    data.get("state") if isinstance(data, dict) else None
                )
                kind = (
                    "context_msg"
                    if document.role == "user"
                    else (
                        "tool_result"
                        if document.message_type.endswith("_output")
                        else "model_turn"
                    )
                )
                created_at = datetime.fromtimestamp(
                    document.created_at_us / 1_000_000,
                    tz=timezone.utc,
                ).isoformat()
                connection.execute(
                    "INSERT INTO conversation_history("
                    "seq, session_id, agent_id, kind, role, name, content, "
                    "tool_call_id, tool_input, tool_state, blocks, metadata, "
                    "created_at, dedup_key, conversation_id, "
                    "turn_id, message_id, block_id, provisional) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "?, 0)",
                    (
                        document.recall_seq,
                        document.session_id,
                        document.agent_id,
                        kind,
                        document.role,
                        document.role,
                        document.content,
                        str(call_id) if call_id is not None else None,
                        str(tool_input) if tool_input is not None else None,
                        str(tool_state) if tool_state is not None else None,
                        f"[{document.block_json}]",
                        document.metadata_json,
                        created_at,
                        document.dedup_key,
                        document.conversation_id,
                        document.turn_id,
                        document.message_id,
                        document.block_id,
                    ),
                )
                connection.execute(
                    "INSERT INTO conversation_history_fts(rowid, content) "
                    "VALUES (?, ?)",
                    (document.recall_seq, document.content),
                )
            projected_sequence = max(
                (document.recall_seq for document in documents),
                default=0,
            )
            connection.execute(
                "INSERT INTO chat_recall_projection_state("
                "conversation_id, source_generation, "
                "projected_recall_seq, state, updated_at_us) "
                "VALUES (?, ?, ?, 'ready', ?) "
                "ON CONFLICT(conversation_id) DO UPDATE SET "
                "source_generation = excluded.source_generation, "
                "projected_recall_seq = excluded.projected_recall_seq, "
                "state = 'ready', error_digest = NULL, "
                "updated_at_us = excluded.updated_at_us",
                (
                    conversation_id,
                    source_generation,
                    projected_sequence,
                    now_us,
                ),
            )


def _mark_outbox_delivered(
    path: Path,
    *,
    source_generation: int,
    now_us: int,
) -> None:
    with connect_database(path) as connection:
        with immediate_transaction(connection):
            connection.execute(
                "UPDATE chat_projection_outbox SET state = 'delivered', "
                "updated_at_us = ? WHERE source_generation <= ?",
                (now_us, source_generation),
            )


def _complete_catalog_repairs(
    paths: ChatStoragePaths,
    conversation_id: str,
    *,
    source_generation: int,
    now_us: int,
) -> None:
    with connect_database(paths.catalog) as connection:
        with immediate_transaction(connection):
            connection.execute(
                "UPDATE chat_catalog_repairs SET state = 'completed', "
                "updated_at_us = ? WHERE conversation_id = ? "
                "AND state IN ('pending', 'running') "
                "AND expected_generation <= ?",
                (now_us, conversation_id, source_generation),
            )


def _record_recall_failure(
    paths: ChatStoragePaths,
    conversation_id: str,
    source_generation: int,
    exc: BaseException,
    *,
    now_us: int,
) -> None:
    initialize_recall_database(paths.recall)
    with connect_database(paths.recall) as connection:
        with immediate_transaction(connection):
            connection.execute(
                "INSERT INTO chat_recall_projection_state("
                "conversation_id, source_generation, "
                "state, error_digest, updated_at_us) "
                "VALUES (?, ?, 'failed', ?, ?) "
                "ON CONFLICT(conversation_id) DO UPDATE SET "
                "state = 'failed', error_digest = excluded.error_digest, "
                "updated_at_us = excluded.updated_at_us",
                (
                    conversation_id,
                    source_generation,
                    _error_digest(exc),
                    now_us,
                ),
            )


def project_conversation(
    paths: ChatStoragePaths,
    conversation_id: str,
    *,
    now_us: int,
) -> ProjectionResult:
    """Idempotently repair catalog and recall projections."""
    path = paths.conversation(conversation_id)
    (
        generation,
        run_id,
        run_state,
        aliases,
        documents,
    ) = _source_projection(path, conversation_id)
    _project_catalog(
        paths,
        conversation_id,
        generation,
        run_id,
        run_state,
        aliases,
        now_us=now_us,
    )
    try:
        _project_recall(
            paths,
            conversation_id,
            generation,
            documents,
            now_us=now_us,
        )
    except BaseException as exc:
        _record_recall_failure(
            paths,
            conversation_id,
            generation,
            exc,
            now_us=now_us,
        )
        raise
    _mark_outbox_delivered(
        path,
        source_generation=generation,
        now_us=now_us,
    )
    _complete_catalog_repairs(
        paths,
        conversation_id,
        source_generation=generation,
        now_us=now_us,
    )
    return ProjectionResult(
        conversation_id=conversation_id,
        source_generation=generation,
        recall_documents=len(documents),
    )


def enqueue_projection_repair(
    paths: ChatStoragePaths,
    conversation_id: str,
    *,
    expected_generation: int,
    now_us: int,
) -> None:
    """Persist an idempotent retry after a derived projection failure."""
    with connect_database(paths.catalog) as connection:
        with immediate_transaction(connection):
            connection.execute(
                "INSERT INTO chat_catalog_repairs("
                "conversation_id, kind, expected_generation, state, "
                "created_at_us, updated_at_us) VALUES "
                "(?, 'source_projection', ?, 'pending', ?, ?) "
                "ON CONFLICT(conversation_id, kind, expected_generation) "
                "DO UPDATE SET state = 'pending', "
                "updated_at_us = excluded.updated_at_us",
                (
                    conversation_id,
                    expected_generation,
                    now_us,
                    now_us,
                ),
            )


def repair_pending_projections(
    paths: ChatStoragePaths,
    *,
    now_us: int,
    limit: int = 32,
) -> tuple[ProjectionResult, ...]:
    """Retry a bounded batch of durable projection repair jobs."""
    if limit < 1:
        raise ValueError("Projection repair limit must be positive")
    with connect_database(paths.catalog, readonly=True) as connection:
        conversation_ids = [
            str(row["conversation_id"])
            for row in connection.execute(
                "SELECT DISTINCT conversation_id "
                "FROM chat_catalog_repairs WHERE state = 'pending' "
                "ORDER BY updated_at_us, conversation_id LIMIT ?",
                (limit,),
            )
        ]
    results: list[ProjectionResult] = []
    for conversation_id in conversation_ids:
        with connect_database(paths.catalog) as connection:
            with immediate_transaction(connection):
                connection.execute(
                    "UPDATE chat_catalog_repairs SET state = 'running', "
                    "attempt = attempt + 1, updated_at_us = ? "
                    "WHERE conversation_id = ? AND state = 'pending'",
                    (now_us, conversation_id),
                )
        try:
            result = project_conversation(
                paths,
                conversation_id,
                now_us=now_us,
            )
        except Exception:
            with connect_database(paths.catalog) as connection:
                with immediate_transaction(connection):
                    connection.execute(
                        "UPDATE chat_catalog_repairs SET state = 'pending', "
                        "updated_at_us = ? WHERE conversation_id = ? "
                        "AND state = 'running'",
                        (now_us, conversation_id),
                    )
            continue
        with connect_database(paths.catalog) as connection:
            with immediate_transaction(connection):
                connection.execute(
                    "UPDATE chat_catalog_repairs SET state = 'completed', "
                    "updated_at_us = ? WHERE conversation_id = ? "
                    "AND expected_generation <= ?",
                    (
                        now_us,
                        conversation_id,
                        result.source_generation,
                    ),
                )
        results.append(result)
    return tuple(results)


def rebuild_all_recall_projections(
    paths: ChatStoragePaths,
    *,
    now_us: int,
) -> tuple[ProjectionResult, ...]:
    """Rebuild every ready Chat projection from authoritative source DBs."""
    with connect_database(paths.catalog, readonly=True) as connection:
        conversation_ids = [
            str(row["conversation_id"])
            for row in connection.execute(
                "SELECT conversation_id FROM chat_catalog_entries "
                "WHERE lifecycle = 'ready' ORDER BY conversation_id",
            )
        ]
    return tuple(
        project_conversation(paths, conversation_id, now_us=now_us)
        for conversation_id in conversation_ids
    )
