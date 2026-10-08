# -*- coding: utf-8 -*-
"""Immutable private context snapshots for model-step continuation."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

from ..kernel import ModelStepContextCheckpoint
from ..utils.io_utils import (
    get_path_lock,
    read_json_async,
    write_json_atomic_async,
)

MODEL_STEP_CONTEXT_SCHEMA = "qwenpaw.model-step-context.v1"


class ModelStepContextConflictError(RuntimeError):
    """Raised when one checkpoint identity has different private state."""


class ModelStepContextNotFoundError(RuntimeError):
    """Raised when checkpoint metadata exists without its private state."""


class FilesystemModelStepContextStore:
    """Owner-only immutable Agent state snapshots for Lite recovery."""

    def __init__(self, workspace_dir: Path) -> None:
        self._root = (
            Path(workspace_dir)
            / ".qwenpaw"
            / "lite"
            / "model-step-contexts"
        )

    def _path(self, checkpoint_id: UUID) -> Path:
        return self._root / f"{checkpoint_id}.json"

    @staticmethod
    def _parse(
        payload: object,
    ) -> tuple[ModelStepContextCheckpoint, dict[str, Any]]:
        if not isinstance(payload, dict):
            raise ModelStepContextConflictError(
                "model-step context payload must be an object",
            )
        if payload.get("schema") != MODEL_STEP_CONTEXT_SCHEMA:
            raise ModelStepContextConflictError(
                "unsupported model-step context schema",
            )
        raw_state = payload.get("agent_state")
        if not isinstance(raw_state, dict):
            raise ModelStepContextConflictError(
                "model-step context has no Agent state",
            )
        checkpoint = ModelStepContextCheckpoint.model_validate(
            payload.get("checkpoint"),
        )
        return checkpoint, raw_state

    @staticmethod
    def _same_checkpoint(
        left: ModelStepContextCheckpoint,
        right: ModelStepContextCheckpoint,
    ) -> bool:
        return left.model_dump(exclude={"created_at"}) == right.model_dump(
            exclude={"created_at"},
        )

    async def save(
        self,
        checkpoint: ModelStepContextCheckpoint,
        agent_state: dict[str, Any],
    ) -> ModelStepContextCheckpoint:
        """Append one immutable snapshot before publishing its reference."""
        path = self._path(checkpoint.checkpoint_id)
        async with get_path_lock(path):
            try:
                payload = await read_json_async(path)
            except FileNotFoundError:
                payload = None
            if payload is not None:
                existing, existing_state = self._parse(payload)
                if (
                    not self._same_checkpoint(existing, checkpoint)
                    or existing_state != agent_state
                ):
                    raise ModelStepContextConflictError(
                        "model-step context already has different state",
                    )
                return existing
            await write_json_atomic_async(
                path,
                {
                    "schema": MODEL_STEP_CONTEXT_SCHEMA,
                    "checkpoint": checkpoint.model_dump(mode="json"),
                    "agent_state": agent_state,
                },
                sort_keys=True,
            )
        return checkpoint

    async def load(
        self,
        checkpoint_id: UUID,
    ) -> tuple[ModelStepContextCheckpoint, dict[str, Any]]:
        """Load one exact private snapshot and its content-safe reference."""
        try:
            payload = await read_json_async(self._path(checkpoint_id))
        except FileNotFoundError as exc:
            raise ModelStepContextNotFoundError(
                str(checkpoint_id),
            ) from exc
        checkpoint, state = self._parse(payload)
        if checkpoint.checkpoint_id != checkpoint_id:
            raise ModelStepContextConflictError(
                "model-step context filename identity mismatch",
            )
        return checkpoint, state


def context_has_terminal_tool_results(
    agent_state: dict[str, Any],
    bindings: tuple[tuple[str, str], ...],
) -> bool:
    """Verify that an Agent snapshot contains every terminal ToolResult."""
    expected = dict(bindings)
    if not expected:
        return False
    found: set[tuple[str, str]] = set()
    state = agent_state.get("state")
    context = state.get("context") if isinstance(state, dict) else None
    if not isinstance(context, list):
        return False
    for message in context:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            call_id = block.get("id")
            metadata = block.get("metadata")
            action_id = (
                metadata.get("qwenpaw_action_id")
                if isinstance(metadata, dict)
                else None
            )
            if (
                block.get("type") == "tool_result"
                and call_id in expected
                and action_id == expected[call_id]
                and block.get("state") != "running"
            ):
                found.add((str(call_id), str(action_id)))
    return found == set(expected.items())


def lite_model_step_context_store(
    workspace_dir: Path,
) -> FilesystemModelStepContextStore:
    """Return the private Lite checkpoint store for one workspace."""
    return FilesystemModelStepContextStore(workspace_dir)


__all__ = [
    "FilesystemModelStepContextStore",
    "MODEL_STEP_CONTEXT_SCHEMA",
    "ModelStepContextConflictError",
    "ModelStepContextNotFoundError",
    "context_has_terminal_tool_results",
    "lite_model_step_context_store",
]
