# -*- coding: utf-8 -*-
"""Durable private context and outbox for completed background Actions."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid5

from ..kernel import (
    COMMITTED_ACTION_ITEM_METADATA_KEY,
    BackgroundActionContextCheckpoint,
    BackgroundActionContinuation,
    BackgroundActionContinuationStatus,
    CommittedActionItem,
    TERMINAL_SUBMISSION_STATUSES,
)
from ..kernel.models import utc_now
from ..utils.io_utils import (
    get_path_lock,
    read_json_async,
    run_sync_io,
    write_json_atomic_async,
)
from .model_step_contexts import context_has_committed_action_items

BACKGROUND_ACTION_CONTEXT_SCHEMA = "qwenpaw.background-action-context.v1"
BACKGROUND_ACTION_CONTINUATION_SCHEMA = (
    "qwenpaw.background-action-continuation.v1"
)
BackgroundActionDispatcher = Callable[
    [BackgroundActionContinuation],
    Awaitable[UUID | None],
]


class BackgroundActionConflictError(RuntimeError):
    """Raised when one background Action identity has conflicting state."""


class BackgroundActionContextNotFoundError(RuntimeError):
    """Raised when a private background Action snapshot is unavailable."""


class BackgroundActionCompletionHandler:
    """Commit one offloaded result and wake durable continuation work."""

    def __init__(
        self,
        *,
        workspace: Any,
        source_submission_id: UUID,
        invocation_id: UUID,
        conversation_id: str,
        correlation_id: UUID,
        agent_id: str,
        recovery_cycle: int,
    ) -> None:
        self._workspace = workspace
        self._source_submission_id = source_submission_id
        self._invocation_id = invocation_id
        self._conversation_id = conversation_id
        self._correlation_id = correlation_id
        self._agent_id = agent_id
        self._recovery_cycle = recovery_cycle

    async def __call__(self, entry: Any, agent_state: dict[str, Any]) -> None:
        """Persist the final hint before the source Invocation disappears."""
        await self.commit_response(
            entry.final_response,
            entry.ctx,
            agent_state,
            end_state=entry.end_state,
        )
        dispatcher = self._workspace.submission_dispatcher
        control = self._workspace.invocation_control
        while True:
            source = await control.get_submission(
                self._source_submission_id,
            )
            if source is None or source.status in TERMINAL_SUBMISSION_STATUSES:
                if dispatcher is not None:
                    dispatcher.wake_resource_waits()
                return
            await asyncio.sleep(0.1)

    async def commit_response(
        self,
        response: Any,
        context: Any,
        agent_state: dict[str, Any],
        *,
        end_state: str = "completed",
    ) -> BackgroundActionContextCheckpoint | None:
        """Commit the private hint immediately after ActionResult storage."""
        committed_item = committed_item_from_response(response)
        if committed_item is None:
            return None
        stored = await self.prepare_response(
            response,
            context,
            agent_state,
            committed_item,
            end_state=end_state,
        )
        workspace_dir = Path(self._workspace.workspace_dir)
        await lite_background_action_continuation_store(workspace_dir).defer(
            stored,
        )
        dispatcher = self._workspace.submission_dispatcher
        if dispatcher is not None:
            dispatcher.wake_resource_waits()
        return stored

    async def prepare_response(
        self,
        response: Any,
        context: Any,
        agent_state: dict[str, Any],
        committed_item: CommittedActionItem,
        *,
        end_state: str = "completed",
    ) -> BackgroundActionContextCheckpoint:
        """Save private context before the public ActionResult commit."""
        if (
            committed_item.invocation_id != self._invocation_id
            or committed_item.conversation_id != self._conversation_id
            or committed_item.executor_item_id != context.tool_call_id
        ):
            raise BackgroundActionConflictError(
                "background Action binding does not match its Invocation",
            )
        from ..tool_calls._hint import make_offload_hint_msg

        snapshot = build_background_action_snapshot(
            agent_state,
            make_offload_hint_msg(
                SimpleNamespace(
                    final_response=response,
                    ctx=context,
                    end_state=end_state,
                ),
            ),
            committed_item,
        )
        checkpoint = build_background_action_checkpoint(
            committed_item=committed_item,
            source_submission_id=self._source_submission_id,
            correlation_id=self._correlation_id,
            agent_id=self._agent_id,
            recovery_cycle=self._recovery_cycle,
        )
        workspace_dir = Path(self._workspace.workspace_dir)
        stored = await lite_background_action_context_store(
            workspace_dir,
        ).save(
            checkpoint,
            snapshot,
        )
        return stored


def committed_item_from_response(response: Any) -> CommittedActionItem | None:
    """Read the exact Action binding computed for result persistence."""
    metadata = getattr(response, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    try:
        return CommittedActionItem.model_validate(
            metadata.get(COMMITTED_ACTION_ITEM_METADATA_KEY),
        )
    except (TypeError, ValueError):
        return None


def build_background_action_snapshot(
    agent_state: dict[str, Any],
    hint: Any,
    committed_item: CommittedActionItem,
) -> dict[str, Any]:
    """Append one verified result hint to an isolated Agent snapshot."""
    snapshot = copy.deepcopy(agent_state)
    state = snapshot.get("state")
    if not isinstance(state, dict):
        raise BackgroundActionConflictError(
            "background Action snapshot has no Agent state",
        )
    context = state.get("context")
    if not isinstance(context, list):
        raise BackgroundActionConflictError(
            "background Action snapshot has no Agent context",
        )
    if context_has_committed_action_items(snapshot, (committed_item,)):
        return snapshot
    model_dump = getattr(hint, "model_dump", None)
    if not callable(model_dump):
        raise BackgroundActionConflictError(
            "background Action hint is not serializable",
        )
    context.append(model_dump(mode="json"))
    if not context_has_committed_action_items(snapshot, (committed_item,)):
        raise BackgroundActionConflictError(
            "background Action hint has no committed binding",
        )
    return snapshot


class FilesystemBackgroundActionContextStore:
    """Owner-only immutable Agent snapshots for background completion."""

    def __init__(self, workspace_dir: Path) -> None:
        self._root = (
            Path(workspace_dir)
            / ".qwenpaw"
            / "lite"
            / "background-action-contexts"
        )

    def _path(self, checkpoint_id: UUID) -> Path:
        return self._root / f"{checkpoint_id}.json"

    @staticmethod
    def _parse(
        payload: object,
    ) -> tuple[BackgroundActionContextCheckpoint, dict[str, Any]]:
        if not isinstance(payload, dict):
            raise BackgroundActionConflictError(
                "background Action context payload must be an object",
            )
        if payload.get("schema") != BACKGROUND_ACTION_CONTEXT_SCHEMA:
            raise BackgroundActionConflictError(
                "unsupported background Action context schema",
            )
        state = payload.get("agent_state")
        if not isinstance(state, dict):
            raise BackgroundActionConflictError(
                "background Action context has no Agent state",
            )
        checkpoint = BackgroundActionContextCheckpoint.model_validate(
            payload.get("checkpoint"),
        )
        return checkpoint, state

    async def save(
        self,
        checkpoint: BackgroundActionContextCheckpoint,
        agent_state: dict[str, Any],
    ) -> BackgroundActionContextCheckpoint:
        """Save the private snapshot before publishing its outbox entry."""
        if not context_has_committed_action_items(
            agent_state,
            (checkpoint.committed_item,),
        ):
            raise BackgroundActionConflictError(
                "background Action snapshot does not prove its binding",
            )
        path = self._path(checkpoint.checkpoint_id)
        async with get_path_lock(path):
            try:
                payload = await read_json_async(path)
            except FileNotFoundError:
                payload = None
            if payload is not None:
                existing, _existing_state = self._parse(payload)
                if (
                    existing.model_dump(exclude={"created_at"})
                    != checkpoint.model_dump(exclude={"created_at"})
                ):
                    raise BackgroundActionConflictError(
                        "background Action context already conflicts",
                    )
                return existing
            await write_json_atomic_async(
                path,
                {
                    "schema": BACKGROUND_ACTION_CONTEXT_SCHEMA,
                    "checkpoint": checkpoint.model_dump(mode="json"),
                    "agent_state": agent_state,
                },
                sort_keys=True,
            )
        return checkpoint

    async def load(
        self,
        checkpoint_id: UUID,
    ) -> tuple[BackgroundActionContextCheckpoint, dict[str, Any]]:
        """Load one exact private snapshot."""
        try:
            payload = await read_json_async(self._path(checkpoint_id))
        except FileNotFoundError as exc:
            raise BackgroundActionContextNotFoundError(
                str(checkpoint_id),
            ) from exc
        checkpoint, state = self._parse(payload)
        if checkpoint.checkpoint_id != checkpoint_id:
            raise BackgroundActionConflictError(
                "background Action context filename identity mismatch",
            )
        return checkpoint, state

    async def list_checkpoints(
        self,
        *,
        agent_id: str,
    ) -> tuple[BackgroundActionContextCheckpoint, ...]:
        """List content-safe references for snapshot-first repair."""
        paths = await run_sync_io(
            lambda: tuple(sorted(self._root.glob("*.json"))),
        )
        checkpoints = []
        for path in paths:
            payload = await read_json_async(path)
            checkpoint, _ = self._parse(payload)
            if checkpoint.agent_id == agent_id:
                checkpoints.append(checkpoint)
        checkpoints.sort(
            key=lambda item: (item.created_at, item.checkpoint_id),
        )
        return tuple(checkpoints)


class FilesystemBackgroundActionContinuationStore:
    """Content-safe outbox for completed background Actions."""

    def __init__(self, workspace_dir: Path) -> None:
        self._root = (
            Path(workspace_dir)
            / ".qwenpaw"
            / "lite"
            / "background-action-continuations"
        )

    def _path(self, continuation_id: UUID) -> Path:
        return self._root / f"{continuation_id}.json"

    @staticmethod
    def _parse(payload: object) -> BackgroundActionContinuation:
        if not isinstance(payload, dict):
            raise BackgroundActionConflictError(
                "background Action continuation must be an object",
            )
        if payload.get("schema") != BACKGROUND_ACTION_CONTINUATION_SCHEMA:
            raise BackgroundActionConflictError(
                "unsupported background Action continuation schema",
            )
        return BackgroundActionContinuation.model_validate(
            payload.get("continuation"),
        )

    async def _write(
        self,
        continuation: BackgroundActionContinuation,
    ) -> None:
        await write_json_atomic_async(
            self._path(continuation.continuation_id),
            {
                "schema": BACKGROUND_ACTION_CONTINUATION_SCHEMA,
                "continuation": continuation.model_dump(mode="json"),
            },
            sort_keys=True,
        )

    async def defer(
        self,
        checkpoint: BackgroundActionContextCheckpoint,
        *,
        max_recovery_cycles: int = 2,
    ) -> BackgroundActionContinuation:
        """Publish one idempotent outbox entry after snapshot commit."""
        continuation = BackgroundActionContinuation(
            continuation_id=checkpoint.continuation_id,
            checkpoint=checkpoint,
            status=(
                BackgroundActionContinuationStatus.WAITING_SOURCE
                if checkpoint.recovery_cycle <= max_recovery_cycles
                else (
                    BackgroundActionContinuationStatus.RECOVERY_EXHAUSTED
                )
            ),
            created_at=checkpoint.created_at,
            updated_at=checkpoint.created_at,
        )
        path = self._path(continuation.continuation_id)
        async with get_path_lock(path):
            try:
                payload = await read_json_async(path)
            except FileNotFoundError:
                payload = None
            if payload is not None:
                existing = self._parse(payload)
                if existing.checkpoint != checkpoint:
                    raise BackgroundActionConflictError(
                        "background Action continuation already conflicts",
                    )
                return existing
            await self._write(continuation)
        return continuation

    async def get(
        self,
        continuation_id: UUID,
    ) -> BackgroundActionContinuation | None:
        """Read one continuation, returning None when absent."""
        try:
            payload = await read_json_async(self._path(continuation_id))
        except FileNotFoundError:
            return None
        return self._parse(payload)

    async def list_pending(
        self,
        *,
        agent_id: str,
    ) -> tuple[BackgroundActionContinuation, ...]:
        """List waiting or ready continuations owned by one Agent."""
        paths = await run_sync_io(
            lambda: tuple(sorted(self._root.glob("*.json"))),
        )
        pending = []
        for path in paths:
            payload = await read_json_async(path)
            continuation = self._parse(payload)
            if (
                continuation.checkpoint.agent_id == agent_id
                and continuation.status
                in {
                    BackgroundActionContinuationStatus.WAITING_SOURCE,
                    BackgroundActionContinuationStatus.READY,
                }
            ):
                pending.append(continuation)
        pending.sort(key=lambda item: (item.created_at, item.continuation_id))
        return tuple(pending)

    async def transition(
        self,
        continuation_id: UUID,
        status: BackgroundActionContinuationStatus,
    ) -> BackgroundActionContinuation:
        """Move a waiting continuation to ready or cancelled."""
        if status not in {
            BackgroundActionContinuationStatus.READY,
            BackgroundActionContinuationStatus.CANCELLED,
        }:
            raise ValueError("unsupported background Action transition")
        path = self._path(continuation_id)
        async with get_path_lock(path):
            current = await self.get(continuation_id)
            if current is None:
                raise BackgroundActionConflictError(
                    "background Action continuation is unavailable",
                )
            if current.status is status:
                return current
            if current.status is not (
                BackgroundActionContinuationStatus.WAITING_SOURCE
            ):
                raise BackgroundActionConflictError(
                    "background Action continuation cannot transition",
                )
            updated = current.model_copy(
                update={
                    "status": status,
                    "revision": current.revision + 1,
                    "updated_at": utc_now(),
                },
            )
            await self._write(updated)
        return updated

    async def dispatch(
        self,
        continuation_id: UUID,
        dispatcher: BackgroundActionDispatcher,
    ) -> BackgroundActionContinuation:
        """Idempotently enqueue and bind one ready continuation."""
        current = await self.get(continuation_id)
        if current is None:
            raise BackgroundActionConflictError(
                "background Action continuation is unavailable",
            )
        if current.status is not BackgroundActionContinuationStatus.READY:
            return current
        submission_id = await dispatcher(current)
        next_status = (
            BackgroundActionContinuationStatus.DISPATCHED
            if submission_id is not None
            else BackgroundActionContinuationStatus.CANCELLED
        )
        path = self._path(continuation_id)
        async with get_path_lock(path):
            latest = await self.get(continuation_id)
            if latest is None:
                raise BackgroundActionConflictError(
                    "background Action continuation disappeared",
                )
            if latest.status is not BackgroundActionContinuationStatus.READY:
                return latest
            updated = latest.model_copy(
                update={
                    "status": next_status,
                    "submission_id": submission_id,
                    "revision": latest.revision + 1,
                    "updated_at": utc_now(),
                },
            )
            await self._write(updated)
        return updated


def build_background_action_checkpoint(
    *,
    committed_item: CommittedActionItem,
    source_submission_id: UUID,
    correlation_id: UUID,
    agent_id: str,
    recovery_cycle: int,
) -> BackgroundActionContextCheckpoint:
    """Build deterministic identities for one committed background result."""
    continuation_id = uuid5(
        committed_item.action_id,
        "background-action-continuation",
    )
    return BackgroundActionContextCheckpoint(
        checkpoint_id=uuid5(
            continuation_id,
            "private-agent-context",
        ),
        continuation_id=continuation_id,
        committed_item=committed_item,
        source_submission_id=source_submission_id,
        correlation_id=correlation_id,
        agent_id=agent_id,
        recovery_cycle=recovery_cycle,
    )


def lite_background_action_context_store(
    workspace_dir: Path,
) -> FilesystemBackgroundActionContextStore:
    """Return the private background Action context store."""
    return FilesystemBackgroundActionContextStore(workspace_dir)


def lite_background_action_continuation_store(
    workspace_dir: Path,
) -> FilesystemBackgroundActionContinuationStore:
    """Return the background Action continuation outbox."""
    return FilesystemBackgroundActionContinuationStore(workspace_dir)


__all__ = [
    "BACKGROUND_ACTION_CONTEXT_SCHEMA",
    "BACKGROUND_ACTION_CONTINUATION_SCHEMA",
    "BackgroundActionCompletionHandler",
    "BackgroundActionConflictError",
    "BackgroundActionContextNotFoundError",
    "FilesystemBackgroundActionContextStore",
    "FilesystemBackgroundActionContinuationStore",
    "build_background_action_checkpoint",
    "build_background_action_snapshot",
    "committed_item_from_response",
    "lite_background_action_context_store",
    "lite_background_action_continuation_store",
]
