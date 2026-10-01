# -*- coding: utf-8 -*-
"""Durable workspace broker for all runtime-to-user interactions."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from ..kernel import (
    ContinuationAvailability,
    ContinuationDispatchStatus,
    ContinuationMode,
    ContinuationRef,
    ConversationContinuation,
    InteractionKind,
    InteractionMode,
    InteractionRecord,
    InteractionRequest,
    InteractionResolution,
    InteractionResponse,
    InteractionStatus,
    WaitCondition,
    WaitConditionKind,
    WaitConditionStatus,
)
from ..kernel.models import utc_now

INTERACTION_SCHEMA_VERSION = 2
logger = logging.getLogger(__name__)

InteractionTerminalHook = Callable[
    [InteractionResolution],
    Awaitable[None],
]


class InteractionError(RuntimeError):
    """Base class for interaction infrastructure failures."""


class InteractionNotFoundError(InteractionError):
    """Raised when an interaction identity is unknown."""


class InteractionConflictError(InteractionError):
    """Raised when an interaction is no longer open."""


class InteractionRevisionConflictError(InteractionConflictError):
    """Raised when optimistic interaction concurrency is stale."""

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"interaction revision conflict: expected {expected}, "
            f"actual {actual}",
        )


class InteractionIdempotencyConflictError(InteractionConflictError):
    """Raised when an idempotency key is reused with another response."""


def _canonical_json(model: Any) -> str:
    return json.dumps(
        model.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


class InteractionService:
    """SQLite source of truth plus live waiters for one workspace."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self._initialize_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._initialized = False
        self._waiters: dict[
            UUID,
            set[asyncio.Future[InteractionResolution]],
        ] = {}
        self._terminal_hooks: dict[
            UUID,
            list[InteractionTerminalHook],
        ] = {}

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_sync(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            version = int(
                connection.execute("PRAGMA user_version").fetchone()[0],
            )
            if version not in {0, 1, INTERACTION_SCHEMA_VERSION}:
                raise InteractionError(
                    f"unsupported interaction schema: {version}",
                )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runtime_interactions (
                    interaction_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    invocation_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    request_json TEXT NOT NULL,
                    response_idempotency_key TEXT,
                    response_json TEXT,
                    resolution_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_interactions_conversation
                    ON runtime_interactions(
                        agent_id,
                        conversation_id,
                        status,
                        created_at
                    );
                CREATE INDEX IF NOT EXISTS idx_interactions_invocation
                    ON runtime_interactions(
                        invocation_id,
                        status,
                        created_at
                    );
                CREATE TABLE IF NOT EXISTS interaction_continuations (
                    interaction_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    response_revision INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    submission_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_continuations_ready
                    ON interaction_continuations(
                        agent_id,
                        status,
                        created_at
                    );
                """,
            )
            connection.execute(
                f"PRAGMA user_version = {INTERACTION_SCHEMA_VERSION}",
            )

    async def start(self) -> None:
        """Initialize the durable store once."""
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            await asyncio.to_thread(self._initialize_sync)
            self._initialized = True

    async def close(self) -> None:
        """Release live waiters without mutating durable interaction state."""
        waiters = [
            waiter for group in self._waiters.values() for waiter in group
        ]
        self._waiters.clear()
        self._terminal_hooks.clear()
        for waiter in waiters:
            if not waiter.done():
                waiter.cancel()

    async def open(
        self,
        request: InteractionRequest,
    ) -> InteractionRequest:
        """Persist one open interaction before any adapter exposes it."""
        if request.status is not InteractionStatus.OPEN:
            raise InteractionConflictError("new interaction must be open")
        await self.start()
        request_json = _canonical_json(request)
        async with self._write_lock:
            return await asyncio.to_thread(
                self._open_sync,
                request,
                request_json,
            )

    def _open_sync(
        self,
        request: InteractionRequest,
        request_json: str,
    ) -> InteractionRequest:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT request_json FROM runtime_interactions "
                "WHERE interaction_id = ?",
                (str(request.interaction_id),),
            ).fetchone()
            if row is not None:
                if row["request_json"] != request_json:
                    raise InteractionIdempotencyConflictError(
                        "interaction identity has conflicting content",
                    )
                return request
            now = utc_now().isoformat()
            connection.execute(
                "INSERT INTO runtime_interactions "
                "(interaction_id, agent_id, conversation_id, invocation_id, "
                "mode, status, revision, request_json, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(request.interaction_id),
                    request.agent_id,
                    request.conversation_id,
                    str(request.invocation_id),
                    request.mode.value,
                    request.status.value,
                    request.revision,
                    request_json,
                    request.created_at.isoformat(),
                    now,
                ),
            )
        return request

    async def get_request(
        self,
        interaction_id: UUID,
    ) -> InteractionRequest | None:
        """Return the immutable request for one interaction."""
        await self.start()
        return await asyncio.to_thread(
            self._get_request_sync,
            interaction_id,
        )

    def _get_request_sync(
        self,
        interaction_id: UUID,
    ) -> InteractionRequest | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT request_json FROM runtime_interactions "
                "WHERE interaction_id = ?",
                (str(interaction_id),),
            ).fetchone()
        if row is None:
            return None
        return InteractionRequest.model_validate_json(row["request_json"])

    async def get_resolution(
        self,
        interaction_id: UUID,
    ) -> InteractionResolution | None:
        """Return the durable terminal receipt when available."""
        await self.start()
        return await asyncio.to_thread(
            self._get_resolution_sync,
            interaction_id,
        )

    def _get_resolution_sync(
        self,
        interaction_id: UUID,
    ) -> InteractionResolution | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT resolution_json FROM runtime_interactions "
                "WHERE interaction_id = ?",
                (str(interaction_id),),
            ).fetchone()
        if row is None:
            raise InteractionNotFoundError(str(interaction_id))
        if row["resolution_json"] is None:
            return None
        return InteractionResolution.model_validate_json(
            row["resolution_json"],
        )

    async def list_ready_continuations(
        self,
        *,
        agent_id: str,
    ) -> Sequence[ConversationContinuation]:
        """List content-free continuation outbox entries in order."""
        if not agent_id.strip():
            raise ValueError("continuation owner cannot be empty")
        await self.start()
        return await asyncio.to_thread(
            self._list_ready_continuations_sync,
            agent_id,
        )

    def _list_ready_continuations_sync(
        self,
        agent_id: str,
    ) -> tuple[ConversationContinuation, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM interaction_continuations "
                "WHERE agent_id = ? AND status = ? "
                "ORDER BY created_at, interaction_id",
                (agent_id, ContinuationDispatchStatus.READY.value),
            ).fetchall()
        return tuple(
            ConversationContinuation.model_validate(dict(row)) for row in rows
        )

    async def mark_continuation_dispatched(
        self,
        interaction_id: UUID,
        submission_id: UUID,
    ) -> ConversationContinuation:
        """Bind one continuation to its durable Submission exactly once."""
        await self.start()
        async with self._write_lock:
            return await asyncio.to_thread(
                self._mark_continuation_dispatched_sync,
                interaction_id,
                submission_id,
            )

    def _mark_continuation_dispatched_sync(
        self,
        interaction_id: UUID,
        submission_id: UUID,
    ) -> ConversationContinuation:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM interaction_continuations "
                "WHERE interaction_id = ?",
                (str(interaction_id),),
            ).fetchone()
            if row is None:
                raise InteractionNotFoundError(str(interaction_id))
            current = ConversationContinuation.model_validate(dict(row))
            if current.status is ContinuationDispatchStatus.DISPATCHED:
                if current.submission_id != submission_id:
                    raise InteractionConflictError(
                        "continuation is bound to another submission",
                    )
                connection.rollback()
                return current
            updated_at = utc_now()
            connection.execute(
                "UPDATE interaction_continuations SET status = ?, "
                "submission_id = ?, updated_at = ? "
                "WHERE interaction_id = ?",
                (
                    ContinuationDispatchStatus.DISPATCHED.value,
                    str(submission_id),
                    updated_at.isoformat(),
                    str(interaction_id),
                ),
            )
            connection.commit()
            return current.model_copy(
                update={
                    "status": ContinuationDispatchStatus.DISPATCHED,
                    "submission_id": submission_id,
                    "updated_at": updated_at,
                },
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def list_open(
        self,
        *,
        agent_id: str,
        conversation_id: str,
    ) -> Sequence[InteractionRequest]:
        """List ChatSpec-owned open interactions in creation order."""
        await self.start()
        return await asyncio.to_thread(
            self._list_open_sync,
            agent_id,
            conversation_id,
        )

    def _list_open_sync(
        self,
        agent_id: str,
        conversation_id: str,
    ) -> tuple[InteractionRequest, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT request_json FROM runtime_interactions "
                "WHERE agent_id = ? AND conversation_id = ? "
                "AND status = ? ORDER BY created_at, interaction_id",
                (
                    agent_id,
                    conversation_id,
                    InteractionStatus.OPEN.value,
                ),
            ).fetchall()
        return tuple(
            InteractionRequest.model_validate_json(row["request_json"])
            for row in rows
        )

    async def list_for_conversation(
        self,
        *,
        agent_id: str,
        conversation_id: str,
        limit: int = 100,
    ) -> Sequence[InteractionRecord]:
        """List newest requests with their optional terminal resolution."""
        if not agent_id.strip() or not conversation_id.strip():
            raise ValueError("interaction owner cannot be empty")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        await self.start()
        return await asyncio.to_thread(
            self._list_for_conversation_sync,
            agent_id,
            conversation_id,
            limit,
        )

    def _list_for_conversation_sync(
        self,
        agent_id: str,
        conversation_id: str,
        limit: int | None,
    ) -> tuple[InteractionRecord, ...]:
        with self._connect() as connection:
            suffix = " LIMIT ?" if limit is not None else ""
            arguments: tuple[object, ...] = (
                (agent_id, conversation_id, limit)
                if limit is not None
                else (agent_id, conversation_id)
            )
            rows = connection.execute(
                "SELECT request_json, resolution_json "
                "FROM runtime_interactions WHERE agent_id = ? "
                "AND conversation_id = ? "
                "ORDER BY updated_at DESC, interaction_id DESC" + suffix,
                arguments,
            ).fetchall()
        return tuple(
            InteractionRecord(
                request=InteractionRequest.model_validate_json(
                    row["request_json"],
                ),
                resolution=(
                    InteractionResolution.model_validate_json(
                        row["resolution_json"],
                    )
                    if row["resolution_json"] is not None
                    else None
                ),
            )
            for row in rows
        )

    async def scan_for_conversation(
        self,
        *,
        agent_id: str,
        conversation_id: str,
    ) -> Sequence[InteractionRecord]:
        """Scan all Lite records for a derived-index rebuild."""
        if not agent_id.strip() or not conversation_id.strip():
            raise ValueError("interaction owner cannot be empty")
        await self.start()
        return await asyncio.to_thread(
            self._list_for_conversation_sync,
            agent_id,
            conversation_id,
            None,
        )

    async def list_wait_conditions(
        self,
        *,
        agent_id: str,
        conversation_id: str,
        include_terminal: bool = False,
        limit: int = 100,
    ) -> Sequence[WaitCondition]:
        """Project blocking interactions as content-free wait conditions."""
        if not agent_id.strip() or not conversation_id.strip():
            raise ValueError("wait-condition owner cannot be empty")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        await self.start()
        records = await asyncio.to_thread(
            self._list_wait_records_sync,
            agent_id,
            conversation_id,
            include_terminal,
            limit,
        )
        attached = set(self._waiters) | set(self._terminal_hooks)
        return tuple(
            self._project_wait_condition(
                item,
                attached=item.request.interaction_id in attached,
            )
            for item in records
        )

    def _list_wait_records_sync(
        self,
        agent_id: str,
        conversation_id: str,
        include_terminal: bool,
        limit: int,
    ) -> tuple[InteractionRecord, ...]:
        query = (
            "SELECT request_json, resolution_json "
            "FROM runtime_interactions WHERE agent_id = ? "
            "AND conversation_id = ? AND mode = ? "
        )
        arguments: tuple[object, ...] = (
            agent_id,
            conversation_id,
            InteractionMode.BLOCKING.value,
        )
        if not include_terminal:
            query += "AND status = ? "
            arguments += (InteractionStatus.OPEN.value,)
        query += "ORDER BY updated_at DESC, interaction_id DESC LIMIT ?"
        arguments += (limit,)
        with self._connect() as connection:
            rows = connection.execute(query, arguments).fetchall()
        return tuple(
            InteractionRecord(
                request=InteractionRequest.model_validate_json(
                    row["request_json"],
                ),
                resolution=(
                    InteractionResolution.model_validate_json(
                        row["resolution_json"],
                    )
                    if row["resolution_json"] is not None
                    else None
                ),
            )
            for row in rows
        )

    @staticmethod
    def _project_wait_condition(
        record: InteractionRecord,
        *,
        attached: bool,
    ) -> WaitCondition:
        request = record.request
        resolution = record.resolution
        kind = {
            InteractionKind.APPROVAL: WaitConditionKind.APPROVAL,
            InteractionKind.USER_INPUT: WaitConditionKind.USER_INPUT,
        }[request.kind]
        status = WaitConditionStatus.WAITING
        if resolution is not None:
            status = {
                InteractionStatus.RESOLVED: WaitConditionStatus.SATISFIED,
                InteractionStatus.EXPIRED: WaitConditionStatus.EXPIRED,
                InteractionStatus.CANCELLED: WaitConditionStatus.CANCELLED,
            }[resolution.status]
        return WaitCondition(
            condition_id=request.interaction_id,
            kind=kind,
            status=status,
            agent_id=request.agent_id,
            conversation_id=request.conversation_id,
            source_type="qwenpaw.interaction",
            source_id=request.interaction_id,
            policy_source_id=request.source_id,
            continuation=ContinuationRef(
                mode=request.continuation_mode,
                availability=(
                    ContinuationAvailability.ATTACHED
                    if attached
                    else ContinuationAvailability.DETACHED
                ),
                invocation_id=request.invocation_id,
                checkpoint_id=request.continuation_checkpoint_id,
            ),
            revision=(
                resolution.revision
                if resolution is not None
                else request.revision
            ),
            created_at=request.created_at,
            resolved_at=(
                resolution.resolved_at if resolution is not None else None
            ),
        )

    @staticmethod
    def _validate_response(
        request: InteractionRequest,
        response: InteractionResponse,
    ) -> None:
        option_ids = {option.option_id for option in request.options}
        unknown = set(response.selected_option_ids) - option_ids
        if unknown:
            raise InteractionConflictError(
                f"unknown interaction option(s): {sorted(unknown)}",
            )
        if request.kind is InteractionKind.APPROVAL and (
            len(response.selected_option_ids) != 1
            or bool(response.text)
            or bool(response.values)
        ):
            raise InteractionConflictError(
                "approval interaction requires exactly one option",
            )

    async def resolve(
        self,
        response: InteractionResponse,
    ) -> InteractionResolution:
        """Resolve once, replaying an identical idempotent response."""
        await self.start()
        async with self._write_lock:
            resolution = await asyncio.to_thread(
                self._resolve_sync,
                response,
            )
        await self._publish_resolution(resolution)
        return resolution

    def _resolve_sync(
        self,
        response: InteractionResponse,
    ) -> InteractionResolution:
        response_json = _canonical_json(response)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM runtime_interactions "
                "WHERE interaction_id = ?",
                (str(response.interaction_id),),
            ).fetchone()
            if row is None:
                raise InteractionNotFoundError(str(response.interaction_id))
            if row["status"] != InteractionStatus.OPEN.value:
                if row["response_idempotency_key"] != response.idempotency_key:
                    raise InteractionConflictError(
                        "interaction is already terminal",
                    )
                if row["response_json"] != response_json:
                    raise InteractionIdempotencyConflictError(
                        "response idempotency key has conflicting content",
                    )
                connection.rollback()
                return InteractionResolution.model_validate_json(
                    row["resolution_json"],
                )
            actual_revision = int(row["revision"])
            if response.expected_revision != actual_revision:
                raise InteractionRevisionConflictError(
                    response.expected_revision,
                    actual_revision,
                )
            request = InteractionRequest.model_validate_json(
                row["request_json"],
            )
            self._validate_response(request, response)
            resolution = InteractionResolution(
                interaction_id=response.interaction_id,
                status=InteractionStatus.RESOLVED,
                revision=actual_revision + 1,
                response=response,
            )
            self._persist_resolution(connection, resolution, response_json)
            if request.continuation_mode is ContinuationMode.CONVERSATION_TURN:
                self._insert_conversation_continuation(
                    connection,
                    request,
                    resolution,
                )
            connection.commit()
            return resolution
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _persist_resolution(
        connection: sqlite3.Connection,
        resolution: InteractionResolution,
        response_json: str | None,
    ) -> None:
        response_key = (
            resolution.response.idempotency_key
            if resolution.response is not None
            else None
        )
        connection.execute(
            "UPDATE runtime_interactions SET status = ?, revision = ?, "
            "response_idempotency_key = ?, response_json = ?, "
            "resolution_json = ?, updated_at = ? WHERE interaction_id = ?",
            (
                resolution.status.value,
                resolution.revision,
                response_key,
                response_json,
                _canonical_json(resolution),
                resolution.resolved_at.isoformat(),
                str(resolution.interaction_id),
            ),
        )

    @staticmethod
    def _insert_conversation_continuation(
        connection: sqlite3.Connection,
        request: InteractionRequest,
        resolution: InteractionResolution,
    ) -> None:
        timestamp = resolution.resolved_at.isoformat()
        connection.execute(
            "INSERT INTO interaction_continuations "
            "(interaction_id, agent_id, conversation_id, "
            "response_revision, status, submission_id, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(request.interaction_id),
                request.agent_id,
                request.conversation_id,
                resolution.revision,
                ContinuationDispatchStatus.READY.value,
                None,
                timestamp,
                timestamp,
            ),
        )

    async def cancel_invocation(
        self,
        invocation_id: UUID,
        *,
        detail: str,
        include_non_blocking: bool = True,
        preserve_conversation_continuations: bool = False,
    ) -> Sequence[InteractionResolution]:
        """Cancel invocation interactions in one durable transaction."""
        await self.start()
        async with self._write_lock:
            resolutions = await asyncio.to_thread(
                self._cancel_invocation_sync,
                invocation_id,
                detail,
                include_non_blocking,
                preserve_conversation_continuations,
            )
        for resolution in resolutions:
            await self._publish_resolution(resolution)
        return resolutions

    async def expire(
        self,
        interaction_id: UUID,
        *,
        detail: str,
    ) -> InteractionResolution:
        """Expire one open interaction, preserving a concurrent terminal."""
        await self.start()
        async with self._write_lock:
            resolution = await asyncio.to_thread(
                self._expire_sync,
                interaction_id,
                detail,
            )
        await self._publish_resolution(resolution)
        return resolution

    def _expire_sync(
        self,
        interaction_id: UUID,
        detail: str,
    ) -> InteractionResolution:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, revision, resolution_json "
                "FROM runtime_interactions WHERE interaction_id = ?",
                (str(interaction_id),),
            ).fetchone()
            if row is None:
                raise InteractionNotFoundError(str(interaction_id))
            if row["status"] != InteractionStatus.OPEN.value:
                connection.rollback()
                return InteractionResolution.model_validate_json(
                    row["resolution_json"],
                )
            resolution = InteractionResolution(
                interaction_id=interaction_id,
                status=InteractionStatus.EXPIRED,
                revision=int(row["revision"]) + 1,
                detail=detail,
            )
            self._persist_resolution(connection, resolution, None)
            connection.commit()
            return resolution
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _cancel_invocation_sync(
        self,
        invocation_id: UUID,
        detail: str,
        include_non_blocking: bool,
        preserve_conversation_continuations: bool,
    ) -> tuple[InteractionResolution, ...]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            query = (
                "SELECT interaction_id, revision, request_json "
                "FROM runtime_interactions "
                "WHERE invocation_id = ? AND status = ? "
            )
            parameters: tuple[object, ...] = (
                str(invocation_id),
                InteractionStatus.OPEN.value,
            )
            if not include_non_blocking:
                query += "AND mode = ? "
                parameters += (InteractionMode.BLOCKING.value,)
            rows = connection.execute(
                query + "ORDER BY created_at, interaction_id",
                parameters,
            ).fetchall()
            resolutions = tuple(
                InteractionResolution(
                    interaction_id=UUID(row["interaction_id"]),
                    status=InteractionStatus.CANCELLED,
                    revision=int(row["revision"]) + 1,
                    detail=detail,
                )
                for row in rows
                if not (
                    preserve_conversation_continuations
                    and InteractionRequest.model_validate_json(
                        row["request_json"],
                    ).continuation_mode
                    is ContinuationMode.CONVERSATION_TURN
                )
            )
            for resolution in resolutions:
                self._persist_resolution(connection, resolution, None)
            connection.commit()
            return resolutions
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def wait(
        self,
        interaction_id: UUID,
        *,
        timeout_seconds: float | None = None,
    ) -> InteractionResolution:
        """Wait for one blocking interaction without owning its lifecycle."""
        request = await self.get_request(interaction_id)
        if request is None:
            raise InteractionNotFoundError(str(interaction_id))
        if request.mode is not InteractionMode.BLOCKING:
            raise InteractionConflictError(
                "non-blocking interaction cannot be awaited",
            )
        existing = await self.get_resolution(interaction_id)
        if existing is not None:
            return existing
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[InteractionResolution] = loop.create_future()
        self._waiters.setdefault(interaction_id, set()).add(waiter)
        try:
            existing = await self.get_resolution(interaction_id)
            if existing is not None:
                return existing
            pending = asyncio.shield(waiter)
            if timeout_seconds is None:
                return await pending
            try:
                return await asyncio.wait_for(pending, timeout_seconds)
            except asyncio.TimeoutError:
                return await self.expire(
                    interaction_id,
                    detail="interaction response timed out",
                )
        finally:
            group = self._waiters.get(interaction_id)
            if group is not None:
                group.discard(waiter)
                if not group:
                    self._waiters.pop(interaction_id, None)

    async def add_terminal_hook(
        self,
        interaction_id: UUID,
        hook: InteractionTerminalHook,
    ) -> None:
        """Invoke one live compatibility hook after durable termination."""
        existing = await self.get_resolution(interaction_id)
        if existing is not None:
            await hook(existing)
            return
        self._terminal_hooks.setdefault(interaction_id, []).append(hook)
        existing = await self.get_resolution(interaction_id)
        if existing is None:
            return
        hooks = self._terminal_hooks.get(interaction_id)
        if hooks is not None:
            if hook in hooks:
                hooks.remove(hook)
            if not hooks:
                self._terminal_hooks.pop(interaction_id, None)
        await hook(existing)

    async def _publish_resolution(
        self,
        resolution: InteractionResolution,
    ) -> None:
        """Wake waiters and drain live terminal adapters after commit."""
        for waiter in self._waiters.pop(
            resolution.interaction_id,
            set(),
        ):
            if not waiter.done():
                waiter.set_result(resolution)
        hooks = self._terminal_hooks.pop(
            resolution.interaction_id,
            [],
        )
        for hook in hooks:
            try:
                await hook(resolution)
            except Exception:  # pylint: disable=broad-except
                logger.warning(
                    "Interaction terminal hook failed: interaction_id=%s",
                    str(resolution.interaction_id)[:8],
                    exc_info=True,
                )
