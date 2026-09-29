# -*- coding: utf-8 -*-
"""Versioned HTTP input contracts for the Task API."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..kernel.models import (
    ApprovalDecisionValue,
    ExecutionContract,
    Task,
    TaskSource,
)
from ..kernel.events import ExecutionEvent


class TaskHttpModel(BaseModel):
    """Strict base for public Task HTTP payloads."""

    model_config = ConfigDict(extra="forbid")


class CreateTaskRequest(TaskHttpModel):
    """Public task creation payload."""

    objective: str = Field(min_length=1)
    constraints: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    execution_contract: ExecutionContract | None = None
    source: TaskSource = TaskSource.USER
    project_dir: str | None = None
    runner_id: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9_.-]*$",
    )
    strategy_id: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9_.-]*$",
    )
    approval_level: Literal["strict", "smart", "auto", "off"] | None = None


class ApprovalDecisionRequest(TaskHttpModel):
    """Public approval resolution payload."""

    decision: ApprovalDecisionValue
    reason: str = Field(min_length=1)
    scope: str = Field(default="exact", pattern=r"^(exact|similar)$")


class SideEffectRetryRequest(TaskHttpModel):
    """Explicit acknowledgement before retrying an uncertain write."""

    reason: str = Field(min_length=1)


class TaskListResponse(TaskHttpModel):
    """Stable cursor page returned by ``GET /api/tasks``."""

    items: tuple[Task, ...]
    next_cursor: str | None = None


class TaskEventPageResponse(TaskHttpModel):
    """Stable sequence page returned by the Task event endpoint."""

    items: tuple[ExecutionEvent, ...]
    next_sequence: int = Field(ge=0)


__all__ = [
    "ApprovalDecisionRequest",
    "CreateTaskRequest",
    "SideEffectRetryRequest",
    "TaskEventPageResponse",
    "TaskListResponse",
]
