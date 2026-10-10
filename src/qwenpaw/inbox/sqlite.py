# -*- coding: utf-8 -*-
"""Lite SQLite Inbox projection over terminal Delivery receipts."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from uuid import UUID

from ..kernel import (
    DeliveryReceipt,
    DeliveryRequest,
    InboxItem,
    InboxItemNotFoundError,
    InboxProjectionConflictError,
)
from ..kernel.models import utc_now

_TITLES = {
    "reply": "Agent reply",
    "result": "Task completed",
    "approval": "Approval required",
    "exception": "Task failed",
    "artifact_ready": "Artifact ready",
}


class SQLiteInboxProjectionStore:
    """Persist Delivery-backed items and local read/handled markers."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = Path(database_path)
        self._prepare_lock = asyncio.Lock()
        self._prepared = False

    async def project(
        self,
        request: DeliveryRequest,
        receipt: DeliveryReceipt,
    ) -> InboxItem:
        if request.delivery_id != receipt.delivery_id:
            raise InboxProjectionConflictError(
                "Inbox Delivery identity does not match receipt",
            )
        await self._prepare()
        return await asyncio.to_thread(
            self._project_sync,
            request,
            receipt,
        )

    async def get(self, item_id: UUID) -> InboxItem | None:
        await self._prepare()
        return await asyncio.to_thread(self._get_sync, item_id)

    async def list_items(
        self,
        *,
        agent_id: str,
        unread_only: bool = False,
        limit: int = 50,
        include_handled: bool = False,
    ) -> tuple[InboxItem, ...]:
        if limit < 1 or limit > 5_000:
            raise ValueError("Inbox limit must be between 1 and 5000")
        await self._prepare()
        return await asyncio.to_thread(
            self._list_sync,
            agent_id,
            unread_only,
            limit,
            include_handled,
        )

    async def mark_read(
        self,
        item_id: UUID,
        *,
        agent_id: str,
        expected_revision: int,
    ) -> InboxItem:
        return await self._mark(
            item_id,
            agent_id=agent_id,
            expected_revision=expected_revision,
            field="read_at",
        )

    async def mark_handled(
        self,
        item_id: UUID,
        *,
        agent_id: str,
        expected_revision: int,
    ) -> InboxItem:
        return await self._mark(
            item_id,
            agent_id=agent_id,
            expected_revision=expected_revision,
            field="handled_at",
        )

    async def _mark(
        self,
        item_id: UUID,
        *,
        agent_id: str,
        expected_revision: int,
        field: str,
    ) -> InboxItem:
        await self._prepare()
        return await asyncio.to_thread(
            self._mark_sync,
            item_id,
            agent_id,
            expected_revision,
            field,
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
        return connection

    def _prepare_sync(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS inbox_items (
                    item_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    read_at TEXT,
                    handled_at TEXT,
                    created_at TEXT NOT NULL,
                    data TEXT NOT NULL
                )
                """,
            )
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(inbox_items)",
                ).fetchall()
            }
            if "handled_at" not in columns:
                connection.execute(
                    "ALTER TABLE inbox_items ADD COLUMN handled_at TEXT",
                )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS inbox_agent_created
                ON inbox_items(agent_id, created_at DESC)
                """,
            )

    def _project_sync(
        self,
        request: DeliveryRequest,
        receipt: DeliveryReceipt,
    ) -> InboxItem:
        delivery_id = request.delivery_id
        if delivery_id is None:  # pragma: no cover - Kernel validator
            raise ValueError("Delivery request has no identity")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data FROM inbox_items WHERE item_id = ?",
                (str(delivery_id),),
            ).fetchone()
            existing = (
                InboxItem.model_validate_json(row["data"])
                if row is not None
                else None
            )
            if existing is not None:
                self._validate_existing(existing, request, receipt)
                if receipt.attempt == existing.delivery_attempt:
                    connection.commit()
                    return existing
            text = request.payload.get("text")
            summary = text[:2_000] if isinstance(text, str) else ""
            source_type = self._payload_text(
                request,
                "source_type",
                "task",
            )
            source_id = self._payload_text(
                request,
                "source_id",
                str(request.task_id or delivery_id),
            )
            event_type = self._payload_text(
                request,
                "event_type",
                request.kind.value,
            )
            source_status = self._payload_text(
                request,
                "source_status",
                (
                    "error"
                    if receipt.status.value in {"failed", "uncertain"}
                    else "success"
                ),
            )
            severity = self._payload_text(
                request,
                "severity",
                "error" if source_status == "error" else "info",
            )
            title = self._payload_text(
                request,
                "title",
                _TITLES[request.kind.value],
            )[:500]
            source_payload = self._source_payload(request)
            item = InboxItem(
                item_id=delivery_id,
                delivery_id=delivery_id,
                source_event_id=request.source_event_id,
                agent_id=request.agent_id,
                source_type=source_type,
                source_id=source_id,
                event_type=event_type,
                source_status=source_status,
                severity=severity,
                kind=request.kind,
                delivery_status=receipt.status,
                delivery_attempt=receipt.attempt,
                chat_id=request.chat_id,
                task_id=request.task_id,
                run_id=request.run_id,
                invocation_id=request.invocation_id,
                correlation_id=request.correlation_id,
                artifact_refs=request.artifact_refs,
                evidence_refs=request.evidence_refs,
                source_payload=source_payload,
                title=title,
                summary=summary,
                revision=(existing.revision + 1 if existing else 1),
                read_at=existing.read_at if existing else None,
                handled_at=existing.handled_at if existing else None,
                created_at=(
                    existing.created_at if existing else request.created_at
                ),
                updated_at=receipt.finished_at,
            )
            connection.execute(
                """
                INSERT INTO inbox_items(
                    item_id, agent_id, read_at, handled_at, created_at, data
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    agent_id = excluded.agent_id,
                    read_at = excluded.read_at,
                    handled_at = excluded.handled_at,
                    data = excluded.data
                """,
                self._row(item),
            )
            connection.commit()
        return item

    @staticmethod
    def _payload_text(
        request: DeliveryRequest,
        key: str,
        default: str,
    ) -> str:
        value = request.payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return default

    @staticmethod
    def _source_payload(request: DeliveryRequest) -> dict[str, object]:
        value = request.payload.get("operational_payload")
        if isinstance(value, dict):
            return value
        return {}

    def _validate_existing(
        self,
        existing: InboxItem,
        request: DeliveryRequest,
        receipt: DeliveryReceipt,
    ) -> None:
        source = (
            existing.source_event_id,
            existing.agent_id,
            existing.source_type,
            existing.source_id,
            existing.event_type,
            existing.kind,
            existing.chat_id,
            existing.task_id,
            existing.run_id,
            existing.invocation_id,
            existing.correlation_id,
            existing.source_payload,
            existing.title,
            existing.summary,
        )
        incoming = (
            request.source_event_id,
            request.agent_id,
            self._payload_text(request, "source_type", "task"),
            self._payload_text(
                request,
                "source_id",
                str(request.task_id or request.delivery_id),
            ),
            self._payload_text(
                request,
                "event_type",
                request.kind.value,
            ),
            request.kind,
            request.chat_id,
            request.task_id,
            request.run_id,
            request.invocation_id,
            request.correlation_id,
            self._source_payload(request),
            self._payload_text(
                request,
                "title",
                _TITLES[request.kind.value],
            )[:500],
            (
                request.payload.get("text", "")[:2_000]
                if isinstance(request.payload.get("text"), str)
                else ""
            ),
        )
        if source != incoming or receipt.attempt < existing.delivery_attempt:
            raise InboxProjectionConflictError(
                "Inbox item conflicts with existing source facts",
            )
        if (
            receipt.attempt == existing.delivery_attempt
            and receipt.status is not existing.delivery_status
        ):
            raise InboxProjectionConflictError(
                "Inbox receipt conflicts with existing attempt",
            )

    def _get_sync(self, item_id: UUID) -> InboxItem | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM inbox_items WHERE item_id = ?",
                (str(item_id),),
            ).fetchone()
        return InboxItem.model_validate_json(row["data"]) if row else None

    def _list_sync(
        self,
        agent_id: str,
        unread_only: bool,
        limit: int,
        include_handled: bool,
    ) -> tuple[InboxItem, ...]:
        query = "SELECT data FROM inbox_items WHERE agent_id = ?"
        values: list[object] = [agent_id]
        if unread_only:
            query += " AND read_at IS NULL"
        if not include_handled:
            query += " AND handled_at IS NULL"
        query += " ORDER BY created_at DESC, item_id DESC LIMIT ?"
        values.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return tuple(
            InboxItem.model_validate_json(row["data"]) for row in rows
        )

    def _mark_sync(
        self,
        item_id: UUID,
        agent_id: str,
        expected_revision: int,
        field: str,
    ) -> InboxItem:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data FROM inbox_items WHERE item_id = ?",
                (str(item_id),),
            ).fetchone()
            if row is None:
                raise InboxItemNotFoundError(str(item_id))
            item = InboxItem.model_validate_json(row["data"])
            if item.agent_id != agent_id:
                raise InboxItemNotFoundError(str(item_id))
            if item.revision != expected_revision:
                raise InboxProjectionConflictError(
                    "Inbox item revision does not match",
                )
            now = utc_now()
            updated = item.model_copy(
                update={
                    field: now,
                    "updated_at": now,
                    "revision": item.revision + 1,
                },
            )
            connection.execute(
                """
                UPDATE inbox_items
                SET read_at = ?, handled_at = ?, data = ?
                WHERE item_id = ?
                """,
                (
                    updated.read_at.isoformat() if updated.read_at else None,
                    (
                        updated.handled_at.isoformat()
                        if updated.handled_at
                        else None
                    ),
                    updated.model_dump_json(),
                    str(item_id),
                ),
            )
            connection.commit()
            return updated

    @staticmethod
    def _row(
        item: InboxItem,
    ) -> tuple[str, str, str | None, str | None, str, str]:
        return (
            str(item.item_id),
            item.agent_id,
            item.read_at.isoformat() if item.read_at else None,
            item.handled_at.isoformat() if item.handled_at else None,
            item.created_at.isoformat(),
            item.model_dump_json(),
        )


__all__ = ["SQLiteInboxProjectionStore"]
