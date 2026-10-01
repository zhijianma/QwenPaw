# -*- coding: utf-8 -*-
"""Content-free Lite index for stable semantic-observation pagination."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from uuid import UUID

from ..kernel import RuntimeObservation
from ..utils.io_utils import run_sync_io

_CURSOR_VERSION = 1
_MAX_CURSOR_LENGTH = 2048


class ObservationCursorError(ValueError):
    """Raised when an observation cursor is invalid for its conversation."""


@dataclass(frozen=True)
class ObservationIndexEntry:
    """Content-free pointer returned by the derived Lite index."""

    observation_id: str
    occurred_at: str


@dataclass(frozen=True)
class _ObservationCursor:
    """Validated cursor payload bound to one conversation snapshot."""

    watermark: int
    occurred_at: str
    observation_id: str


def _owner_key(conversation_id: str) -> str:
    return hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()


def _occurred_at(observation: RuntimeObservation) -> str:
    return observation.occurred_at.astimezone(timezone.utc).isoformat(
        timespec="microseconds",
    )


def _encode_cursor(
    *,
    owner_key: str,
    watermark: int,
    entry: ObservationIndexEntry,
) -> str:
    payload = {
        "v": _CURSOR_VERSION,
        "owner": owner_key,
        "watermark": watermark,
        "occurred_at": entry.occurred_at,
        "observation_id": entry.observation_id,
    }
    encoded = json.dumps(
        payload,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor: str, *, owner_key: str) -> _ObservationCursor:
    if not cursor or len(cursor) > _MAX_CURSOR_LENGTH:
        raise ObservationCursorError("invalid observation cursor")
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.b64decode(
            cursor + padding,
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ObservationCursorError("invalid observation cursor") from exc
    expected_keys = {
        "v",
        "owner",
        "watermark",
        "occurred_at",
        "observation_id",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise ObservationCursorError("invalid observation cursor")
    if (
        type(payload["v"]) is not int
        or payload["v"] != _CURSOR_VERSION
        or payload["owner"] != owner_key
    ):
        raise ObservationCursorError("observation cursor owner mismatch")
    watermark = payload["watermark"]
    occurred_at = payload["occurred_at"]
    observation_id = payload["observation_id"]
    if type(watermark) is not int or watermark < 1:
        raise ObservationCursorError("invalid observation cursor watermark")
    if not isinstance(occurred_at, str) or not occurred_at:
        raise ObservationCursorError("invalid observation cursor timestamp")
    if not isinstance(observation_id, str):
        raise ObservationCursorError("invalid observation cursor identity")
    try:
        UUID(observation_id)
    except ValueError as exc:
        raise ObservationCursorError(
            "invalid observation cursor identity",
        ) from exc
    return _ObservationCursor(
        watermark=watermark,
        occurred_at=occurred_at,
        observation_id=observation_id,
    )


class LiteObservationIndex:
    """Maintain pointers only; authoritative facts stay in source stores."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = Path(database_path)

    def _connect(self) -> sqlite3.Connection:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS observation_index ("
            "indexed_sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
            "owner_key TEXT NOT NULL, observation_id TEXT NOT NULL, "
            "occurred_at TEXT NOT NULL, source_type TEXT NOT NULL, "
            "source_id TEXT NOT NULL, "
            "UNIQUE(owner_key, observation_id))",
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_observation_page "
            "ON observation_index(owner_key, occurred_at DESC, "
            "observation_id DESC)",
        )
        return connection

    def _sync_and_page_sync(
        self,
        conversation_id: str,
        observations: tuple[RuntimeObservation, ...],
        limit: int,
        cursor: str | None,
    ) -> tuple[tuple[ObservationIndexEntry, ...], str | None]:
        owner = _owner_key(conversation_id)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                "INSERT OR IGNORE INTO observation_index "
                "(owner_key, observation_id, occurred_at, source_type, "
                "source_id) VALUES (?, ?, ?, ?, ?)",
                (
                    (
                        owner,
                        str(item.observation_id),
                        _occurred_at(item),
                        item.source.source_type,
                        item.source.source_id,
                    )
                    for item in observations
                ),
            )
            connection.execute(
                "CREATE TEMP TABLE IF NOT EXISTS current_observation_ids ("
                "observation_id TEXT PRIMARY KEY)",
            )
            connection.execute("DELETE FROM current_observation_ids")
            connection.executemany(
                "INSERT INTO current_observation_ids (observation_id) "
                "VALUES (?)",
                ((str(item.observation_id),) for item in observations),
            )
            connection.execute(
                "DELETE FROM observation_index WHERE owner_key = ? "
                "AND observation_id NOT IN ("
                "SELECT observation_id FROM current_observation_ids)",
                (owner,),
            )
            row = connection.execute(
                "SELECT COALESCE(MAX(indexed_sequence), 0) AS watermark "
                "FROM observation_index WHERE owner_key = ?",
                (owner,),
            ).fetchone()
            current_watermark = int(row["watermark"])
            payload = (
                _decode_cursor(cursor, owner_key=owner)
                if cursor is not None
                else None
            )
            watermark = (
                payload.watermark if payload is not None else current_watermark
            )
            if watermark > current_watermark:
                raise ObservationCursorError(
                    "observation cursor watermark is not available",
                )
            clauses = ["owner_key = ?", "indexed_sequence <= ?"]
            arguments: list[object] = [owner, watermark]
            if payload is not None:
                occurred_at = payload.occurred_at
                observation_id = payload.observation_id
                anchor = connection.execute(
                    "SELECT 1 FROM observation_index WHERE owner_key = ? "
                    "AND indexed_sequence <= ? AND occurred_at = ? "
                    "AND observation_id = ?",
                    (owner, watermark, occurred_at, observation_id),
                ).fetchone()
                if anchor is None:
                    raise ObservationCursorError(
                        "observation cursor anchor is not available",
                    )
                clauses.append(
                    "(occurred_at < ? OR (occurred_at = ? "
                    "AND observation_id < ?))",
                )
                arguments.extend(
                    (occurred_at, occurred_at, observation_id),
                )
            arguments.append(limit + 1)
            rows = connection.execute(
                "SELECT observation_id, occurred_at "
                "FROM observation_index WHERE "
                + " AND ".join(clauses)
                + " ORDER BY occurred_at DESC, observation_id DESC "
                "LIMIT ?",
                arguments,
            ).fetchall()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        has_more = len(rows) > limit
        entries = tuple(
            ObservationIndexEntry(
                observation_id=str(row["observation_id"]),
                occurred_at=str(row["occurred_at"]),
            )
            for row in rows[:limit]
        )
        next_cursor = (
            _encode_cursor(
                owner_key=owner,
                watermark=watermark,
                entry=entries[-1],
            )
            if has_more and entries
            else None
        )
        return entries, next_cursor

    async def sync_and_page(
        self,
        conversation_id: str,
        observations: tuple[RuntimeObservation, ...],
        *,
        limit: int,
        cursor: str | None,
    ) -> tuple[tuple[ObservationIndexEntry, ...], str | None]:
        """Index current pointers and return one fixed-watermark page."""
        return await run_sync_io(
            self._sync_and_page_sync,
            conversation_id,
            observations,
            limit,
            cursor,
        )


__all__ = [
    "LiteObservationIndex",
    "ObservationCursorError",
    "ObservationIndexEntry",
]
