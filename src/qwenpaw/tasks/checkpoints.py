# -*- coding: utf-8 -*-
"""Run-scoped host adapter for durable safe checkpoints."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from pydantic import JsonValue

from ..kernel.models import ExecutionCheckpoint
from .service import TaskService


@dataclass(frozen=True, slots=True)
class TaskCheckpointBroker:
    """Bind checkpoint creation to one host-owned Task and Run."""

    service: TaskService
    task_id: UUID
    run_id: UUID

    async def save(
        self,
        *,
        runner_cursor: JsonValue = None,
        workspace_checkpoint_ref: str | None = None,
        idempotency_key: str | None = None,
    ) -> ExecutionCheckpoint:
        """Persist one safe boundary without exposing host identities."""
        return await self.service.create_checkpoint(
            self.task_id,
            self.run_id,
            runner_cursor=runner_cursor,
            workspace_checkpoint_ref=workspace_checkpoint_ref,
            idempotency_key=idempotency_key,
        )


__all__ = ["TaskCheckpointBroker"]
