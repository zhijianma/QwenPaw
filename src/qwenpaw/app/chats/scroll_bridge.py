# -*- coding: utf-8 -*-
"""Scroll compatibility writer backed by the rebuildable Chat recall DB."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

from .storage import (
    ChatStoragePaths,
    initialize_recall_database,
    reserve_recall_sequences,
)
from .storage.common import connect_database, immediate_transaction


_RECALL_TOOL_NAMES = {"recall_history", "recall_history_python"}


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


class ChatRecallHistoryStore:
    """Provide Scroll's writer contract without touching legacy history.db."""

    def __init__(
        self,
        *,
        paths: ChatStoragePaths,
        conversation_id: str,
        turn_id: str,
        session_id: str,
        agent_id: str,
    ) -> None:
        initialize_recall_database(paths.recall)
        self._paths = paths
        self._conversation_id = conversation_id
        self._turn_id = turn_id
        self._session_id = session_id
        self._agent_id = agent_id
        self._lease_owner = f"scroll:{os.getpid()}:{uuid4().hex}"
        self._lock = threading.Lock()
        self._closed = False
        self.degraded = False
        self.write_failures = 0

    @classmethod
    def resolve(
        cls,
        workspace: Path,
        *,
        session_id: str,
        agent_id: str,
    ) -> "ChatRecallHistoryStore":
        """Resolve the accepted run for one workspace request."""
        paths = ChatStoragePaths(workspace)
        with connect_database(paths.catalog, readonly=True) as connection:
            rows = connection.execute(
                "SELECT e.conversation_id "
                "FROM chat_alias_index AS a "
                "JOIN chat_catalog_entries AS e ON e.conversation_id = "
                "a.conversation_id WHERE a.agent_id = ? "
                "AND a.alias_namespace = 'legacy_session' "
                "AND a.external_key = ? AND e.lifecycle = 'ready'",
                (agent_id, session_id),
            ).fetchall()
        if len(rows) != 1:
            raise LookupError(
                f"Chat alias is missing or ambiguous: {session_id}",
            )
        conversation_id = str(rows[0]["conversation_id"])
        with connect_database(
            paths.conversation(conversation_id),
            readonly=True,
        ) as connection:
            run = connection.execute(
                "SELECT turn_id FROM chat_runs WHERE state IN "
                "('accepted', 'running', 'stopping')",
            ).fetchone()
        if run is None:
            raise LookupError(f"Accepted Chat run is missing: {session_id}")
        return cls(
            paths=paths,
            conversation_id=conversation_id,
            turn_id=str(run["turn_id"]),
            session_id=session_id,
            agent_id=agent_id,
        )

    @property
    def path(self) -> Path:
        """Return the recall projection path used by existing recall tools."""
        return self._paths.recall

    @property
    def closed(self) -> bool:
        """Return whether the bridge intentionally stopped accepting writes."""
        return self._closed

    def note_write_failure(self, _exc: BaseException) -> None:
        """Expose the same durability health signal as legacy HistoryStore."""
        self.degraded = True
        self.write_failures += 1

    def _existing(self, dedup_key: str | None) -> int | None:
        if dedup_key is None:
            return None
        with connect_database(self.path, readonly=True) as connection:
            row = connection.execute(
                "SELECT seq FROM conversation_history "
                "WHERE session_id = ? AND dedup_key = ?",
                (self._session_id, dedup_key),
            ).fetchone()
        return int(row["seq"]) if row is not None else None

    def append(
        self,
        *,
        session_id: str,
        entry: Any,
        agent_id: str | None = None,
        dedup_key: str | None = None,
    ) -> int:
        """Insert one provisional recall row with a global stable sequence."""
        if self._closed:
            raise RuntimeError("Chat recall history bridge is closed")
        if session_id != self._session_id:
            raise ValueError("Scroll session does not match accepted Chat")
        with self._lock:
            existing = self._existing(dedup_key)
            if existing is not None:
                return existing
            now_us = time.time_ns() // 1_000
            lease = reserve_recall_sequences(
                self._paths.catalog,
                workspace_id=str(self._paths.workspace.resolve()),
                count=1,
                lease_owner=self._lease_owner,
                now_us=now_us,
            )
            if lease is None:
                raise RuntimeError("Recall sequence allocation failed")
            sequence = lease.start
            block_id = f"provisional:{uuid4().hex}"
            content = getattr(entry, "content", None)
            name = getattr(entry, "name", None)
            with connect_database(self.path) as connection:
                with immediate_transaction(connection):
                    connection.execute(
                        "INSERT INTO conversation_history("
                        "seq, session_id, agent_id, kind, role, name, "
                        "content, tool_call_id, tool_input, tool_state, "
                        "headline, blocks, metadata, created_at, dedup_key, "
                        "conversation_id, turn_id, message_id, "
                        "block_id, provisional) VALUES (?, ?, ?, ?, ?, ?, "
                        "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                        (
                            sequence,
                            session_id,
                            agent_id or self._agent_id,
                            getattr(entry, "kind", "model_turn"),
                            getattr(entry, "role", None),
                            name,
                            content,
                            getattr(entry, "tool_call_id", None),
                            _json(getattr(entry, "tool_input", None)),
                            getattr(entry, "tool_state", None),
                            getattr(entry, "headline", None),
                            _json(getattr(entry, "blocks", None)),
                            _json(getattr(entry, "metadata", None)),
                            getattr(entry, "created_at", None),
                            dedup_key,
                            self._conversation_id,
                            self._turn_id,
                            dedup_key or block_id,
                            block_id,
                        ),
                    )
                    if name not in _RECALL_TOOL_NAMES:
                        connection.execute(
                            "INSERT INTO conversation_history_fts("
                            "rowid, content) VALUES (?, ?)",
                            (sequence, content or ""),
                        )
            return sequence

    def append_many(
        self,
        *,
        session_id: str,
        entries: Sequence[tuple[Any, str | None]],
        agent_id: str | None = None,
    ) -> int:
        """Append a compatibility batch and report newly created rows."""
        inserted = 0
        for entry, dedup_key in entries:
            if self._existing(dedup_key) is None:
                self.append(
                    session_id=session_id,
                    entry=entry,
                    agent_id=agent_id,
                    dedup_key=dedup_key,
                )
                inserted += 1
        return inserted

    def update_entry(self, seq: int, **values: Any) -> None:
        """Refresh a provisional row while keeping its FTS entry aligned."""
        allowed = {
            "content",
            "headline",
            "blocks",
            "tool_call_id",
            "name",
            "tool_state",
            "tool_input",
            "metadata",
        }
        selected = {
            key: value for key, value in values.items() if key in allowed
        }
        if not selected:
            return
        for key in ("blocks", "tool_input", "metadata"):
            if key in selected:
                selected[key] = _json(selected[key])
        with self._lock, connect_database(self.path) as connection:
            with immediate_transaction(connection):
                old = connection.execute(
                    "SELECT content, name FROM conversation_history "
                    "WHERE seq = ? AND provisional = 1",
                    (seq,),
                ).fetchone()
                if old is None:
                    return
                connection.execute(
                    "INSERT INTO conversation_history_fts("
                    "conversation_history_fts, rowid, content) "
                    "VALUES ('delete', ?, ?)",
                    (seq, old["content"] or ""),
                )
                assignments = ", ".join(f"{key} = ?" for key in selected)
                connection.execute(
                    f"UPDATE conversation_history SET {assignments} "
                    "WHERE seq = ? AND provisional = 1",
                    (*selected.values(), seq),
                )
                next_name = selected.get("name", old["name"])
                if next_name not in _RECALL_TOOL_NAMES:
                    connection.execute(
                        "INSERT INTO conversation_history_fts(rowid, content) "
                        "VALUES (?, ?)",
                        (seq, selected.get("content", old["content"]) or ""),
                    )

    def existing_seqs(self, seqs: set[int]) -> set[int]:
        """Return the globally addressed rows currently present."""
        if not seqs:
            return set()
        placeholders = ", ".join("?" for _ in seqs)
        with connect_database(self.path, readonly=True) as connection:
            rows = connection.execute(
                "SELECT seq FROM conversation_history WHERE seq IN ("
                f"{placeholders})",
                tuple(sorted(seqs)),
            ).fetchall()
        return {int(row["seq"]) for row in rows}

    def contents_by_seqs(self, seqs: set[int]) -> dict[int, str | None]:
        """Return exact content for selected global sequence values."""
        if not seqs:
            return {}
        placeholders = ", ".join("?" for _ in seqs)
        with connect_database(self.path, readonly=True) as connection:
            rows = connection.execute(
                "SELECT seq, content FROM conversation_history WHERE seq IN ("
                f"{placeholders})",
                tuple(sorted(seqs)),
            ).fetchall()
        return {int(row["seq"]): row["content"] for row in rows}

    def estimate_purge(self, **_kwargs: Any) -> dict[str, int]:
        """Disable retention estimates for permanent Chat history."""
        return {"rows": 0, "content_bytes": 0}

    def purge(self, **_kwargs: Any) -> int:
        """Never expire normalized Chat transcript through Scroll retention."""
        return 0

    def vacuum(self) -> None:
        """Leave projection maintenance to the Chat storage coordinator."""

    def close(self) -> None:
        """Stop accepting provisional writes; no connection is retained."""
        self._closed = True


__all__ = ["ChatRecallHistoryStore"]
