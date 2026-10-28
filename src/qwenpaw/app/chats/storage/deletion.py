# -*- coding: utf-8 -*-
"""Recoverable deletion saga for normalized Chat storage."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .common import connect_database, immediate_transaction
from .paths import ChatStoragePaths
from .recall import initialize_recall_database
from .runtime_repository import interrupt_nonterminal_work


DELETE_RUNTIME_STOPPED = 1 << 0
DELETE_CHECKPOINTS = 1 << 1
DELETE_LEGACY = 1 << 2
DELETE_PROJECTIONS = 1 << 3
DELETE_DATABASE = 1 << 4
DELETE_TOMBSTONE = 1 << 5


@dataclass(frozen=True)
class ChatDeletionTarget:
    """Persisted identity required to resume an interrupted deletion."""

    conversation_id: str
    spec_json: str
    completed_steps: int
    state: str


def _target(
    connection: sqlite3.Connection,
    conversation_id: str,
) -> ChatDeletionTarget | None:
    row = connection.execute(
        "SELECT e.conversation_id, e.spec_json, j.completed_steps, j.state "
        "FROM chat_catalog_entries AS e LEFT JOIN chat_deletion_jobs AS j "
        "ON j.conversation_id = e.conversation_id "
        "WHERE e.conversation_id = ?",
        (conversation_id,),
    ).fetchone()
    if row is None:
        return None
    return ChatDeletionTarget(
        conversation_id=str(row["conversation_id"]),
        spec_json=str(row["spec_json"] or "{}"),
        completed_steps=int(row["completed_steps"] or 0),
        state=str(row["state"] or "pending"),
    )


def begin_conversation_deletion(
    paths: ChatStoragePaths,
    conversation_id: str,
    *,
    spec_json: str | None,
    now_us: int,
) -> ChatDeletionTarget | None:
    """Tombstone one Chat before any external or physical cleanup."""
    with connect_database(paths.catalog) as connection:
        with immediate_transaction(connection):
            existing = connection.execute(
                "SELECT lifecycle FROM chat_catalog_entries "
                "WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if existing is None:
                return None
            lifecycle = str(existing["lifecycle"])
            if lifecycle not in {"deleting", "deleted"}:
                connection.execute(
                    "UPDATE chat_catalog_entries SET lifecycle = "
                    "'deleting', spec_json = CASE WHEN ? IS NULL THEN "
                    "spec_json ELSE ? END, updated_at_us = ? "
                    "WHERE conversation_id = ?",
                    (spec_json, spec_json, now_us, conversation_id),
                )
            connection.execute(
                "INSERT INTO chat_deletion_jobs("
                "conversation_id, state, created_at_us, updated_at_us) "
                "VALUES (?, 'pending', ?, ?) "
                "ON CONFLICT(conversation_id) DO UPDATE SET "
                "state = CASE WHEN chat_deletion_jobs.state = 'completed' "
                "THEN 'completed' ELSE 'pending' END, "
                "updated_at_us = excluded.updated_at_us",
                (conversation_id, now_us, now_us),
            )
        return _target(connection, conversation_id)


def record_deletion_step(
    paths: ChatStoragePaths,
    conversation_id: str,
    step: int,
    *,
    now_us: int,
) -> ChatDeletionTarget:
    """Persist one idempotent saga step after its side effect succeeds."""
    with connect_database(paths.catalog) as connection:
        with immediate_transaction(connection):
            connection.execute(
                "UPDATE chat_deletion_jobs SET state = 'running', "
                "completed_steps = completed_steps | ?, "
                "attempt = attempt + 1, updated_at_us = ? "
                "WHERE conversation_id = ? AND state != 'completed'",
                (step, now_us, conversation_id),
            )
        target = _target(connection, conversation_id)
    if target is None:
        raise LookupError(conversation_id)
    return target


def list_pending_deletions(
    paths: ChatStoragePaths,
) -> tuple[ChatDeletionTarget, ...]:
    """Return durable deletion jobs that must resume after a crash."""
    with connect_database(paths.catalog, readonly=True) as connection:
        rows = connection.execute(
            "SELECT conversation_id FROM chat_deletion_jobs "
            "WHERE state != 'completed' ORDER BY created_at_us",
        ).fetchall()
        result: list[ChatDeletionTarget] = []
        for row in rows:
            target = _target(connection, str(row["conversation_id"]))
            if target is not None:
                result.append(target)
        return tuple(result)


def _delete_recall_projection(
    paths: ChatStoragePaths,
    conversation_id: str,
) -> None:
    initialize_recall_database(paths.recall)
    with connect_database(paths.recall) as connection:
        with immediate_transaction(connection):
            rows = connection.execute(
                "SELECT row_id, content FROM chat_recall_documents "
                "WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "INSERT INTO chat_recall_fts("
                    "chat_recall_fts, rowid, content) "
                    "VALUES ('delete', ?, ?)",
                    (row["row_id"], row["content"]),
                )
            connection.execute(
                "DELETE FROM chat_recall_documents "
                "WHERE conversation_id = ?",
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
                "DELETE FROM conversation_history "
                "WHERE conversation_id = ?",
                (conversation_id,),
            )
            connection.execute(
                "DELETE FROM chat_recall_projection_state "
                "WHERE conversation_id = ?",
                (conversation_id,),
            )


def _clean_catalog_projections(
    paths: ChatStoragePaths,
    conversation_id: str,
) -> None:
    with connect_database(paths.catalog) as connection:
        with immediate_transaction(connection):
            for table in (
                "chat_alias_index",
                "chat_runtime_intents",
                "chat_catalog_repairs",
            ):
                connection.execute(
                    f"DELETE FROM {table} WHERE conversation_id = ?",
                    (conversation_id,),
                )


def _unlink_conversation(
    paths: ChatStoragePaths,
    conversation_id: str,
) -> None:
    database = paths.conversation(conversation_id)
    for path in (
        Path(f"{database}-wal"),
        Path(f"{database}-shm"),
        Path(f"{database}-journal"),
        database,
    ):
        path.unlink(missing_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(database.parent, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _finish_tombstone(
    paths: ChatStoragePaths,
    conversation_id: str,
    *,
    now_us: int,
) -> None:
    with connect_database(paths.catalog) as connection:
        with immediate_transaction(connection):
            connection.execute(
                "UPDATE chat_migrations SET state = 'failed_terminal', "
                "progress_json = '{}', error_digest = 'user_deleted', "
                "lease_owner = NULL, lease_expires_at_us = NULL, "
                "updated_at_us = ? WHERE conversation_id = ?",
                (now_us, conversation_id),
            )
            connection.execute(
                "UPDATE chat_catalog_entries SET lifecycle = 'deleted', "
                "display_name = 'Deleted Chat', spec_json = '{}', "
                "active_run_id = NULL, run_state = NULL, updated_at_us = ? "
                "WHERE conversation_id = ?",
                (now_us, conversation_id),
            )
            connection.execute(
                "UPDATE chat_deletion_jobs SET state = 'completed', "
                "completed_steps = completed_steps | ?, "
                "lease_owner = NULL, lease_expires_at_us = NULL, "
                "last_error_digest = NULL, updated_at_us = ? "
                "WHERE conversation_id = ?",
                (DELETE_TOMBSTONE, now_us, conversation_id),
            )


def purge_conversation_storage(
    paths: ChatStoragePaths,
    conversation_id: str,
    *,
    now_us: int,
    after_step: Callable[[int], None] | None = None,
) -> None:
    """Finish normalized cleanup; every step is safe to replay."""
    database = paths.conversation(conversation_id)
    if database.is_file():
        interrupt_nonterminal_work(
            database,
            conversation_id=conversation_id,
            now_us=now_us,
        )
    _delete_recall_projection(paths, conversation_id)
    _clean_catalog_projections(paths, conversation_id)
    record_deletion_step(
        paths,
        conversation_id,
        DELETE_PROJECTIONS,
        now_us=now_us,
    )
    if after_step is not None:
        after_step(DELETE_PROJECTIONS)
    _unlink_conversation(paths, conversation_id)
    record_deletion_step(
        paths,
        conversation_id,
        DELETE_DATABASE,
        now_us=now_us,
    )
    if after_step is not None:
        after_step(DELETE_DATABASE)
    _finish_tombstone(paths, conversation_id, now_us=now_us)
    if after_step is not None:
        after_step(DELETE_TOMBSTONE)
