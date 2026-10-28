# -*- coding: utf-8 -*-
"""Bounded full-text search over the rebuildable Chat recall projection."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .common import connect_database


SEARCH_PAGE_LIMIT = 100
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


@dataclass(frozen=True)
class ChatRecallHit:
    """One stable transcript search target with a bounded preview."""

    recall_seq: int
    conversation_id: str
    turn_id: str
    message_id: str
    block_id: str
    role: str | None
    snippet: str
    created_at_us: int

    def as_dict(self) -> dict[str, object]:
        """Return the JSON-compatible wire representation."""
        return {
            "recall_seq": self.recall_seq,
            "conversation_id": self.conversation_id,
            "turn_id": self.turn_id,
            "message_id": self.message_id,
            "block_id": self.block_id,
            "role": self.role,
            "snippet": self.snippet,
            "created_at_us": self.created_at_us,
        }


@dataclass(frozen=True)
class ChatRecallSearchPage:
    """One newest-first keyset page of recall hits."""

    items: tuple[ChatRecallHit, ...]
    next_before_seq: int | None

    def as_dict(self) -> dict[str, object]:
        """Return the JSON-compatible response payload."""
        return {
            "items": [item.as_dict() for item in self.items],
            "next_before_seq": self.next_before_seq,
        }


def _fts_query(raw: str) -> str:
    """Convert plain user text into an implicit-AND FTS5 expression."""
    return " ".join(
        f'"{token.replace(chr(34), chr(34) * 2)}"'
        for token in _TOKEN_RE.findall(raw)
    )


def search_chat_recall(
    path: Path,
    *,
    query: str,
    before_seq: int | None = None,
    limit: int = 50,
) -> ChatRecallSearchPage:
    """Search Chat projections without opening source databases."""
    match = _fts_query(query.strip())
    if not match or not path.exists():
        return ChatRecallSearchPage(items=(), next_before_seq=None)
    page_limit = max(1, min(limit, SEARCH_PAGE_LIMIT))
    where = ["chat_recall_fts MATCH ?"]
    params: list[object] = [match]
    if before_seq is not None:
        where.append("d.recall_seq < ?")
        params.append(before_seq)
    params.append(page_limit + 1)
    statement = (
        "SELECT d.recall_seq, d.conversation_id, "
        "d.turn_id, d.message_id, d.block_id, d.created_at_us, "
        "ch.role, snippet(chat_recall_fts, 0, '', '', '…', 24) "
        "AS snippet FROM chat_recall_fts "
        "JOIN chat_recall_documents AS d "
        "ON d.row_id = chat_recall_fts.rowid "
        "LEFT JOIN conversation_history AS ch "
        "ON ch.conversation_id = d.conversation_id "
        "AND ch.turn_id = d.turn_id "
        "AND ch.message_id = d.message_id AND ch.block_id = d.block_id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY d.recall_seq DESC LIMIT ?"
    )
    try:
        with connect_database(path, readonly=True) as connection:
            rows = connection.execute(statement, params).fetchall()
    except sqlite3.OperationalError:
        # A rebuild can temporarily replace the derived FTS table. The UI can
        # safely retry after projection repair; never fall back to scanning
        # every transcript body on the request path.
        return ChatRecallSearchPage(items=(), next_before_seq=None)
    has_more = len(rows) > page_limit
    visible = rows[:page_limit]
    items = tuple(
        ChatRecallHit(
            recall_seq=int(row["recall_seq"]),
            conversation_id=str(row["conversation_id"]),
            turn_id=str(row["turn_id"]),
            message_id=str(row["message_id"]),
            block_id=str(row["block_id"]),
            role=str(row["role"]) if row["role"] is not None else None,
            snippet=str(row["snippet"]),
            created_at_us=int(row["created_at_us"]),
        )
        for row in visible
    )
    return ChatRecallSearchPage(
        items=items,
        next_before_seq=items[-1].recall_seq if has_more and items else None,
    )


__all__ = [
    "SEARCH_PAGE_LIMIT",
    "ChatRecallHit",
    "ChatRecallSearchPage",
    "search_chat_recall",
]
