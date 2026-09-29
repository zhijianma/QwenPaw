# -*- coding: utf-8 -*-
"""Lite SQLite implementation of the durable Delivery Projection Port."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from ..kernel.delivery import (
    DeliveryAttempt,
    DeliveryAttemptConflictError,
    DeliveryAttemptNotFoundError,
    DeliveryAttemptStatus,
    DeliveryReceipt,
    DeliveryRequest,
    DeliveryRequestConflictError,
    DeliveryStatus,
)


def _canonical_json(model: object) -> str:
    model_dump = getattr(model, "model_dump")
    return json.dumps(
        model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("delivery datetimes must be timezone-aware")
    return value.astimezone(timezone.utc)


class SQLiteDeliveryProjectionStore:
    """Persist immutable requests and explicit revisioned attempts."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = Path(database_path)
        self._prepare_lock = asyncio.Lock()
        self._prepared = False

    async def claim(
        self,
        request: DeliveryRequest,
        *,
        attempt: int,
        owner_id: str,
        lease_seconds: float,
    ) -> DeliveryAttempt:
        self._validate_claim_input(attempt, owner_id, lease_seconds)
        await self._prepare()
        return await asyncio.to_thread(
            self._claim_sync,
            request,
            attempt,
            owner_id,
            lease_seconds,
        )

    async def renew(
        self,
        delivery_id: UUID,
        *,
        attempt: int,
        owner_id: str,
        expected_revision: int,
        lease_seconds: float,
    ) -> DeliveryAttempt:
        self._validate_claim_input(attempt, owner_id, lease_seconds)
        await self._prepare()
        return await asyncio.to_thread(
            self._renew_sync,
            delivery_id,
            attempt,
            owner_id,
            expected_revision,
            lease_seconds,
        )

    async def settle(
        self,
        receipt: DeliveryReceipt,
        *,
        owner_id: str,
        expected_revision: int,
    ) -> DeliveryAttempt:
        if not owner_id.strip():
            raise ValueError("delivery owner_id must not be empty")
        await self._prepare()
        return await asyncio.to_thread(
            self._settle_sync,
            receipt,
            owner_id,
            expected_revision,
        )

    async def get_request(
        self,
        delivery_id: UUID,
    ) -> DeliveryRequest | None:
        await self._prepare()
        return await asyncio.to_thread(self._get_request_sync, delivery_id)

    async def list_attempts(
        self,
        delivery_id: UUID,
    ) -> tuple[DeliveryAttempt, ...]:
        await self._prepare()
        return await asyncio.to_thread(self._list_attempts_sync, delivery_id)

    async def recover_expired(
        self,
        *,
        agent_id: str,
        now: datetime,
    ) -> tuple[DeliveryAttempt, ...]:
        await self._prepare()
        return await asyncio.to_thread(
            self._recover_expired_sync,
            agent_id,
            _utc(now),
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
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS delivery_requests (
                    delivery_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    adapter_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_json TEXT NOT NULL
                )
                """,
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS delivery_attempts (
                    delivery_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    owner_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    expires_at TEXT NOT NULL,
                    attempt_json TEXT NOT NULL,
                    PRIMARY KEY(delivery_id, attempt),
                    FOREIGN KEY(delivery_id)
                        REFERENCES delivery_requests(delivery_id)
                )
                """,
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_delivery_expiry
                ON delivery_attempts(status, expires_at)
                """,
            )
            connection.commit()

    def _claim_sync(
        self,
        request: DeliveryRequest,
        attempt: int,
        owner_id: str,
        lease_seconds: float,
    ) -> DeliveryAttempt:
        delivery_id = request.delivery_id
        if delivery_id is None:  # pragma: no cover - Kernel validator
            raise ValueError("delivery request has no stable identity")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_request = connection.execute(
                """
                SELECT request_json FROM delivery_requests
                WHERE delivery_id = ?
                """,
                (str(delivery_id),),
            ).fetchone()
            if existing_request is None:
                connection.execute(
                    """
                    INSERT INTO delivery_requests (
                        delivery_id, agent_id, adapter_id,
                        idempotency_key, request_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        str(delivery_id),
                        request.agent_id,
                        request.destination.adapter_id,
                        request.idempotency_key,
                        _canonical_json(request),
                    ),
                )
            else:
                stored_request = DeliveryRequest.model_validate_json(
                    existing_request["request_json"],
                )
                if not stored_request.same_projection(request):
                    connection.rollback()
                    raise DeliveryRequestConflictError(
                        "delivery idempotency key has conflicting content",
                    )

            rows = connection.execute(
                """
                SELECT attempt_json FROM delivery_attempts
                WHERE delivery_id = ? ORDER BY attempt
                """,
                (str(delivery_id),),
            ).fetchall()
            attempts = tuple(
                DeliveryAttempt.model_validate_json(row["attempt_json"])
                for row in rows
            )
            existing = next(
                (item for item in attempts if item.attempt == attempt),
                None,
            )
            if existing is not None:
                connection.rollback()
                return existing
            if any(
                item.status
                in {
                    DeliveryAttemptStatus.DELIVERED,
                    DeliveryAttemptStatus.SUPPRESSED,
                }
                for item in attempts
            ):
                connection.rollback()
                raise DeliveryAttemptConflictError(
                    "settled delivery cannot create another attempt",
                )
            expected_attempt = len(attempts) + 1
            if attempt != expected_attempt:
                connection.rollback()
                raise DeliveryAttemptConflictError(
                    f"delivery attempt must be {expected_attempt}",
                )
            if (
                attempts
                and attempts[-1].status is not DeliveryAttemptStatus.FAILED
            ):
                connection.rollback()
                raise DeliveryAttemptConflictError(
                    "next delivery attempt requires prior failure",
                )
            now = datetime.now(timezone.utc)
            claimed = DeliveryAttempt(
                delivery_id=delivery_id,
                attempt=attempt,
                owner_id=owner_id,
                acquired_at=now,
                expires_at=now + timedelta(seconds=lease_seconds),
            )
            connection.execute(
                """
                INSERT INTO delivery_attempts (
                    delivery_id, attempt, owner_id, status,
                    revision, expires_at, attempt_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                self._attempt_row(claimed),
            )
            connection.commit()
        return claimed

    def _renew_sync(
        self,
        delivery_id: UUID,
        attempt: int,
        owner_id: str,
        expected_revision: int,
        lease_seconds: float,
    ) -> DeliveryAttempt:
        now = datetime.now(timezone.utc)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._claimed_for_update(
                connection,
                delivery_id,
                attempt,
                owner_id,
                expected_revision,
                now,
            )
            updated = current.model_copy(
                update={
                    "revision": current.revision + 1,
                    "expires_at": now + timedelta(seconds=lease_seconds),
                },
            )
            self._write_attempt(connection, updated)
            connection.commit()
        return updated

    def _settle_sync(
        self,
        receipt: DeliveryReceipt,
        owner_id: str,
        expected_revision: int,
    ) -> DeliveryAttempt:
        now = datetime.now(timezone.utc)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            request = self._request_for_update(
                connection,
                receipt.delivery_id,
            )
            if receipt.adapter_id != request.destination.adapter_id:
                connection.rollback()
                raise DeliveryAttemptConflictError(
                    "delivery receipt adapter does not match destination",
                )
            current = self._claimed_for_update(
                connection,
                receipt.delivery_id,
                receipt.attempt,
                owner_id,
                expected_revision,
                now,
            )
            updated = DeliveryAttempt.model_validate(
                {
                    **current.model_dump(),
                    "status": DeliveryAttemptStatus(receipt.status.value),
                    "revision": current.revision + 1,
                    "receipt": receipt,
                },
            )
            self._write_attempt(connection, updated)
            connection.commit()
        return updated

    def _recover_expired_sync(
        self,
        agent_id: str,
        now: datetime,
    ) -> tuple[DeliveryAttempt, ...]:
        recovered: list[DeliveryAttempt] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT a.attempt_json, r.request_json
                FROM delivery_attempts AS a
                JOIN delivery_requests AS r
                    ON r.delivery_id = a.delivery_id
                WHERE r.agent_id = ? AND a.status = ?
                    AND a.expires_at <= ?
                ORDER BY a.expires_at, a.delivery_id, a.attempt
                """,
                (
                    agent_id,
                    DeliveryAttemptStatus.CLAIMED.value,
                    now.isoformat(),
                ),
            ).fetchall()
            for row in rows:
                current = DeliveryAttempt.model_validate_json(
                    row["attempt_json"],
                )
                request = DeliveryRequest.model_validate_json(
                    row["request_json"],
                )
                receipt = DeliveryReceipt(
                    delivery_id=current.delivery_id,
                    adapter_id=request.destination.adapter_id,
                    status=DeliveryStatus.UNCERTAIN,
                    attempt=current.attempt,
                    finished_at=now,
                    error_code="lease_expired_outcome_unknown",
                )
                updated = DeliveryAttempt.model_validate(
                    {
                        **current.model_dump(),
                        "status": DeliveryAttemptStatus.UNCERTAIN,
                        "revision": current.revision + 1,
                        "receipt": receipt,
                    },
                )
                self._write_attempt(connection, updated)
                recovered.append(updated)
            connection.commit()
        return tuple(recovered)

    def _get_request_sync(
        self,
        delivery_id: UUID,
    ) -> DeliveryRequest | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT request_json FROM delivery_requests
                WHERE delivery_id = ?
                """,
                (str(delivery_id),),
            ).fetchone()
        return (
            DeliveryRequest.model_validate_json(row["request_json"])
            if row is not None
            else None
        )

    def _list_attempts_sync(
        self,
        delivery_id: UUID,
    ) -> tuple[DeliveryAttempt, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT attempt_json FROM delivery_attempts
                WHERE delivery_id = ? ORDER BY attempt
                """,
                (str(delivery_id),),
            ).fetchall()
        return tuple(
            DeliveryAttempt.model_validate_json(row["attempt_json"])
            for row in rows
        )

    @staticmethod
    def _validate_claim_input(
        attempt: int,
        owner_id: str,
        lease_seconds: float,
    ) -> None:
        if attempt < 1:
            raise ValueError("delivery attempt must be positive")
        if not owner_id.strip():
            raise ValueError("delivery owner_id must not be empty")
        if lease_seconds <= 0:
            raise ValueError("delivery lease duration must be positive")

    def _request_for_update(
        self,
        connection: sqlite3.Connection,
        delivery_id: UUID,
    ) -> DeliveryRequest:
        row = connection.execute(
            """
            SELECT request_json FROM delivery_requests
            WHERE delivery_id = ?
            """,
            (str(delivery_id),),
        ).fetchone()
        if row is None:
            raise DeliveryAttemptNotFoundError(str(delivery_id))
        return DeliveryRequest.model_validate_json(row["request_json"])

    def _claimed_for_update(
        self,
        connection: sqlite3.Connection,
        delivery_id: UUID,
        attempt: int,
        owner_id: str,
        expected_revision: int,
        now: datetime,
    ) -> DeliveryAttempt:
        row = connection.execute(
            """
            SELECT attempt_json FROM delivery_attempts
            WHERE delivery_id = ? AND attempt = ?
            """,
            (str(delivery_id), attempt),
        ).fetchone()
        if row is None:
            raise DeliveryAttemptNotFoundError(
                f"{delivery_id}:{attempt}",
            )
        current = DeliveryAttempt.model_validate_json(row["attempt_json"])
        if current.status is not DeliveryAttemptStatus.CLAIMED:
            raise DeliveryAttemptConflictError(
                "delivery attempt is already terminal",
            )
        if current.owner_id != owner_id:
            raise DeliveryAttemptConflictError(
                "delivery attempt belongs to another owner",
            )
        if current.revision != expected_revision:
            raise DeliveryAttemptConflictError(
                "delivery attempt revision does not match",
            )
        if current.expires_at <= now:
            raise DeliveryAttemptConflictError(
                "delivery attempt lease has expired",
            )
        return current

    @staticmethod
    def _attempt_row(attempt: DeliveryAttempt) -> tuple[object, ...]:
        return (
            str(attempt.delivery_id),
            attempt.attempt,
            attempt.owner_id,
            attempt.status.value,
            attempt.revision,
            attempt.expires_at.isoformat(),
            _canonical_json(attempt),
        )

    def _write_attempt(
        self,
        connection: sqlite3.Connection,
        attempt: DeliveryAttempt,
    ) -> None:
        cursor = connection.execute(
            """
            UPDATE delivery_attempts SET
                owner_id = ?, status = ?, revision = ?,
                expires_at = ?, attempt_json = ?
            WHERE delivery_id = ? AND attempt = ?
            """,
            (
                attempt.owner_id,
                attempt.status.value,
                attempt.revision,
                attempt.expires_at.isoformat(),
                _canonical_json(attempt),
                str(attempt.delivery_id),
                attempt.attempt,
            ),
        )
        if cursor.rowcount != 1:
            raise DeliveryAttemptNotFoundError(
                f"{attempt.delivery_id}:{attempt.attempt}",
            )


__all__ = ["SQLiteDeliveryProjectionStore"]
