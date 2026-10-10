# -*- coding: utf-8 -*-
"""Versioned HTTP contracts for the Task API."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from ..kernel.events import ExecutionEvent
from ..kernel.models import (
    ApprovalDecision,
    ApprovalDecisionValue,
    ApprovalRequest,
    ArtifactPreviewDescriptor,
    ArtifactRecord,
    ArtifactRef,
    EvidenceRecord,
    EvidenceRef,
    ExecutionCheckpoint,
    ExecutionContract,
    NamespacedId,
    Plan,
    ResultPackage,
    Run,
    Task,
    TaskSource,
    UsageSnapshot,
    VerificationRecord,
    VerificationResult,
)


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


class TaskConversationMessageResponse(TaskHttpModel):
    """One committed Chat message projected into the Task Workbench."""

    role: Literal["user", "assistant"]
    text: str
    run_id: UUID | None
    created_at: AwareDatetime
    completed_at: AwareDatetime


class TaskApprovalResponse(ApprovalRequest):
    """One approval request with its optional immutable decision."""

    decision: ApprovalDecision | None


class TaskArtifactResponse(ArtifactRef):
    """One immutable Artifact with current preview availability."""

    preview: ArtifactPreviewDescriptor


class TaskCapabilitySelectionResponse(TaskHttpModel):
    """One generation-pinned capability visible in the projection."""

    capability_id: NamespacedId
    slot: NamespacedId
    registry_generation: int = Field(ge=1)


class TaskProjectionResponse(TaskHttpModel):
    """Authoritative, bounded Task Workbench read contract."""

    task: Task
    active_run: Run | None
    runs: tuple[Run, ...]
    latest_plan: Plan | None
    conversation_messages: tuple[TaskConversationMessageResponse, ...]
    tool_activities: tuple[ExecutionEvent, ...]
    pending_approvals: tuple[TaskApprovalResponse, ...]
    recent_decisions: tuple[TaskApprovalResponse, ...]
    artifacts: tuple[TaskArtifactResponse, ...]
    artifact_registry: tuple[ArtifactRecord, ...]
    evidence: tuple[EvidenceRef, ...]
    evidence_registry: tuple[EvidenceRecord, ...]
    verifications: tuple[VerificationResult, ...]
    verification_registry: tuple[VerificationRecord, ...]
    result_package: ResultPackage | None
    usage: UsageSnapshot
    checkpoint: ExecutionCheckpoint | None
    capabilities: tuple[TaskCapabilitySelectionResponse, ...]
    last_sequence: int = Field(ge=0)


__all__ = [
    "ApprovalDecisionRequest",
    "CreateTaskRequest",
    "SideEffectRetryRequest",
    "TaskEventPageResponse",
    "TaskListResponse",
    "TaskApprovalResponse",
    "TaskArtifactResponse",
    "TaskCapabilitySelectionResponse",
    "TaskConversationMessageResponse",
    "TaskProjectionResponse",
]
