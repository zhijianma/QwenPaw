# -*- coding: utf-8 -*-
"""Framework-independent Task Workbench read model."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from ..kernel.events import ExecutionEvent
from ..kernel.models import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    ExecutionCheckpoint,
    Plan,
    Run,
    Task,
    UsageSnapshot,
)
from .results import TaskResultProjection, load_task_result_projection
from .service import TaskNotFoundError, TaskService
from .usage import load_task_usage_snapshot


@dataclass(frozen=True, slots=True)
class ConversationMessageProjection:
    """One public message assembled from committed execution events."""

    role: Literal["user", "assistant"]
    text: str
    run_id: UUID | None
    created_at: datetime
    completed_at: datetime

    def to_public_dict(self) -> dict[str, Any]:
        """Serialize the stable transport-neutral public shape."""
        return {
            "role": self.role,
            "text": self.text,
            "run_id": str(self.run_id) if self.run_id else None,
            "created_at": self.created_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ApprovalWorkbenchItem:
    """One approval request paired with its immutable decision."""

    request: ApprovalRequest
    decision: ApprovalDecision | None

    @property
    def status(self) -> ApprovalStatus:
        """Return the projected lifecycle status for the request."""
        if self.decision is None:
            return ApprovalStatus.PENDING
        return ApprovalStatus(self.decision.decision.value)

    def to_public_dict(self) -> dict[str, Any]:
        """Preserve the existing flat Workbench approval response."""
        item = self.request.model_dump(mode="json")
        item["status"] = self.status.value
        item["decision"] = (
            self.decision.model_dump(mode="json")
            if self.decision is not None
            else None
        )
        return item


def project_conversation_messages(
    events: Iterable[ExecutionEvent],
) -> tuple[ConversationMessageProjection, ...]:
    """Assemble public user and assistant text from committed events."""
    messages: list[ConversationMessageProjection] = []
    for event in events:
        if event.event_type not in {
            "conversation.user",
            "conversation.assistant.delta",
        }:
            continue
        role = event.payload.get("role")
        text = event.payload.get("text")
        if role not in {"user", "assistant"} or not isinstance(text, str):
            continue
        if role == "assistant" and messages:
            previous = messages[-1]
            if (
                previous.role == "assistant"
                and previous.run_id == event.run_id
            ):
                messages[-1] = ConversationMessageProjection(
                    role="assistant",
                    text=f"{previous.text}{text}",
                    run_id=event.run_id,
                    created_at=previous.created_at,
                    completed_at=event.occurred_at,
                )
                continue
        messages.append(
            ConversationMessageProjection(
                role=role,
                text=text,
                run_id=event.run_id,
                created_at=event.occurred_at,
                completed_at=event.occurred_at,
            ),
        )
    return tuple(messages)


def project_approval_items(
    approvals: Iterable[
        tuple[ApprovalRequest, ApprovalDecision | None]
    ],
) -> tuple[
    tuple[ApprovalWorkbenchItem, ...],
    tuple[ApprovalWorkbenchItem, ...],
]:
    """Split approvals into pending requests and recent decisions."""
    pending: list[ApprovalWorkbenchItem] = []
    decided: list[ApprovalWorkbenchItem] = []
    for request, decision in approvals:
        item = ApprovalWorkbenchItem(request=request, decision=decision)
        if decision is None:
            pending.append(item)
        else:
            decided.append(item)
    return tuple(pending), tuple(decided[-20:])


def project_tool_activities(
    events: Iterable[ExecutionEvent],
) -> tuple[ExecutionEvent, ...]:
    """Return the authoritative tool events shown by the Workbench."""
    return tuple(
        event for event in events if event.event_type.startswith("tool.")
    )


@dataclass(frozen=True, slots=True)
class TaskWorkbenchReadModel:
    """Consistent aggregate data needed by Task-facing adapters."""

    task: Task
    active_run: Run | None
    runs: tuple[Run, ...]
    latest_plan: Plan | None
    approvals: tuple[
        tuple[ApprovalRequest, ApprovalDecision | None],
        ...,
    ]
    events: tuple[ExecutionEvent, ...]
    conversation_messages: tuple[ConversationMessageProjection, ...]
    tool_activities: tuple[ExecutionEvent, ...]
    pending_approvals: tuple[ApprovalWorkbenchItem, ...]
    recent_decisions: tuple[ApprovalWorkbenchItem, ...]
    checkpoint: ExecutionCheckpoint | None
    usage: UsageSnapshot
    results: TaskResultProjection
    last_sequence: int


async def load_task_workbench(
    service: TaskService,
    task_id: UUID,
) -> TaskWorkbenchReadModel:
    """Load bounded interaction data and complete durable projections."""
    snapshot = await service.projection_snapshot(task_id)
    if snapshot.task is None:
        raise TaskNotFoundError(str(task_id))
    task = snapshot.task
    active_run = next(
        (run for run in snapshot.runs if run.run_id == task.active_run_id),
        None,
    )
    usage = await load_task_usage_snapshot(
        service,
        task_id,
        through_sequence=snapshot.last_sequence,
    )
    results = await load_task_result_projection(
        service,
        task_id,
        through_sequence=snapshot.last_sequence,
    )
    pending_approvals, recent_decisions = project_approval_items(
        snapshot.approvals,
    )
    return TaskWorkbenchReadModel(
        task=task,
        active_run=active_run,
        runs=snapshot.runs,
        latest_plan=snapshot.latest_plan,
        approvals=snapshot.approvals,
        events=snapshot.events,
        conversation_messages=project_conversation_messages(
            snapshot.events,
        ),
        tool_activities=project_tool_activities(snapshot.events),
        pending_approvals=pending_approvals,
        recent_decisions=recent_decisions,
        checkpoint=snapshot.checkpoint,
        usage=usage,
        results=results,
        last_sequence=snapshot.last_sequence,
    )


__all__ = [
    "ApprovalWorkbenchItem",
    "ConversationMessageProjection",
    "TaskWorkbenchReadModel",
    "load_task_workbench",
    "project_approval_items",
    "project_conversation_messages",
    "project_tool_activities",
]
