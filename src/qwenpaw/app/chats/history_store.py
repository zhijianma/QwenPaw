# -*- coding: utf-8 -*-
"""Storage-independent reads for user-visible conversation history."""

from __future__ import annotations

import base64
import sqlite3
import struct
from dataclasses import dataclass
from typing import Protocol

from ...schemas import Message
from .transcript import TranscriptCursor

_CURSOR = struct.Struct(">QQQ")
_SNAPSHOT = struct.Struct(">Q")


@dataclass(frozen=True)
class ConversationHistoryPage:
    """One stable, byte-bounded page in chronological display order."""

    messages: list[Message]
    next_before: str | None
    has_more: bool
    snapshot: str


class ConversationHistoryStore(Protocol):
    """Read contract independent from a concrete persistence engine."""

    def get_page(
        self,
        *,
        session_id: str,
        user_id: str,
        channel: str,
        before: str | None = None,
        limit: int = 20,
        max_bytes: int = 2 * 1024 * 1024,
    ) -> ConversationHistoryPage | None:
        """Read one stable page using an opaque continuation cursor."""


class ConversationHistoryError(RuntimeError):
    """A history backend failed after its request was validated."""


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(
            f"{value}{padding}",
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid history cursor") from exc


def _encode_cursor(value: TranscriptCursor | None) -> str | None:
    if value is None:
        return None
    snapshot = value.snapshot_turn_seq
    if snapshot is None:
        raise ValueError("history cursor is missing its snapshot")
    return _encode(_CURSOR.pack(snapshot, value.turn_seq, value.ordinal))


def _decode_cursor(value: str | None) -> TranscriptCursor | None:
    if value is None:
        return None
    raw = _decode(value)
    if len(raw) != _CURSOR.size:
        raise ValueError("invalid history cursor")
    snapshot, turn_seq, ordinal = _CURSOR.unpack(raw)
    if snapshot < 1 or turn_seq < 1 or turn_seq > snapshot:
        raise ValueError("invalid history cursor")
    return TranscriptCursor(
        turn_seq=turn_seq,
        ordinal=ordinal,
        snapshot_turn_seq=snapshot,
    )


class TranscriptHistoryStore:
    """Adapt the current transcript catalog to the public read contract."""

    def __init__(self, transcript_store) -> None:
        self._transcript_store = transcript_store

    def get_page(
        self,
        *,
        session_id: str,
        user_id: str,
        channel: str,
        before: str | None = None,
        limit: int = 20,
        max_bytes: int = 2 * 1024 * 1024,
    ) -> ConversationHistoryPage | None:
        cursor = _decode_cursor(before)
        try:
            page = self._transcript_store.get_page(
                session_id=session_id,
                user_id=user_id,
                channel=channel,
                before=cursor,
                limit=limit,
                max_bytes=max_bytes,
            )
        except (OSError, sqlite3.Error, TypeError) as exc:
            raise ConversationHistoryError("history read failed") from exc
        if page is None:
            return None
        return ConversationHistoryPage(
            messages=page.messages,
            next_before=_encode_cursor(page.next_before),
            has_more=page.has_more,
            snapshot=_encode(_SNAPSHOT.pack(page.snapshot_turn_seq)),
        )


__all__ = [
    "ConversationHistoryPage",
    "ConversationHistoryError",
    "ConversationHistoryStore",
    "TranscriptHistoryStore",
]
