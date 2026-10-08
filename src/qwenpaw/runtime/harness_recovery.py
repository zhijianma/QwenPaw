# -*- coding: utf-8 -*-
"""Durable, content-safe admission evidence for Harness recovery."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid5

from ..kernel import (
    CommittedActionItem,
    HarnessRecoveryContextCheckpoint,
)
from ..utils.io_utils import (
    get_path_lock,
    read_json_async,
    write_json_atomic_async,
)

HARNESS_RECOVERY_CONTEXT_SCHEMA = "qwenpaw.harness-recovery-context.v1"


class HarnessRecoveryContextConflictError(RuntimeError):
    """Raised when one checkpoint identity has conflicting evidence."""


class HarnessRecoveryContextNotFoundError(RuntimeError):
    """Raised when one Harness recovery checkpoint is unavailable."""


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
        conversation_id=conversation_id,
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


__all__ = [
    "FilesystemHarnessRecoveryContextStore",
    "HARNESS_RECOVERY_CONTEXT_SCHEMA",
    "HarnessRecoveryContextConflictError",
    "HarnessRecoveryContextNotFoundError",
    "build_harness_recovery_checkpoint",
    "lite_harness_recovery_context_store",
]
