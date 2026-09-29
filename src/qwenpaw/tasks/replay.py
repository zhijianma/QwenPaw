# -*- coding: utf-8 -*-
"""Deterministic projection of an execution event timeline."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from ..kernel.events import ExecutionEvent
from ..kernel.models import RunStatus, TaskStatus
from ..kernel.state_machine import (
    InvalidRunTransition,
    InvalidTaskTransition,
    validate_run_transition,
    validate_task_transition,
)


class ReplayError(ValueError):
    """Raised when persisted events cannot form a valid execution state."""


@dataclass(frozen=True)
class ReplayedExecution:
    """Current task and run state reconstructed from immutable events."""

    task_id: UUID
    task_status: TaskStatus
    run_id: UUID | None
    run_status: RunStatus | None
    latest_sequence: int


def _task_target(event_type: str) -> TaskStatus | None:
    return {
        "task.planned": TaskStatus.PLANNED,
        "task.cancelled": TaskStatus.CANCELLED,
        "run.started": TaskStatus.RUNNING,
        "approval.requested": TaskStatus.WAITING_APPROVAL,
        "run.resumed": TaskStatus.RUNNING,
        "run.suspended": TaskStatus.SUSPENDED,
        "run.completed": TaskStatus.COMPLETED,
        "run.failed": TaskStatus.FAILED,
        "run.cancelled": TaskStatus.CANCELLED,
    }.get(event_type)


def _run_target(event_type: str) -> RunStatus | None:
    return {
        "run.started": RunStatus.RUNNING,
        "approval.requested": RunStatus.WAITING_APPROVAL,
        "run.resumed": RunStatus.RUNNING,
        "run.suspended": RunStatus.SUSPENDED,
        "run.completed": RunStatus.SUCCEEDED,
        "run.failed": RunStatus.FAILED,
        "run.cancelled": RunStatus.CANCELLED,
    }.get(event_type)


def replay_execution(
    events: list[ExecutionEvent],
) -> ReplayedExecution:
    """Replay a complete ordered task timeline into its current state."""
    if not events:
        raise ReplayError("cannot replay an empty event timeline")

    first = events[0]
    if first.sequence != 1 or first.event_type != "task.created":
        raise ReplayError("sequence 1 must be task.created")

    task_id = first.task_id
    task_status = TaskStatus.CREATED
    run_id: UUID | None = None
    run_status: RunStatus | None = None
    expected_sequence = 1

    for event in events:
        if event.task_id != task_id:
            raise ReplayError("event timeline contains more than one task")
        if event.sequence != expected_sequence:
            raise ReplayError(
                f"event sequence mismatch: expected {expected_sequence}",
            )
        expected_sequence += 1

        task_target = _task_target(event.event_type)
        run_target = _run_target(event.event_type)
        try:
            if task_target is not None:
                validate_task_transition(task_status, task_target)
                task_status = task_target

            if event.event_type == "run.started":
                if event.run_id is None:
                    raise ReplayError("run.started requires run_id")
                run_id = event.run_id
                run_status = RunStatus.PENDING

            if run_target is not None:
                if event.run_id is None:
                    raise ReplayError(
                        f"{event.event_type} requires run_id",
                    )
                if run_id != event.run_id or run_status is None:
                    raise ReplayError(
                        f"{event.event_type} references an inactive run",
                    )
                validate_run_transition(run_status, run_target)
                run_status = run_target
        except (InvalidTaskTransition, InvalidRunTransition) as exc:
            raise ReplayError(
                f"invalid transition while replaying {event.event_type}: "
                f"{exc}",
            ) from exc

    return ReplayedExecution(
        task_id=task_id,
        task_status=task_status,
        run_id=run_id,
        run_status=run_status,
        latest_sequence=events[-1].sequence,
    )
