# -*- coding: utf-8 -*-
"""Durable, display-faithful chat transcript storage."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

from ...constant import QWENPAW_CLIENT_MESSAGE_ID_KEY
from ...schemas import Message, TextContent
from ...token_usage.turn_usage import TURN_USAGE_META_KEY
from .context_revisions import (
    commit_turn_revision,
    record_revision,
)
from .transcript_blocks import (
    copy_message_blocks,
    message_envelope,
    write_message_blocks,
)
from .fork_turn import fork_turn
from .transcript_schema import initialize_database, validate_database
from .transcript_models import (
    RuntimeSnapshot,
    TranscriptCursor,
    TranscriptPage,
    TranscriptUserMessage,
    TranscriptUserMessagePage,
)
from .transcript_paging import TranscriptPageBuilder

_BUSY_TIMEOUT_MS = 5000
TurnStatus = Literal["running", "completed", "failed", "cancelled"]
_DEFAULT_PAGE_MAX_BYTES = 2 * 1024 * 1024
_DEFAULT_PAGE_MAX_CONTENT_BLOCKS = 40


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _enum_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "")


# pylint: disable=too-many-public-methods
class TranscriptStore:
    """Workspace-owned SQLite store for user-visible chat transcripts."""

    def __init__(
        self,
        db_path: str | Path,
    ) -> None:
        self._path = Path(db_path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._conn = sqlite3.connect(
            str(self._path),
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        try:
            initialize_database(self._conn)
        except BaseException:
            self._conn.close()
            self._closed = True
            raise

    @property
    def path(self) -> Path:
        """Return the database path owned by this store."""
        return self._path

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        """Open a short-lived read-only connection for page reads."""
        uri = f"{self._path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA query_only=ON")
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _client_message_id(message: Message) -> str | None:
        metadata = message.metadata or {}
        value = metadata.get(QWENPAW_CLIENT_MESSAGE_ID_KEY)
        nested = metadata.get("metadata")
        if not value and isinstance(nested, dict):
            value = nested.get(QWENPAW_CLIENT_MESSAGE_ID_KEY)
        return str(value) if value else None

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._conn.rollback()
                raise
            self._conn.commit()

    def _session_row(
        self,
        session_id: str,
        connection: sqlite3.Connection | None = None,
    ) -> sqlite3.Row | None:
        database = connection or self._conn
        return database.execute(
            "SELECT * FROM transcript_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()

    @staticmethod
    def _assert_identity(
        row: sqlite3.Row,
        *,
        user_id: str,
        channel: str,
    ) -> None:
        if row["user_id"] != user_id or row["channel"] != channel:
            raise ValueError("transcript session identity mismatch")

    def has_session(
        self,
        *,
        session_id: str,
        user_id: str,
        channel: str,
    ) -> bool:
        """Return whether this database already owns the session."""
        with self._read_connection() as connection:
            session = self._session_row(session_id, connection)
            if session is None:
                return False
            self._assert_identity(
                session,
                user_id=user_id,
                channel=channel,
            )
            return True

    def read_runtime_state(
        self,
        *,
        session_id: str,
        user_id: str,
        channel: str,
    ) -> RuntimeSnapshot | None:
        """Read the current runtime snapshot without mutating the store."""
        with self._read_connection() as connection:
            session = self._session_row(session_id, connection)
            if session is None:
                return None
            self._assert_identity(
                session,
                user_id=user_id,
                channel=channel,
            )
            row = connection.execute(
                "SELECT state_json, context_generation, current_usage_json "
                "FROM session_runtime WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            return RuntimeSnapshot(
                state=json.loads(row["state_json"]),
                context_generation=int(row["context_generation"]),
                current_usage=(
                    json.loads(row["current_usage_json"])
                    if row["current_usage_json"]
                    else None
                ),
            )

    def read_current_usage(
        self,
        *,
        session_id: str,
        user_id: str,
        channel: str,
    ) -> tuple[int, dict[str, Any] | None] | None:
        """Read context usage without decoding the runtime state."""
        with self._read_connection() as connection:
            session = self._session_row(session_id, connection)
            if session is None:
                return None
            self._assert_identity(
                session,
                user_id=user_id,
                channel=channel,
            )
            row = connection.execute(
                "SELECT context_generation, current_usage_json "
                "FROM session_runtime WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            usage = (
                json.loads(row["current_usage_json"])
                if row["current_usage_json"]
                else None
            )
            return int(row["context_generation"]), usage

    def read_turn_context(
        self,
        *,
        session_id: str,
        user_id: str,
        channel: str,
        turn_id: str,
    ) -> RuntimeSnapshot | None:
        """Read the exact committed context; legacy turns have no revision."""
        with self._read_connection() as connection:
            session = self._session_row(session_id, connection)
            if session is None:
                return None
            self._assert_identity(session, user_id=user_id, channel=channel)
            row = connection.execute(
                "SELECT b.payload, r.context_generation, "
                "r.current_usage_json FROM turn_context_revision t "
                "JOIN context_revisions r ON r.session_id = t.session_id "
                "AND r.revision_id = t.revision_id "
                "JOIN context_blobs b ON b.content_hash = r.content_hash "
                "WHERE t.session_id = ? AND t.turn_id = ?",
                (session_id, turn_id),
            ).fetchone()
            if row is None:
                return None
            return RuntimeSnapshot(
                state=json.loads(row["payload"]),
                context_generation=int(row["context_generation"]),
                current_usage=(
                    json.loads(row["current_usage_json"])
                    if row["current_usage_json"]
                    else None
                ),
            )

    def write_runtime_state(
        self,
        *,
        session_id: str,
        user_id: str,
        channel: str,
        state: dict[str, Any],
        current_usage: dict[str, Any] | None = None,
        reset_context: bool = False,
        only_if_missing: bool = False,
        turn_id: str | None = None,
    ) -> tuple[int, bool]:
        """Persist one runtime snapshot and optionally start a new context."""
        state_json = json.dumps(
            state,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        timestamp = _utc_now()
        with self._transaction():
            self._conn.execute(
                "INSERT INTO transcript_sessions("
                "session_id, user_id, channel) "
                "VALUES (?, ?, ?) ON CONFLICT(session_id) DO NOTHING",
                (session_id, user_id, channel),
            )
            session = self._session_row(session_id)
            if session is None:
                raise RuntimeError("failed to create transcript session")
            self._assert_identity(
                session,
                user_id=user_id,
                channel=channel,
            )
            existing = self._conn.execute(
                "SELECT context_generation, current_usage_json "
                "FROM session_runtime WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if existing is not None and only_if_missing:
                return int(existing["context_generation"]), False

            generation = int(existing["context_generation"]) if existing else 0
            usage_json = existing["current_usage_json"] if existing else None
            if current_usage is not None:
                usage_json = json.dumps(
                    current_usage,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            if reset_context:
                generation += 1
                previous = json.loads(usage_json) if usage_json else {}
                previous_context = previous.get("context_usage") or {}
                usage_json = json.dumps(
                    {
                        "usage": None,
                        "context_usage": {
                            "estimated_tokens": 0,
                            "max_input_length": int(
                                previous_context.get("max_input_length", 0)
                                or 0,
                            ),
                            "context_usage_ratio": 0,
                        },
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            self._conn.execute(
                "INSERT INTO session_runtime("
                "session_id, context_generation, state_json, "
                "current_usage_json, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "context_generation = excluded.context_generation, "
                "state_json = excluded.state_json, "
                "current_usage_json = excluded.current_usage_json, "
                "updated_at = excluded.updated_at",
                (
                    session_id,
                    generation,
                    state_json,
                    usage_json,
                    timestamp,
                ),
            )
            if turn_id is not None or reset_context:
                record_revision(
                    self._conn,
                    session_id=session_id,
                    turn_id=turn_id,
                    payload=state_json,
                    generation=generation,
                    usage_json=usage_json,
                    created_at=timestamp,
                )
            else:
                self._conn.execute(
                    "DELETE FROM current_context_revision "
                    "WHERE session_id = ?",
                    (session_id,),
                )
            return generation, True

    def replace_runtime_state(
        self,
        *,
        session_id: str,
        user_id: str,
        channel: str,
        state: dict[str, Any],
        context_generation: int,
        current_usage: dict[str, Any] | None,
    ) -> None:
        """Replace a runtime snapshot while preserving its generation."""
        with self._transaction():
            self._conn.execute(
                "INSERT INTO transcript_sessions("
                "session_id, user_id, channel) VALUES (?, ?, ?) "
                "ON CONFLICT(session_id) DO NOTHING",
                (session_id, user_id, channel),
            )
            session = self._session_row(session_id)
            if session is None:
                raise RuntimeError("failed to create transcript session")
            self._assert_identity(
                session,
                user_id=user_id,
                channel=channel,
            )
            self._conn.execute(
                "INSERT INTO session_runtime("
                "session_id, context_generation, state_json, "
                "current_usage_json, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "context_generation = excluded.context_generation, "
                "state_json = excluded.state_json, "
                "current_usage_json = excluded.current_usage_json, "
                "updated_at = excluded.updated_at",
                (
                    session_id,
                    context_generation,
                    json.dumps(
                        state,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    (
                        json.dumps(
                            current_usage,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        if current_usage is not None
                        else None
                    ),
                    _utc_now(),
                ),
            )
            self._conn.execute(
                "DELETE FROM current_context_revision WHERE session_id = ?",
                (session_id,),
            )

    def update_runtime_state(
        self,
        *,
        session_id: str,
        path: list[str],
        value: Any,
    ) -> None:
        """Update one nested value in an existing runtime snapshot."""
        with self._transaction():
            row = self._conn.execute(
                "SELECT state_json FROM session_runtime "
                "WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            state = json.loads(row["state_json"])
            current = state
            for key in path[:-1]:
                child = current.get(key)
                if not isinstance(child, dict):
                    child = {}
                    current[key] = child
                current = child
            current[path[-1]] = value
            state_json = json.dumps(
                state,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            self._conn.execute(
                "UPDATE session_runtime SET state_json = ?, updated_at = ? "
                "WHERE session_id = ?",
                (state_json, _utc_now(), session_id),
            )
            self._conn.execute(
                "DELETE FROM current_context_revision WHERE session_id = ?",
                (session_id,),
            )

    def set_current_usage(
        self,
        *,
        session_id: str,
        usage: dict[str, Any] | None,
        context_usage: dict[str, Any] | None,
    ) -> None:
        """Persist the current context projection independently of history."""
        payload = json.dumps(
            {"usage": usage, "context_usage": context_usage},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._transaction():
            cursor = self._conn.execute(
                "UPDATE session_runtime SET current_usage_json = ?, "
                "updated_at = ? WHERE session_id = ?",
                (payload, _utc_now(), session_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(session_id)

    def export_database(
        self,
        *,
        session_id: str,
        user_id: str,
        channel: str,
    ) -> bytes:
        """Return a consistent database snapshot without running turns."""
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "session.db"
            snapshot = sqlite3.connect(str(snapshot_path))
            snapshot.row_factory = sqlite3.Row
            snapshot.execute("PRAGMA foreign_keys=ON")
            try:
                with self._lock:
                    session = self._session_row(session_id)
                    if session is None:
                        raise KeyError(session_id)
                    self._assert_identity(
                        session,
                        user_id=user_id,
                        channel=channel,
                    )
                    self._conn.backup(snapshot)
                with snapshot:
                    snapshot.execute(
                        "DELETE FROM transcript_turns "
                        "WHERE session_id = ? AND status = 'running'",
                        (session_id,),
                    )
                    snapshot.execute(
                        "UPDATE transcript_sessions SET next_turn_seq = "
                        "COALESCE((SELECT MAX(turn_seq) + 1 "
                        "FROM transcript_turns WHERE session_id = ?), 1) "
                        "WHERE session_id = ?",
                        (session_id, session_id),
                    )
                integrity = snapshot.execute(
                    "PRAGMA integrity_check",
                ).fetchone()
                if integrity is None or integrity[0] != "ok":
                    raise sqlite3.DatabaseError(
                        "checkpoint database integrity check failed",
                    )
            finally:
                snapshot.close()
            return snapshot_path.read_bytes()

    def restore_database(
        self,
        blob: bytes,
        *,
        session_id: str,
        user_id: str,
        channel: str,
    ) -> None:
        """Restore a database snapshot and retain active command turns."""
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "session.db"
            snapshot_path.write_bytes(blob)
            source = sqlite3.connect(str(snapshot_path))
            source.row_factory = sqlite3.Row
            source.execute("PRAGMA query_only=ON")
            try:
                validate_database(source)
                integrity = source.execute(
                    "PRAGMA integrity_check",
                ).fetchone()
                if integrity is None or integrity[0] != "ok":
                    raise sqlite3.DatabaseError(
                        "checkpoint database integrity check failed",
                    )
                session = self._session_row(session_id, source)
                if session is None:
                    raise KeyError(session_id)
                self._assert_identity(
                    session,
                    user_id=user_id,
                    channel=channel,
                )
                with self._lock:
                    running_turns = self._conn.execute(
                        "SELECT session_id, turn_seq, turn_id, status, "
                        "error_json, replaces_turn_id, created_at, "
                        "finished_at FROM transcript_turns "
                        "WHERE session_id = ? AND status = 'running'",
                        (session_id,),
                    ).fetchall()
                    running_messages = self._conn.execute(
                        "SELECT m.session_id, m.turn_id, m.message_id, "
                        "m.ordinal, m.role, m.envelope_json, "
                        "m.client_message_id, m.superseded_at, "
                        "m.created_at, m.finished_at "
                        "FROM transcript_messages AS m "
                        "JOIN transcript_turns AS t "
                        "ON t.session_id = m.session_id "
                        "AND t.turn_id = m.turn_id "
                        "WHERE m.session_id = ? AND t.status = 'running'",
                        (session_id,),
                    ).fetchall()
                    running_blocks = self._conn.execute(
                        "SELECT b.* FROM transcript_blocks b "
                        "JOIN transcript_messages m "
                        "ON m.session_id = b.session_id "
                        "AND m.message_id = b.message_id "
                        "JOIN transcript_turns t "
                        "ON t.session_id = m.session_id "
                        "AND t.turn_id = m.turn_id "
                        "WHERE m.session_id = ? AND t.status = 'running'",
                        (session_id,),
                    ).fetchall()
                    source.backup(self._conn)
                    with self._conn:
                        row = self._conn.execute(
                            "SELECT COALESCE(MAX(turn_seq), 0) "
                            "FROM transcript_turns WHERE session_id = ?",
                            (session_id,),
                        ).fetchone()
                        next_turn_seq = int(row[0]) + 1
                        restored_turns = [
                            (
                                turn["session_id"],
                                next_turn_seq + index,
                                turn["turn_id"],
                                turn["status"],
                                turn["error_json"],
                                turn["replaces_turn_id"],
                                turn["created_at"],
                                turn["finished_at"],
                            )
                            for index, turn in enumerate(running_turns)
                        ]
                        self._conn.executemany(
                            "INSERT INTO transcript_turns("
                            "session_id, turn_seq, turn_id, status, "
                            "error_json, replaces_turn_id, created_at, "
                            "finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            restored_turns,
                        )
                        self._conn.executemany(
                            "INSERT INTO transcript_messages("
                            "session_id, turn_id, message_id, ordinal, role, "
                            "envelope_json, client_message_id, "
                            "superseded_at, created_at, finished_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            [tuple(row) for row in running_messages],
                        )
                        self._conn.executemany(
                            "INSERT INTO transcript_blocks VALUES "
                            "(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            [tuple(row) for row in running_blocks],
                        )
                        self._conn.execute(
                            "UPDATE transcript_sessions SET next_turn_seq = "
                            "COALESCE((SELECT MAX(turn_seq) + 1 "
                            "FROM transcript_turns WHERE session_id = ?), 1) "
                            "WHERE session_id = ?",
                            (session_id, session_id),
                        )
            finally:
                source.close()

    def clone_completed_session_from(
        self,
        *,
        source_path: Path,
        source_session_id: str,
        target_session_id: str,
        target_user_id: str,
        target_channel: str,
    ) -> bool:
        """Clone persisted chat history and agent context into this store."""
        if self._session_row(target_session_id) is not None:
            raise ValueError("target transcript session already exists")

        self._conn.execute("ATTACH DATABASE ? AS parent", (str(source_path),))
        try:
            source = self._conn.execute(
                "SELECT next_turn_seq FROM parent.transcript_sessions "
                "WHERE session_id = ?",
                (source_session_id,),
            ).fetchone()
            if source is None:
                return False

            runtime = self._conn.execute(
                "SELECT context_generation, state_json, "
                "current_usage_json, updated_at "
                "FROM parent.session_runtime WHERE session_id = ?",
                (source_session_id,),
            ).fetchone()
            with self._transaction():
                self._conn.execute(
                    "INSERT INTO transcript_sessions("
                    "session_id, user_id, channel, next_turn_seq) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        target_session_id,
                        target_user_id,
                        target_channel,
                        int(source["next_turn_seq"]),
                    ),
                )
                self._conn.execute(
                    "INSERT INTO transcript_turns("
                    "session_id, turn_seq, turn_id, status, error_json, "
                    "replaces_turn_id, created_at, finished_at) "
                    "SELECT ?, turn_seq, turn_id, status, error_json, "
                    "replaces_turn_id, created_at, finished_at "
                    "FROM parent.transcript_turns "
                    "WHERE session_id = ? AND status != 'running'",
                    (target_session_id, source_session_id),
                )
                self._conn.execute(
                    "INSERT INTO transcript_messages("
                    "session_id, turn_id, message_id, ordinal, role, "
                    "envelope_json, client_message_id, superseded_at, "
                    "created_at, finished_at) "
                    "SELECT ?, m.turn_id, m.message_id, m.ordinal, m.role, "
                    "m.envelope_json, m.client_message_id, m.superseded_at, "
                    "m.created_at, m.finished_at "
                    "FROM parent.transcript_messages AS m "
                    "JOIN parent.transcript_turns AS t "
                    "ON t.session_id = m.session_id "
                    "AND t.turn_id = m.turn_id "
                    "WHERE m.session_id = ? AND t.status != 'running'",
                    (target_session_id, source_session_id),
                )
                copy_message_blocks(
                    self._conn,
                    source_session_id=source_session_id,
                    target_session_id=target_session_id,
                )
                if runtime is not None:
                    source_state = json.loads(runtime["state_json"])
                    state = (
                        {"agent": source_state["agent"]}
                        if "agent" in source_state
                        else {}
                    )
                    self._conn.execute(
                        "INSERT INTO session_runtime("
                        "session_id, context_generation, state_json, "
                        "current_usage_json, updated_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            target_session_id,
                            int(runtime["context_generation"]),
                            json.dumps(
                                state,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                            runtime["current_usage_json"],
                            runtime["updated_at"],
                        ),
                    )
            return True
        finally:
            self._conn.execute("DETACH DATABASE parent")

    def fork_turn_from(
        self,
        *,
        source_path: Path,
        source_session_id: str,
        target_session_id: str,
        target_user_id: str,
        target_channel: str,
        turn_id: str,
    ) -> None:
        """Create an independent session at an exact completed boundary."""
        with self._lock:
            self._conn.execute(
                "ATTACH DATABASE ? AS parent",
                (str(source_path),),
            )
            try:
                with self._transaction():
                    fork_turn(
                        self._conn,
                        source_session_id=source_session_id,
                        target_session_id=target_session_id,
                        target_user_id=target_user_id,
                        target_channel=target_channel,
                        turn_id=turn_id,
                    )
            finally:
                self._conn.execute("DETACH DATABASE parent")

    def message_payloads(
        self,
        *,
        session_id: str,
        start_date: str,
        end_date: str,
    ) -> list[dict[str, Any]]:
        """Return visible persisted messages within an inclusive date range."""
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT payload_json, created_at "
                "FROM transcript_message_payloads WHERE session_id = ? "
                "AND superseded_at IS NULL "
                "AND substr(created_at, 1, 10) BETWEEN ? AND ? "
                "ORDER BY created_at, ordinal",
                (session_id, start_date, end_date),
            ).fetchall()
        messages: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            if not isinstance(payload, dict):
                continue
            if not payload.get("created_at") and not payload.get("timestamp"):
                payload["created_at"] = row["created_at"]
            messages.append(payload)
        return messages

    def recover_running_turns(self) -> int:
        """Cancel turns left running by a previous process."""
        with self._lock:
            orphan = self._conn.execute(
                "SELECT 1 FROM transcript_turns WHERE status = 'running' "
                "LIMIT 1",
            ).fetchone()
            if orphan is None:
                return 0
            with self._transaction():
                cursor = self._conn.execute(
                    "UPDATE transcript_turns SET status = 'cancelled', "
                    "finished_at = COALESCE(finished_at, ?) "
                    "WHERE status = 'running'",
                    (_utc_now(),),
                )
                return cursor.rowcount

    def import_legacy_messages(
        self,
        *,
        session_id: str,
        user_id: str,
        channel: str,
        messages: list[Message],
    ) -> bool:
        """Atomically seed an empty transcript from legacy display history."""
        if not messages:
            return False
        turns: list[list[Message]] = []
        for message in messages:
            if not turns or _enum_value(message.role) == "user":
                turns.append([])
            turns[-1].append(message)

        now = _utc_now()
        with self._transaction():
            session = self._session_row(session_id)
            if session is not None:
                self._assert_identity(
                    session,
                    user_id=user_id,
                    channel=channel,
                )
                existing_turn = self._conn.execute(
                    "SELECT 1 FROM transcript_turns WHERE session_id = ? "
                    "LIMIT 1",
                    (session_id,),
                ).fetchone()
                if existing_turn is not None:
                    return False
                self._conn.execute(
                    "UPDATE transcript_sessions SET next_turn_seq = ? "
                    "WHERE session_id = ?",
                    (len(turns) + 1, session_id),
                )
            else:
                self._conn.execute(
                    "INSERT INTO transcript_sessions("
                    "session_id, user_id, channel, next_turn_seq) "
                    "VALUES (?, ?, ?, ?)",
                    (session_id, user_id, channel, len(turns) + 1),
                )
            for turn_seq, turn_messages in enumerate(turns, start=1):
                first_metadata = turn_messages[0].metadata or {}
                last_metadata = turn_messages[-1].metadata or {}
                created_at = str(first_metadata.get("timestamp") or now)
                finished_at = str(
                    last_metadata.get("finished_at")
                    or last_metadata.get("timestamp")
                    or created_at,
                )
                turn_id = f"legacy:{turn_seq}"
                self._conn.execute(
                    "INSERT INTO transcript_turns("
                    "session_id, turn_seq, turn_id, status, created_at, "
                    "finished_at) VALUES (?, ?, ?, 'completed', ?, ?)",
                    (
                        session_id,
                        turn_seq,
                        turn_id,
                        created_at,
                        finished_at,
                    ),
                )
                for ordinal, message in enumerate(turn_messages):
                    metadata = message.metadata or {}
                    self._conn.execute(
                        "INSERT INTO transcript_messages("
                        "session_id, turn_id, message_id, ordinal, role, "
                        "envelope_json, created_at, client_message_id, "
                        "finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            session_id,
                            turn_id,
                            message.id,
                            ordinal,
                            _enum_value(message.role),
                            message_envelope(message),
                            str(metadata.get("timestamp") or created_at),
                            self._client_message_id(message),
                            metadata.get("finished_at"),
                        ),
                    )
                    write_message_blocks(
                        self._conn,
                        session_id=session_id,
                        message_id=message.id,
                        content=[
                            block.model_dump(mode="json")
                            for block in message.content
                        ],
                    )
        return True

    def start_turn(
        self,
        *,
        session_id: str,
        user_id: str,
        channel: str,
        turn_id: str,
        replaces_turn_id: str | None = None,
        created_at: str | None = None,
    ) -> None:
        """Create a running turn if it does not already exist."""
        timestamp = created_at or _utc_now()
        with self._transaction():
            self._conn.execute(
                "INSERT INTO transcript_sessions("
                "session_id, user_id, channel) VALUES (?, ?, ?) "
                "ON CONFLICT(session_id) DO NOTHING",
                (
                    session_id,
                    user_id,
                    channel,
                ),
            )
            session = self._session_row(session_id)
            if session is None:
                raise RuntimeError("failed to create transcript session")
            self._assert_identity(
                session,
                user_id=user_id,
                channel=channel,
            )
            existing = self._conn.execute(
                "SELECT turn_seq, replaces_turn_id FROM transcript_turns "
                "WHERE session_id = ? AND turn_id = ?",
                (session_id, turn_id),
            ).fetchone()
            if existing is not None:
                if existing["replaces_turn_id"] != replaces_turn_id:
                    raise ValueError("transcript replacement target mismatch")
                return

            if replaces_turn_id is not None:
                replacement = self._conn.execute(
                    "SELECT 1 FROM transcript_turns "
                    "WHERE session_id = ? AND turn_id = ?",
                    (session_id, replaces_turn_id),
                ).fetchone()
                if replacement is None:
                    raise ValueError("replacement transcript turn not found")

            turn_seq = int(session["next_turn_seq"])
            self._conn.execute(
                "INSERT INTO transcript_turns("
                "session_id, turn_seq, turn_id, status, "
                "replaces_turn_id, created_at) "
                "VALUES (?, ?, ?, 'running', ?, ?)",
                (
                    session_id,
                    turn_seq,
                    turn_id,
                    replaces_turn_id,
                    timestamp,
                ),
            )
            self._conn.execute(
                "UPDATE transcript_sessions SET "
                "next_turn_seq = ? WHERE session_id = ?",
                (
                    turn_seq + 1,
                    session_id,
                ),
            )

    def upsert_message(
        self,
        *,
        session_id: str,
        turn_id: str,
        message: Message,
        ordinal: int,
        created_at: str | None = None,
        finished_at: str | None = None,
    ) -> None:
        """Insert or update one complete display message snapshot."""
        payload = message_envelope(message)
        role = _enum_value(message.role)
        client_message_id = self._client_message_id(message)
        timestamp = created_at or _utc_now()
        with self._transaction():
            turn = self._conn.execute(
                "SELECT 1 FROM transcript_turns "
                "WHERE session_id = ? AND turn_id = ?",
                (session_id, turn_id),
            ).fetchone()
            if turn is None:
                raise ValueError("transcript turn does not exist")
            existing = self._conn.execute(
                "SELECT turn_id, ordinal, role, envelope_json, "
                "client_message_id, finished_at "
                "FROM transcript_messages "
                "WHERE session_id = ? AND message_id = ?",
                (session_id, message.id),
            ).fetchone()
            if existing is not None and existing["turn_id"] != turn_id:
                raise ValueError("transcript message belongs to another turn")

            self._conn.execute(
                "INSERT INTO transcript_messages("
                "session_id, turn_id, message_id, ordinal, role, "
                "envelope_json, created_at, "
                "client_message_id, finished_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id, message_id) DO UPDATE SET "
                "turn_id = excluded.turn_id, ordinal = excluded.ordinal, "
                "role = excluded.role, "
                "envelope_json = excluded.envelope_json, "
                "client_message_id = excluded.client_message_id, "
                "finished_at = excluded.finished_at "
                "WHERE transcript_messages.envelope_json "
                "IS NOT excluded.envelope_json "
                "OR transcript_messages.ordinal != excluded.ordinal "
                "OR transcript_messages.finished_at "
                "IS NOT excluded.finished_at "
                "OR transcript_messages.client_message_id "
                "IS NOT excluded.client_message_id",
                (
                    session_id,
                    turn_id,
                    message.id,
                    ordinal,
                    role,
                    payload,
                    timestamp,
                    client_message_id,
                    finished_at,
                ),
            )

            write_message_blocks(
                self._conn,
                session_id=session_id,
                message_id=message.id,
                content=[
                    block.model_dump(mode="json") for block in message.content
                ],
            )

    def read_replay_page(
        self,
        *,
        session_id: str,
        turn_id: str,
        through: int,
        after: int,
    ) -> list[tuple[int, str]]:
        """Read committed envelope messages without exposing running turns.

        Envelope terminal message sequence numbers do not change when the
        containing response is finalized. Later completions are excluded.
        """
        result: list[tuple[int, str]] = []
        size = 0
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT ordinal,payload_json FROM transcript_message_payloads "
                "WHERE session_id = ? AND turn_id = ? AND ordinal > ? "
                "AND json_extract(envelope_json, "
                "'$.sequence_number') <= ? "
                "AND role != 'user' ORDER BY ordinal LIMIT 40",
                (session_id, turn_id, after, through),
            )
            for row in rows:
                payload = str(row["payload_json"])
                length = len(payload.encode("utf-8"))
                if length > 8 * 1024 * 1024:
                    raise ValueError("replay message exceeds byte budget")
                if result and size + length > 2 * 1024 * 1024:
                    break
                result.append((int(row["ordinal"]), payload))
                size += length
        return result

    def finish_turn(
        self,
        *,
        session_id: str,
        turn_id: str,
        status: TurnStatus,
        error: dict[str, Any] | None = None,
        finished_at: str | None = None,
    ) -> None:
        """Set a terminal turn status and activate successful replacements."""
        if status == "running":
            raise ValueError("finish_turn requires a terminal status")
        error_json = (
            json.dumps(error, ensure_ascii=False, sort_keys=True)
            if error is not None
            else None
        )
        with self._transaction():
            existing = self._conn.execute(
                "SELECT status, error_json, finished_at, replaces_turn_id "
                "FROM transcript_turns "
                "WHERE session_id = ? AND turn_id = ?",
                (session_id, turn_id),
            ).fetchone()
            if existing is None:
                raise ValueError("transcript turn does not exist")
            if (
                finished_at is None
                and existing["status"] == status
                and existing["error_json"] == error_json
                and existing["finished_at"] is not None
            ):
                return
            timestamp = finished_at or _utc_now()
            values = (status, error_json, timestamp)
            if tuple(existing)[:3] == values:
                return
            self._conn.execute(
                "UPDATE transcript_turns SET status = ?, error_json = ?, "
                "finished_at = ? WHERE session_id = ? AND turn_id = ?",
                (status, error_json, timestamp, session_id, turn_id),
            )
            commit_turn_revision(
                self._conn,
                session_id=session_id,
                turn_id=turn_id,
            )
            if status == "completed":
                if existing["replaces_turn_id"] is not None:
                    self._conn.execute(
                        "UPDATE transcript_messages SET superseded_at = ? "
                        "WHERE session_id = ? AND turn_id = ? "
                        "AND superseded_at IS NULL",
                        (
                            timestamp,
                            session_id,
                            existing["replaces_turn_id"],
                        ),
                    )

    def attach_turn_usage(
        self,
        *,
        session_id: str,
        turn_id: str,
        usage: dict[str, Any] | None,
        context_usage: dict[str, Any] | None,
    ) -> bool:
        """Attach one usage snapshot to the turn's closing assistant."""
        snapshot = {
            "usage": usage,
            "context_usage": context_usage,
        }
        with self._transaction():
            row = self._conn.execute(
                "SELECT message_id, envelope_json "
                "FROM transcript_messages "
                "WHERE session_id = ? AND turn_id = ? "
                "AND role = 'assistant' "
                "ORDER BY ordinal DESC LIMIT 1",
                (session_id, turn_id),
            ).fetchone()
            if row is None:
                return False

            message = Message.model_validate_json(row["envelope_json"])
            metadata = dict(message.metadata or {})
            if metadata.get(TURN_USAGE_META_KEY) == snapshot:
                return True

            metadata[TURN_USAGE_META_KEY] = snapshot
            updated = message.model_copy(
                update={"metadata": metadata},
                deep=True,
            )
            self._conn.execute(
                "UPDATE transcript_messages SET envelope_json = ? "
                "WHERE session_id = ? AND message_id = ?",
                (
                    message_envelope(updated),
                    session_id,
                    row["message_id"],
                ),
            )
            return True

    def get_page(
        self,
        *,
        session_id: str,
        user_id: str,
        channel: str,
        before: TranscriptCursor | None = None,
        after_message_id: str | None = None,
        limit: int = 5,
        max_bytes: int = _DEFAULT_PAGE_MAX_BYTES,
        max_content_blocks: int = _DEFAULT_PAGE_MAX_CONTENT_BLOCKS,
    ) -> TranscriptPage | None:
        """Read a turn-, block-, and byte-bounded durable history page."""
        if before is not None and after_message_id is not None:
            raise ValueError("history boundaries are mutually exclusive")
        if limit < 1 or limit > 100:
            raise ValueError(
                "transcript turn limit must be between 1 and 100",
            )
        if max_bytes < 1:
            raise ValueError("transcript page max_bytes must be positive")
        if max_content_blocks < 1:
            raise ValueError(
                "transcript page max_content_blocks must be positive",
            )
        with self._read_connection() as connection:
            connection.execute("BEGIN")
            session = self._session_row(session_id, connection)
            if session is None:
                return None
            self._assert_identity(
                session,
                user_id=user_id,
                channel=channel,
            )
            snapshot_turn_seq = (
                before.snapshot_turn_seq
                if before is not None and before.snapshot_turn_seq is not None
                else max(0, int(session["next_turn_seq"]) - 1)
            )
            turn_sql = "SELECT t.turn_seq FROM transcript_turns t "
            if after_message_id is not None:
                boundary = connection.execute(
                    "SELECT t.turn_seq FROM transcript_messages m "
                    "JOIN transcript_turns t ON t.session_id = m.session_id "
                    "AND t.turn_id = m.turn_id WHERE m.session_id = ? "
                    "AND m.message_id = ? AND m.superseded_at IS NULL",
                    (session_id, after_message_id),
                ).fetchone()
                if boundary is None:
                    raise ValueError("history boundary message not found")
                following = connection.execute(
                    "SELECT t.turn_seq FROM transcript_turns t "
                    "WHERE t.session_id = ? AND t.turn_seq > ? "
                    "AND t.turn_seq <= ? AND EXISTS ("
                    "SELECT 1 FROM transcript_messages m "
                    "WHERE m.session_id = t.session_id "
                    "AND m.turn_id = t.turn_id "
                    "AND m.superseded_at IS NULL "
                    "AND (t.status != 'running' OR m.role = 'user')) "
                    "ORDER BY t.turn_seq ASC LIMIT ?",
                    (
                        session_id,
                        boundary["turn_seq"],
                        snapshot_turn_seq,
                        limit,
                    ),
                ).fetchall()
                if not following:
                    return TranscriptPage(
                        messages=[],
                        next_before=None,
                        has_more=False,
                        snapshot_turn_seq=snapshot_turn_seq,
                    )
                before = TranscriptCursor(
                    turn_seq=int(following[-1]["turn_seq"]) + 1,
                    ordinal=0,
                    snapshot_turn_seq=snapshot_turn_seq,
                )
            turn_sql += "WHERE t.session_id = ? AND t.turn_seq <= ? "
            params: list[Any] = [session_id, snapshot_turn_seq]
            if before is not None:
                at_turn_boundary = (
                    before.ordinal == 0 and before.content_before == 0
                )
                operator = "<" if at_turn_boundary else "<="
                turn_sql += f"AND t.turn_seq {operator} ? "
                params.append(before.turn_seq)
            turn_sql += (
                "AND EXISTS (SELECT 1 FROM transcript_messages vm "
                "WHERE vm.session_id = t.session_id "
                "AND vm.turn_id = t.turn_id "
                "AND vm.superseded_at IS NULL "
                "AND (t.status != 'running' OR vm.role = 'user')) "
                "ORDER BY t.turn_seq DESC LIMIT ?"
            )
            params.append(limit + 1)
            candidates = connection.execute(turn_sql, params).fetchall()
            selected_turns = [
                int(row["turn_seq"]) for row in candidates[:limit]
            ]
            if not selected_turns:
                return TranscriptPage(
                    messages=[],
                    next_before=None,
                    has_more=False,
                    snapshot_turn_seq=snapshot_turn_seq,
                )

            message_filter = (
                "FROM transcript_turns t CROSS JOIN transcript_messages m "
                "ON m.session_id = t.session_id AND m.turn_id = t.turn_id "
                "WHERE t.session_id = ? AND m.superseded_at IS NULL "
                "AND (t.status != 'running' OR m.role = 'user') "
                "AND t.turn_seq IN (SELECT value FROM json_each(?)) "
            )
            message_params: list[Any] = [
                session_id,
                json.dumps(selected_turns),
            ]
            if before is not None and (
                before.ordinal > 0 or before.content_before > 0
            ):
                message_filter += "AND (t.turn_seq < ? OR "
                message_filter += (
                    "(t.turn_seq = ? AND m.ordinal <= ?)) "
                    if before.content_before > 0
                    else "(t.turn_seq = ? AND m.ordinal < ?)) "
                )
                message_params.extend(
                    [before.turn_seq, before.turn_seq, before.ordinal],
                )

            size_sql = (
                "SELECT m.envelope_json, m.session_id, m.message_id, "
                "(SELECT COUNT(*) FROM transcript_blocks b "
                "WHERE b.session_id = m.session_id "
                "AND b.message_id = m.message_id) AS content_total, "
                "m.created_at AS message_created_at, "
                "m.finished_at AS message_finished_at, m.ordinal, "
                "t.turn_id, t.turn_seq, t.status AS turn_status, "
                "t.error_json, t.finished_at AS turn_finished_at "
                + message_filter
                + "ORDER BY t.turn_seq DESC, m.ordinal DESC"
            )
            selection = TranscriptPageBuilder().select(
                connection.execute(size_sql, message_params),
                connection=connection,
                before=before,
                snapshot_turn_seq=snapshot_turn_seq,
                max_bytes=max_bytes,
                max_content_blocks=max_content_blocks,
                has_more=len(candidates) > limit,
            )
            if not selection.messages:
                return TranscriptPage(
                    messages=[],
                    next_before=None,
                    has_more=False,
                    snapshot_turn_seq=snapshot_turn_seq,
                )

            selection.messages.reverse()
            return TranscriptPage(
                messages=selection.messages,
                next_before=(
                    selection.oldest_position if selection.has_more else None
                ),
                has_more=selection.has_more,
                snapshot_turn_seq=snapshot_turn_seq,
            )

    def get_user_message_text(
        self,
        *,
        session_id: str,
        user_id: str,
        channel: str,
        message_id: str,
    ) -> str | None:
        """Read one complete user input without loading assistant messages."""
        with self._read_connection() as connection:
            connection.execute("BEGIN")
            session = self._session_row(session_id, connection)
            if session is None:
                return None
            self._assert_identity(session, user_id=user_id, channel=channel)
            row = connection.execute(
                "SELECT payload_json FROM transcript_message_payloads "
                "WHERE session_id = ? AND message_id = ? "
                "AND role = 'user' AND superseded_at IS NULL",
                (session_id, message_id),
            ).fetchone()
            if row is None:
                return None
            message = Message.model_validate_json(row["payload_json"])
            return "\n".join(
                part.text
                for part in message.content
                if isinstance(part, TextContent) and part.text
            )

    def get_user_message_page(
        self,
        *,
        session_id: str,
        user_id: str,
        channel: str,
        before: TranscriptCursor | None = None,
        limit: int = 200,
    ) -> TranscriptUserMessagePage | None:
        """Read lightweight user-message entries for history navigation."""
        if limit < 1 or limit > 1000:
            raise ValueError(
                "user-message index limit must be between 1 and 1000",
            )
        with self._read_connection() as connection:
            connection.execute("BEGIN")
            session = self._session_row(session_id, connection)
            if session is None:
                return None
            self._assert_identity(
                session,
                user_id=user_id,
                channel=channel,
            )
            snapshot_turn_seq = (
                before.snapshot_turn_seq
                if before is not None and before.snapshot_turn_seq is not None
                else max(0, int(session["next_turn_seq"]) - 1)
            )
            query = (
                "SELECT m.message_id, m.payload_json, m.created_at, "
                "m.client_message_id, "
                "m.ordinal, t.turn_seq "
                "FROM transcript_turns AS t "
                "JOIN transcript_message_payloads AS m "
                "ON m.session_id = t.session_id "
                "AND m.turn_id = t.turn_id "
                "WHERE t.session_id = ? AND t.turn_seq <= ? "
                "AND m.role = 'user' AND m.superseded_at IS NULL "
            )
            params: list[Any] = [session_id, snapshot_turn_seq]
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM transcript_turns AS t "
                    "JOIN transcript_message_payloads AS m "
                    "ON m.session_id = t.session_id AND m.turn_id = t.turn_id "
                    "WHERE t.session_id = ? AND t.turn_seq <= ? "
                    "AND m.role = 'user' AND m.superseded_at IS NULL",
                    params,
                ).fetchone()[0],
            )
            if before is not None:
                query += (
                    "AND (t.turn_seq < ? OR "
                    "(t.turn_seq = ? AND m.ordinal < ?)) "
                )
                params.extend(
                    [before.turn_seq, before.turn_seq, before.ordinal],
                )
            query += "ORDER BY t.turn_seq DESC, m.ordinal DESC LIMIT ?"
            params.append(limit + 1)
            rows = connection.execute(query, params).fetchall()
            has_more = len(rows) > limit
            selected = rows[:limit]
            items = []
            for row in selected:
                preview = self._user_message_preview(
                    row["payload_json"],
                )
                items.append(
                    TranscriptUserMessage(
                        message_id=str(row["message_id"]),
                        client_message_id=row["client_message_id"],
                        preview=preview,
                        created_at=str(row["created_at"]),
                        anchor=TranscriptCursor(
                            turn_seq=int(row["turn_seq"]) + 1,
                            ordinal=0,
                            snapshot_turn_seq=snapshot_turn_seq,
                        ),
                    ),
                )
            items.reverse()
            next_before = None
            if has_more and selected:
                oldest = selected[-1]
                next_before = TranscriptCursor(
                    turn_seq=int(oldest["turn_seq"]),
                    ordinal=int(oldest["ordinal"]),
                    snapshot_turn_seq=snapshot_turn_seq,
                )
            return TranscriptUserMessagePage(
                items=items,
                next_before=next_before,
                has_more=has_more,
                snapshot_turn_seq=snapshot_turn_seq,
                total=total,
            )

    @staticmethod
    def _user_message_preview(
        payload_json: str,
        limit: int = 160,
    ) -> str:
        """Extract a bounded single-line preview from a user message."""
        message = Message.model_validate_json(payload_json)
        text = "\n".join(
            content.text
            for content in message.content
            if isinstance(content, TextContent)
        )
        preview = " ".join(text.split())[:limit]
        return preview

    def find_turn_for_message(
        self,
        *,
        session_id: str,
        message_id: str | None = None,
        client_message_id: str | None = None,
    ) -> str | None:
        """Resolve a regeneration target to its transcript turn."""
        if not message_id and not client_message_id:
            return None
        with self._read_connection() as connection:
            if message_id:
                row = connection.execute(
                    "SELECT turn_id FROM transcript_messages "
                    "WHERE session_id = ? AND message_id = ?",
                    (session_id, message_id),
                ).fetchone()
                if row is not None:
                    return str(row["turn_id"])
            if client_message_id:
                row = connection.execute(
                    "SELECT turn_id FROM transcript_messages "
                    "WHERE session_id = ? AND client_message_id = ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (session_id, client_message_id),
                ).fetchone()
                if row is not None:
                    return str(row["turn_id"])
        return None

    def close(self) -> None:
        """Close the SQLite connection idempotently."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()


__all__ = [
    "RuntimeSnapshot",
    "TranscriptCursor",
    "TranscriptPage",
    "TranscriptStore",
]
