# -*- coding: utf-8 -*-
"""Durable, content-safe admission evidence for Harness recovery."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import UUID, uuid5

from ..kernel import (
    CommittedActionItem,
    HarnessRecoveryContextCheckpoint,
    HarnessStepContinuation,
    HarnessStepContinuationStatus,
)
from ..kernel.models import utc_now
from ..utils.io_utils import (
    get_path_lock,
    read_json_async,
    run_sync_io,
    write_json_atomic_async,
)

HARNESS_RECOVERY_CONTEXT_SCHEMA = "qwenpaw.harness-recovery-context.v1"
HARNESS_STEP_CONTINUATION_SCHEMA = "qwenpaw.harness-step-continuation.v1"
HarnessStepDispatcher = Callable[
    [HarnessStepContinuation],
    Awaitable[UUID | None],
]


class HarnessRecoveryContextConflictError(RuntimeError):
    """Raised when one checkpoint identity has conflicting evidence."""


class HarnessRecoveryContextNotFoundError(RuntimeError):
    """Raised when one Harness recovery checkpoint is unavailable."""


class HarnessStepContinuationConflictError(RuntimeError):
    """Raised when one continuation identity has conflicting state."""


def _digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def build_harness_recovery_checkpoint(
    *,
    invocation_id: UUID,
    conversation_id: str,
    source_submission_id: UUID,
    backend: str,
    provider_context_id: str,
    provider_item_ids: set[str],
    action_evidence_digest: str,
    expected_items: tuple[CommittedActionItem, ...],
    session_items: tuple[CommittedActionItem, ...],
) -> HarnessRecoveryContextCheckpoint | None:
    """Admit recovery only when provider, session, and Actions agree."""
    if not backend.strip() or not provider_context_id.strip():
        return None
    expected = {
        (
            str(item.action_id),
            str(item.invocation_id),
            item.conversation_id,
            item.executor_item_id,
            item.observation_digest,
        )
        for item in expected_items
    }
    if not expected:
        return None
    durable_session = {
        (
            str(item.action_id),
            str(item.invocation_id),
            item.conversation_id,
            item.executor_item_id,
            item.observation_digest,
        )
        for item in session_items
    }
    if not expected.issubset(durable_session):
        return None
    if any(
        item.invocation_id != invocation_id
        or item.conversation_id != conversation_id
        or item.executor_item_id not in provider_item_ids
        for item in expected_items
    ):
        return None
    committed_provider_items = sorted(
        item.executor_item_id for item in expected_items
    )
    return HarnessRecoveryContextCheckpoint(
        checkpoint_id=uuid5(
            invocation_id,
            f"harness-recovery-context:{backend}",
        ),
        invocation_id=invocation_id,
        chat_id=conversation_id,
        source_submission_id=source_submission_id,
        backend=backend,
        provider_context_digest=_digest(
            {
                "backend": backend,
                "context_id": provider_context_id,
                "invocation_id": str(invocation_id),
                "source_submission_id": str(source_submission_id),
            },
        ),
        provider_item_digest=_digest(committed_provider_items),
        action_evidence_digest=action_evidence_digest,
        action_count=len(expected_items),
    )


class FilesystemHarnessRecoveryContextStore:
    """Owner-only immutable Harness recovery admission store for Lite."""

    def __init__(self, workspace_dir: Path) -> None:
        self._root = (
            Path(workspace_dir)
            / ".qwenpaw"
            / "lite"
            / "harness-recovery-contexts"
        )

    def _path(self, checkpoint_id: UUID) -> Path:
        return self._root / f"{checkpoint_id}.json"

    @staticmethod
    def _parse(payload: object) -> HarnessRecoveryContextCheckpoint:
        if not isinstance(payload, dict):
            raise HarnessRecoveryContextConflictError(
                "Harness recovery context payload must be an object",
            )
        if payload.get("schema") != HARNESS_RECOVERY_CONTEXT_SCHEMA:
            raise HarnessRecoveryContextConflictError(
                "unsupported Harness recovery context schema",
            )
        return HarnessRecoveryContextCheckpoint.model_validate(
            payload.get("checkpoint"),
        )

    async def save(
        self,
        checkpoint: HarnessRecoveryContextCheckpoint,
    ) -> HarnessRecoveryContextCheckpoint:
        """Append one immutable checkpoint before publishing its ID."""
        path = self._path(checkpoint.checkpoint_id)
        async with get_path_lock(path):
            try:
                payload = await read_json_async(path)
            except FileNotFoundError:
                payload = None
            if payload is not None:
                existing = self._parse(payload)
                if existing.model_dump(exclude={"created_at"}) != (
                    checkpoint.model_dump(exclude={"created_at"})
                ):
                    raise HarnessRecoveryContextConflictError(
                        "Harness recovery checkpoint already conflicts",
                    )
                return existing
            await write_json_atomic_async(
                path,
                {
                    "schema": HARNESS_RECOVERY_CONTEXT_SCHEMA,
                    "checkpoint": checkpoint.model_dump(mode="json"),
                },
                sort_keys=True,
            )
        return checkpoint

    async def load(
        self,
        checkpoint_id: UUID,
    ) -> HarnessRecoveryContextCheckpoint:
        """Load one exact Harness recovery checkpoint."""
        try:
            payload = await read_json_async(self._path(checkpoint_id))
        except FileNotFoundError as exc:
            raise HarnessRecoveryContextNotFoundError(
                str(checkpoint_id),
            ) from exc
        checkpoint = self._parse(payload)
        if checkpoint.checkpoint_id != checkpoint_id:
            raise HarnessRecoveryContextConflictError(
                "Harness recovery checkpoint filename identity mismatch",
            )
        return checkpoint


def lite_harness_recovery_context_store(
    workspace_dir: Path,
) -> FilesystemHarnessRecoveryContextStore:
    """Return the Lite Harness recovery admission store."""
    return FilesystemHarnessRecoveryContextStore(workspace_dir)


class FilesystemHarnessStepContinuationStore:
    """Durable Harness continuation outbox consumed by the Chat dispatcher."""

    def __init__(self, workspace_dir: Path) -> None:
        self._root = (
            Path(workspace_dir)
            / ".qwenpaw"
            / "lite"
            / "harness-step-continuations"
        )

    def _path(self, continuation_id: UUID) -> Path:
        return self._root / f"{continuation_id}.json"

    @staticmethod
    def _parse(payload: object) -> HarnessStepContinuation:
        if not isinstance(payload, dict):
            raise HarnessStepContinuationConflictError(
                "Harness continuation payload must be an object",
            )
        if payload.get("schema") != HARNESS_STEP_CONTINUATION_SCHEMA:
            raise HarnessStepContinuationConflictError(
                "unsupported Harness continuation schema",
            )
        return HarnessStepContinuation.model_validate(
            payload.get("continuation"),
        )

    async def _write(self, continuation: HarnessStepContinuation) -> None:
        await write_json_atomic_async(
            self._path(continuation.continuation_id),
            {
                "schema": HARNESS_STEP_CONTINUATION_SCHEMA,
                "continuation": continuation.model_dump(mode="json"),
            },
            sort_keys=True,
        )

    async def defer(
        self,
        checkpoint: HarnessRecoveryContextCheckpoint,
        *,
        correlation_id: UUID,
        agent_id: str,
        recovery_cycle: int,
        max_recovery_cycles: int = 2,
    ) -> HarnessStepContinuation:
        """Create one idempotent continuation after durable admission."""
        continuation = HarnessStepContinuation(
            continuation_id=uuid5(
                checkpoint.checkpoint_id,
                "harness-step-continuation",
            ),
            checkpoint=checkpoint,
            correlation_id=correlation_id,
            agent_id=agent_id,
            recovery_cycle=recovery_cycle,
            status=(
                HarnessStepContinuationStatus.READY
                if recovery_cycle <= max_recovery_cycles
                else HarnessStepContinuationStatus.RECOVERY_EXHAUSTED
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
                immutable = {
                    "continuation_id",
                    "checkpoint",
                    "correlation_id",
                    "agent_id",
                    "recovery_cycle",
                    "created_at",
                }
                if existing.model_dump(include=immutable) != (
                    continuation.model_dump(include=immutable)
                ):
                    raise HarnessStepContinuationConflictError(
                        "Harness continuation already conflicts",
                    )
                return existing
            await self._write(continuation)
        return continuation

    async def get(
        self,
        continuation_id: UUID,
    ) -> HarnessStepContinuation | None:
        """Read one continuation, returning None when it is absent."""
        try:
            payload = await read_json_async(self._path(continuation_id))
        except FileNotFoundError:
            return None
        return self._parse(payload)

    async def list_ready(
        self,
        *,
        agent_id: str,
    ) -> tuple[HarnessStepContinuation, ...]:
        """List ready continuations owned by one Agent."""
        paths = await run_sync_io(
            lambda: tuple(sorted(self._root.glob("*.json"))),
        )
        ready = []
        for path in paths:
            try:
                payload = await read_json_async(path)
                continuation = self._parse(payload)
            except (OSError, TypeError, ValueError):
                continue
            if (
                continuation.agent_id == agent_id
                and continuation.status
                is HarnessStepContinuationStatus.READY
            ):
                ready.append(continuation)
        ready.sort(key=lambda item: (item.created_at, item.continuation_id))
        return tuple(ready)

    async def dispatch(
        self,
        continuation_id: UUID,
        dispatcher: HarnessStepDispatcher,
    ) -> HarnessStepContinuation:
        """Idempotently enqueue and bind one ready continuation."""
        current = await self.get(continuation_id)
        if current is None:
            raise HarnessStepContinuationConflictError(
                "Harness continuation is unavailable",
            )
        if current.status is not HarnessStepContinuationStatus.READY:
            return current
        submission_id = await dispatcher(current)
        next_status = (
            HarnessStepContinuationStatus.DISPATCHED
            if submission_id is not None
            else HarnessStepContinuationStatus.CANCELLED
        )
        path = self._path(continuation_id)
        async with get_path_lock(path):
            latest = await self.get(continuation_id)
            if latest is None:
                raise HarnessStepContinuationConflictError(
                    "Harness continuation disappeared during dispatch",
                )
            if latest.status is not HarnessStepContinuationStatus.READY:
                if (
                    latest.submission_id is not None
                    and submission_id is not None
                    and latest.submission_id != submission_id
                ):
                    raise HarnessStepContinuationConflictError(
                        "Harness continuation submission conflict",
                    )
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


def lite_harness_step_continuation_store(
    workspace_dir: Path,
) -> FilesystemHarnessStepContinuationStore:
    """Return the Lite Harness continuation outbox."""
    return FilesystemHarnessStepContinuationStore(workspace_dir)


__all__ = [
    "FilesystemHarnessRecoveryContextStore",
    "FilesystemHarnessStepContinuationStore",
    "HARNESS_RECOVERY_CONTEXT_SCHEMA",
    "HARNESS_STEP_CONTINUATION_SCHEMA",
    "HarnessRecoveryContextConflictError",
    "HarnessRecoveryContextNotFoundError",
    "HarnessStepContinuationConflictError",
    "build_harness_recovery_checkpoint",
    "lite_harness_recovery_context_store",
    "lite_harness_step_continuation_store",
]
