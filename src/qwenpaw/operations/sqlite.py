# -*- coding: utf-8 -*-
"""Lite SQLite source store for durable operational facts."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from uuid import UUID

from ..kernel import (
    OperationalEvent,
    OperationalEventConflictError,
)


class SQLiteOperationalEventStore:
    """Persist immutable Agent-scoped operational events."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = Path(database_path)
        self._prepare_lock = asyncio.Lock()
        self._prepared = False

    async def commit(self, event: OperationalEvent) -> OperationalEvent:
        """Commit one event or replay its identical persisted fact."""
        await self._prepare()
        return await asyncio.to_thread(self._commit_sync, event)

    async def get(self, event_id: UUID) -> OperationalEvent | None:
        """Return one immutable event by ID."""
        await self._prepare()
        return await asyncio.to_thread(self._get_sync, event_id)

    async def list_events(
        self,
        *,
        agent_id: str,
        producer_id: str | None = None,
        limit: int = 100,
    ) -> tuple[OperationalEvent, ...]:
        """Return newest events for one Agent and optional producer."""
        if limit < 1 or limit > 5_000:
            raise ValueError("Operational event limit must be 1..5000")
        await self._prepare()
        return await asyncio.to_thread(
            self._list_sync,
            agent_id,
            producer_id,
            limit,
        )

    async def _prepare(self) -> None:
        if self._prepared:
            return
        async with self._prepare_lock:
            if self._prepared:
                return
            await asyncio.to_thread(self._prepare_sync)
            self._prepared = True

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database_path,
            timeout=10.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _prepare_sync(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS operational_events (
                    event_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    producer_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    data TEXT NOT NULL
                )
                """,
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS operational_agent_time
                ON operational_events(
                    agent_id, occurred_at DESC, event_id DESC
                )
                """,
            )

    def _commit_sync(self, event: OperationalEvent) -> OperationalEvent:
        event_id = event.event_id
        if event_id is None:  # pragma: no cover - Kernel validator
            raise ValueError("Operational event has no identity")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data FROM operational_events WHERE event_id = ?",
                (str(event_id),),
            ).fetchone()
            if row is not None:
                existing = OperationalEvent.model_validate_json(row["data"])
                if not existing.same_fact(event):
                    raise OperationalEventConflictError(
                        "operational idempotency identity conflicts",
                    )
                connection.commit()
                return existing
            connection.execute(
                """
                INSERT INTO operational_events(
                    event_id, agent_id, producer_id, occurred_at, data
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(event_id),
                    event.agent_id,
                    event.producer_id,
                    event.occurred_at.isoformat(),
                    event.model_dump_json(),
                ),
            )
            connection.commit()
        return event

    def _get_sync(self, event_id: UUID) -> OperationalEvent | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM operational_events WHERE event_id = ?",
                (str(event_id),),
            ).fetchone()
        return (
            OperationalEvent.model_validate_json(row["data"])
            if row is not None
            else None
        )

    def _list_sync(
        self,
        agent_id: str,
        producer_id: str | None,
        limit: int,
    ) -> tuple[OperationalEvent, ...]:
        query = "SELECT data FROM operational_events WHERE agent_id = ?"
        values: list[object] = [agent_id]
        if producer_id is not None:
            query += " AND producer_id = ?"
            values.append(producer_id)
        query += " ORDER BY occurred_at DESC, event_id DESC LIMIT ?"
        values.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return tuple(
            OperationalEvent.model_validate_json(row["data"]) for row in rows
        )


__all__ = ["SQLiteOperationalEventStore"]
