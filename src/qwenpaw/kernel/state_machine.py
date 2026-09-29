# -*- coding: utf-8 -*-
"""Explicit task and run lifecycle transition rules."""

from __future__ import annotations

from .models import RunStatus, TaskStatus

TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.CREATED: frozenset(
        {TaskStatus.PLANNED, TaskStatus.CANCELLED},
    ),
    TaskStatus.PLANNED: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.CANCELLED,
        },
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.SUSPENDED,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        },
    ),
    TaskStatus.WAITING_APPROVAL: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        },
    ),
    TaskStatus.SUSPENDED: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        },
    ),
    TaskStatus.FAILED: frozenset({TaskStatus.PLANNED}),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}

RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PENDING: frozenset(
        {RunStatus.RUNNING, RunStatus.CANCELLED},
    ),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.WAITING_APPROVAL,
            RunStatus.SUSPENDED,
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        },
    ),
    RunStatus.WAITING_APPROVAL: frozenset(
        {
            RunStatus.RUNNING,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        },
    ),
    RunStatus.SUSPENDED: frozenset(
        {
            RunStatus.RUNNING,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        },
    ),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


class InvalidTaskTransition(ValueError):
    """Raised when a task lifecycle transition violates the contract."""

    def __init__(self, current: TaskStatus, target: TaskStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(
            f"invalid task transition: {current.value} -> {target.value}",
        )


class InvalidRunTransition(ValueError):
    """Raised when a run lifecycle transition violates the contract."""

    def __init__(self, current: RunStatus, target: RunStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(
            f"invalid run transition: {current.value} -> {target.value}",
        )


def validate_task_transition(
    current: TaskStatus,
    target: TaskStatus,
) -> None:
    """Validate one task transition or raise a domain error."""
    if target not in TASK_TRANSITIONS[current]:
        raise InvalidTaskTransition(current, target)


def validate_run_transition(
    current: RunStatus,
    target: RunStatus,
) -> None:
    """Validate one run transition or raise a domain error."""
    if target not in RUN_TRANSITIONS[current]:
        raise InvalidRunTransition(current, target)


def is_task_terminal(status: TaskStatus) -> bool:
    """Return whether a task can never transition again."""
    return not TASK_TRANSITIONS[status]


def is_run_terminal(status: RunStatus) -> bool:
    """Return whether a run can never transition again."""
    return not RUN_TRANSITIONS[status]
