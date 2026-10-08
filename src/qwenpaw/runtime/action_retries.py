# -*- coding: utf-8 -*-
"""Private input and content-safe outbox for durable Action retries."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid5

from ..kernel import (
    ActionStore,
    ActionRequest,
    ActionResult,
    ActionRetryContinuation,
    ActionRetryContinuationStatus,
    ActionRetryDecision,
    ActionRetryDisposition,
    ActionRetryInputCheckpoint,
    ActionRetryInputStore,
)
from ..kernel.models import JsonObject
from ..kernel.models import utc_now
from ..utils.io_utils import (
    get_path_lock,
    read_json_async,
    run_sync_io,
    write_json_atomic_async,
)

ACTION_RETRY_INPUT_SCHEMA = "qwenpaw.action-retry-input.v1"
ACTION_RETRY_CONTINUATION_SCHEMA = (
    "qwenpaw.action-retry-continuation.v1"
)
ActionRetryDispatcher = Callable[
    [ActionRetryContinuation],
    Awaitable[UUID | None],
]


class ActionRetryInputConflictError(RuntimeError):
    """Raised when private retry input conflicts with durable identity."""


class ActionRetryContinuationConflictError(RuntimeError):
    """Raised when retry outbox state conflicts with immutable evidence."""


class FilesystemActionRetryInputStore:
    """Owner-only raw input store referenced by public Action evidence."""

    def __init__(self, workspace_dir: Path) -> None:
        self._root = (
            Path(workspace_dir)
            / ".qwenpaw"
            / "lite"
            / "action-retry-inputs"
        )

    def _path(self, checkpoint_id: UUID) -> Path:
        return self._root / f"{checkpoint_id}.json"

    @staticmethod
    def _parse(
        payload: object,
    ) -> tuple[ActionRetryInputCheckpoint, JsonObject]:
        if not isinstance(payload, dict):
            raise ActionRetryInputConflictError(
                "Action retry input payload must be an object",
            )
        if payload.get("schema") != ACTION_RETRY_INPUT_SCHEMA:
            raise ActionRetryInputConflictError(
                "unsupported Action retry input schema",
            )
        arguments = payload.get("arguments")
        if not isinstance(arguments, dict):
            raise ActionRetryInputConflictError(
                "Action retry input arguments must be an object",
            )
        checkpoint = ActionRetryInputCheckpoint.model_validate(
            payload.get("checkpoint"),
        )
        return checkpoint, arguments

    async def save(
        self,
        request: ActionRequest,
        decision: ActionRetryDecision,
    ) -> ActionRetryInputCheckpoint:
        """Save raw input before the public retry decision is committed."""
        if (
            decision.disposition
            is not ActionRetryDisposition.RETRY_FROM_NEW_ACTION
            or decision.next_attempt is None
        ):
            raise ValueError("retry input requires an admitted next attempt")
        if decision.max_attempts != request.retry_policy.max_attempts:
            raise ValueError("retry input policy does not match Action")
        if request.agent_id is None:
            raise ValueError("retry input requires an Action agent identity")
        checkpoint = ActionRetryInputCheckpoint(
            checkpoint_id=uuid5(
                request.action_id,
                f"retry-input:{decision.next_attempt}",
            ),
            action_id=request.action_id,
            retry_root_action_id=(
                request.retry_root_action_id or request.action_id
            ),
            invocation_id=request.invocation_id,
            agent_id=request.agent_id,
            conversation_id=request.conversation_id,
            correlation_id=request.correlation_id,
            registry_generation=request.registry_generation,
            capability_id=request.capability_id,
            kind=request.kind,
            action_name=request.action_name,
            arguments_hash=request.arguments_hash,
            next_attempt=decision.next_attempt,
            created_at=request.requested_at,
        )
        path = self._path(checkpoint.checkpoint_id)
        async with get_path_lock(path):
            try:
                existing_payload = await read_json_async(path)
            except FileNotFoundError:
                existing_payload = None
            if existing_payload is not None:
                existing, arguments = self._parse(existing_payload)
                if existing != checkpoint or arguments != request.arguments:
                    raise ActionRetryInputConflictError(
                        "Action retry input already conflicts",
                    )
                return existing
            await write_json_atomic_async(
                path,
                {
                    "schema": ACTION_RETRY_INPUT_SCHEMA,
                    "checkpoint": checkpoint.model_dump(mode="json"),
                    "arguments": request.arguments,
                },
                sort_keys=True,
            )
        return checkpoint

    async def load(
        self,
        checkpoint_id: UUID,
    ) -> tuple[ActionRetryInputCheckpoint, JsonObject]:
        """Load exact private input without exposing it through projections."""
        try:
            payload = await read_json_async(self._path(checkpoint_id))
        except FileNotFoundError as exc:
            raise ActionRetryInputConflictError(
                "Action retry input is unavailable",
            ) from exc
        checkpoint, arguments = self._parse(payload)
        if checkpoint.checkpoint_id != checkpoint_id:
            raise ActionRetryInputConflictError(
                "Action retry input filename identity mismatch",
            )
        return checkpoint, arguments

    async def list_checkpoints(
        self,
        *,
        agent_id: str,
    ) -> tuple[ActionRetryInputCheckpoint, ...]:
        """List private retry references for startup outbox repair."""
        paths = await run_sync_io(
            lambda: tuple(sorted(self._root.glob("*.json"))),
        )
        checkpoints = []
        for path in paths:
            checkpoint, _ = self._parse(await read_json_async(path))
            if checkpoint.agent_id == agent_id:
                checkpoints.append(checkpoint)
        checkpoints.sort(
            key=lambda item: (item.created_at, item.checkpoint_id),
        )
        return tuple(checkpoints)


class FilesystemActionRetryContinuationStore:
    """Content-safe durable outbox for Host-admitted Action retries."""

    def __init__(
        self,
        workspace_dir: Path,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._root = (
            Path(workspace_dir)
            / ".qwenpaw"
            / "lite"
            / "action-retry-continuations"
        )
        self._clock = clock

    def _path(self, continuation_id: UUID) -> Path:
        return self._root / f"{continuation_id}.json"

    @staticmethod
    def _parse(payload: object) -> ActionRetryContinuation:
        if not isinstance(payload, dict):
            raise ActionRetryContinuationConflictError(
                "Action retry continuation must be an object",
            )
        if payload.get("schema") != ACTION_RETRY_CONTINUATION_SCHEMA:
            raise ActionRetryContinuationConflictError(
                "unsupported Action retry continuation schema",
            )
        return ActionRetryContinuation.model_validate(
            payload.get("continuation"),
        )

    async def _write(
        self,
        continuation: ActionRetryContinuation,
    ) -> None:
        await write_json_atomic_async(
            self._path(continuation.continuation_id),
            {
                "schema": ACTION_RETRY_CONTINUATION_SCHEMA,
                "continuation": continuation.model_dump(mode="json"),
            },
            sort_keys=True,
        )

    @staticmethod
    def _validate_binding(
        checkpoint: ActionRetryInputCheckpoint,
        result: ActionResult,
    ) -> ActionRetryDecision:
        decision = result.retry_decision
        if (
            result.action_id != checkpoint.action_id
            or result.invocation_id != checkpoint.invocation_id
            or result.conversation_id != checkpoint.conversation_id
        ):
            raise ActionRetryContinuationConflictError(
                "Action retry result identity does not match checkpoint",
            )
        if (
            not result.retryable
            or decision is None
            or decision.disposition
            is not ActionRetryDisposition.RETRY_FROM_NEW_ACTION
            or decision.input_checkpoint_id != checkpoint.checkpoint_id
            or decision.next_attempt != checkpoint.next_attempt
        ):
            raise ActionRetryContinuationConflictError(
                "Action retry result does not admit this checkpoint",
            )
        return decision

    @staticmethod
    def _validate_request_binding(
        checkpoint: ActionRetryInputCheckpoint,
        request: ActionRequest,
    ) -> None:
        expected_root = request.retry_root_action_id or request.action_id
        checkpoint_identity = (
            checkpoint.retry_root_action_id,
            checkpoint.agent_id,
            checkpoint.correlation_id,
            checkpoint.registry_generation,
            checkpoint.capability_id,
            checkpoint.kind,
            checkpoint.action_name,
            checkpoint.arguments_hash,
        )
        request_identity = (
            expected_root,
            request.agent_id,
            request.correlation_id,
            request.registry_generation,
            request.capability_id,
            request.kind,
            request.action_name,
            request.arguments_hash,
        )
        if checkpoint_identity != request_identity:
            raise ActionRetryContinuationConflictError(
                "Action retry checkpoint does not match its request",
            )

    async def defer(
        self,
        checkpoint: ActionRetryInputCheckpoint,
        result: ActionResult,
    ) -> ActionRetryContinuation:
        """Publish one idempotent entry after ActionResult persistence."""
        decision = self._validate_binding(checkpoint, result)
        retry_delay = decision.retry_after_seconds or 0
        ready_at = result.completed_at + timedelta(seconds=retry_delay)
        continuation = ActionRetryContinuation(
            continuation_id=uuid5(
                checkpoint.checkpoint_id,
                "action-retry-continuation",
            ),
            checkpoint=checkpoint,
            source_observation_digest=result.observation_digest,
            ready_at=ready_at,
            status=(
                ActionRetryContinuationStatus.READY
                if retry_delay == 0
                else ActionRetryContinuationStatus.WAITING_DELAY
            ),
            created_at=result.completed_at,
            updated_at=result.completed_at,
        )
        path = self._path(continuation.continuation_id)
        async with get_path_lock(path):
            try:
                payload = await read_json_async(path)
            except FileNotFoundError:
                payload = None
            if payload is not None:
                existing = self._parse(payload)
                if (
                    existing.checkpoint != checkpoint
                    or existing.source_observation_digest
                    != result.observation_digest
                    or existing.ready_at != ready_at
                ):
                    raise ActionRetryContinuationConflictError(
                        "Action retry continuation already conflicts",
                    )
                return existing
            await self._write(continuation)
        return continuation

    async def get(
        self,
        continuation_id: UUID,
    ) -> ActionRetryContinuation | None:
        """Read one retry continuation, returning None when absent."""
        try:
            payload = await read_json_async(self._path(continuation_id))
        except FileNotFoundError:
            return None
        return self._parse(payload)

    async def _promote_if_ready(
        self,
        continuation_id: UUID,
        now: datetime,
    ) -> ActionRetryContinuation:
        path = self._path(continuation_id)
        async with get_path_lock(path):
            current = await self.get(continuation_id)
            if current is None:
                raise ActionRetryContinuationConflictError(
                    "Action retry continuation is unavailable",
                )
            if (
                current.status
                is not ActionRetryContinuationStatus.WAITING_DELAY
                or current.ready_at > now
            ):
                return current
            updated = current.model_copy(
                update={
                    "status": ActionRetryContinuationStatus.READY,
                    "revision": current.revision + 1,
                    "updated_at": now,
                },
            )
            await self._write(updated)
        return updated

    async def list_pending(
        self,
        *,
        agent_id: str,
        now: datetime | None = None,
    ) -> tuple[ActionRetryContinuation, ...]:
        """List owned waiting work and promote every matured delay."""
        effective_now = now or self._clock()
        paths = await run_sync_io(
            lambda: tuple(sorted(self._root.glob("*.json"))),
        )
        pending = []
        for path in paths:
            continuation = self._parse(await read_json_async(path))
            if continuation.checkpoint.agent_id != agent_id:
                continue
            continuation = await self._promote_if_ready(
                continuation.continuation_id,
                effective_now,
            )
            if continuation.status in {
                ActionRetryContinuationStatus.WAITING_DELAY,
                ActionRetryContinuationStatus.READY,
            }:
                pending.append(continuation)
        pending.sort(
            key=lambda item: (item.ready_at, item.continuation_id),
        )
        return tuple(pending)

    async def cancel(
        self,
        continuation_id: UUID,
    ) -> ActionRetryContinuation:
        """Cancel undispatched retry work without touching its evidence."""
        path = self._path(continuation_id)
        async with get_path_lock(path):
            current = await self.get(continuation_id)
            if current is None:
                raise ActionRetryContinuationConflictError(
                    "Action retry continuation is unavailable",
                )
            if current.status in {
                ActionRetryContinuationStatus.CANCELLED,
                ActionRetryContinuationStatus.DISPATCHED,
            }:
                return current
            updated = current.model_copy(
                update={
                    "status": ActionRetryContinuationStatus.CANCELLED,
                    "revision": current.revision + 1,
                    "updated_at": self._clock(),
                },
            )
            await self._write(updated)
        return updated

    async def dispatch(
        self,
        continuation_id: UUID,
        dispatcher: ActionRetryDispatcher,
    ) -> ActionRetryContinuation:
        """Idempotently bind one ready entry to durable dispatch work."""
        path = self._path(continuation_id)
        async with get_path_lock(path):
            current = await self.get(continuation_id)
            if current is None:
                raise ActionRetryContinuationConflictError(
                    "Action retry continuation is unavailable",
                )
            if current.status is not ActionRetryContinuationStatus.READY:
                return current
            dispatch_id = await dispatcher(current)
            updated = current.model_copy(
                update={
                    "status": (
                        ActionRetryContinuationStatus.DISPATCHED
                        if dispatch_id is not None
                        else ActionRetryContinuationStatus.CANCELLED
                    ),
                    "dispatch_id": dispatch_id,
                    "revision": current.revision + 1,
                    "updated_at": self._clock(),
                },
            )
            await self._write(updated)
        return updated

    async def repair(
        self,
        *,
        agent_id: str,
        input_store: ActionRetryInputStore,
        action_store: ActionStore,
    ) -> tuple[ActionRetryContinuation, ...]:
        """Rebuild outbox entries missing after a post-result crash."""
        repaired = []
        checkpoints = await input_store.list_checkpoints(
            agent_id=agent_id,
        )
        for checkpoint in checkpoints:
            record = await action_store.get(
                checkpoint.action_id,
                invocation_id=checkpoint.invocation_id,
                conversation_id=checkpoint.conversation_id,
            )
            if record is None or record.result is None:
                continue
            self._validate_request_binding(checkpoint, record.request)
            decision = record.result.retry_decision
            if (
                not record.result.retryable
                or decision is None
                or decision.input_checkpoint_id
                != checkpoint.checkpoint_id
            ):
                continue
            repaired.append(
                await self.defer(checkpoint, record.result),
            )
        return tuple(repaired)


def lite_action_retry_input_store(
    workspace_dir: Path,
) -> FilesystemActionRetryInputStore:
    """Return the Lite private Action retry input store."""
    return FilesystemActionRetryInputStore(workspace_dir)


def lite_action_retry_continuation_store(
    workspace_dir: Path,
    *,
    clock: Callable[[], datetime] = utc_now,
) -> FilesystemActionRetryContinuationStore:
    """Return the Lite Action retry continuation outbox."""
    return FilesystemActionRetryContinuationStore(
        workspace_dir,
        clock=clock,
    )


__all__ = [
    "ACTION_RETRY_INPUT_SCHEMA",
    "ACTION_RETRY_CONTINUATION_SCHEMA",
    "ActionRetryContinuationConflictError",
    "ActionRetryDispatcher",
    "ActionRetryInputConflictError",
    "FilesystemActionRetryContinuationStore",
    "FilesystemActionRetryInputStore",
    "lite_action_retry_continuation_store",
    "lite_action_retry_input_store",
]
