# -*- coding: utf-8 -*-
"""Private execution input for durable Action retry admission."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid5

from ..kernel import (
    ActionRequest,
    ActionRetryDecision,
    ActionRetryDisposition,
    ActionRetryInputCheckpoint,
)
from ..kernel.models import JsonObject
from ..utils.io_utils import (
    get_path_lock,
    read_json_async,
    write_json_atomic_async,
)

ACTION_RETRY_INPUT_SCHEMA = "qwenpaw.action-retry-input.v1"


class ActionRetryInputConflictError(RuntimeError):
    """Raised when private retry input conflicts with durable identity."""


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


def lite_action_retry_input_store(
    workspace_dir: Path,
) -> FilesystemActionRetryInputStore:
    """Return the Lite private Action retry input store."""
    return FilesystemActionRetryInputStore(workspace_dir)


__all__ = [
    "ACTION_RETRY_INPUT_SCHEMA",
    "ActionRetryInputConflictError",
    "FilesystemActionRetryInputStore",
    "lite_action_retry_input_store",
]
