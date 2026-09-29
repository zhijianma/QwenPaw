# -*- coding: utf-8 -*-
"""Contract tests for task and run lifecycle state machines."""

import pytest

from qwenpaw.kernel.models import RunStatus, TaskStatus
from qwenpaw.kernel.state_machine import (
    InvalidRunTransition,
    InvalidTaskTransition,
    is_run_terminal,
    is_task_terminal,
    validate_run_transition,
    validate_task_transition,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (TaskStatus.CREATED, TaskStatus.PLANNED),
        (TaskStatus.PLANNED, TaskStatus.RUNNING),
        (TaskStatus.RUNNING, TaskStatus.WAITING_APPROVAL),
        (TaskStatus.WAITING_APPROVAL, TaskStatus.RUNNING),
        (TaskStatus.RUNNING, TaskStatus.SUSPENDED),
        (TaskStatus.SUSPENDED, TaskStatus.RUNNING),
        (TaskStatus.RUNNING, TaskStatus.COMPLETED),
        (TaskStatus.RUNNING, TaskStatus.FAILED),
        (TaskStatus.FAILED, TaskStatus.PLANNED),
        (TaskStatus.CREATED, TaskStatus.CANCELLED),
    ],
)
def test_task_state_machine_accepts_declared_transitions(
    current: TaskStatus,
    target: TaskStatus,
) -> None:
    validate_task_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (TaskStatus.CREATED, TaskStatus.COMPLETED),
        (TaskStatus.PLANNED, TaskStatus.COMPLETED),
        (TaskStatus.COMPLETED, TaskStatus.RUNNING),
        (TaskStatus.CANCELLED, TaskStatus.PLANNED),
    ],
)
def test_task_state_machine_rejects_invalid_transitions(
    current: TaskStatus,
    target: TaskStatus,
) -> None:
    with pytest.raises(InvalidTaskTransition):
        validate_task_transition(current, target)


def test_task_terminal_states_are_explicit() -> None:
    assert is_task_terminal(TaskStatus.COMPLETED)
    assert is_task_terminal(TaskStatus.CANCELLED)
    assert not is_task_terminal(TaskStatus.FAILED)
    assert not is_task_terminal(TaskStatus.SUSPENDED)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (RunStatus.PENDING, RunStatus.RUNNING),
        (RunStatus.RUNNING, RunStatus.WAITING_APPROVAL),
        (RunStatus.WAITING_APPROVAL, RunStatus.RUNNING),
        (RunStatus.RUNNING, RunStatus.SUSPENDED),
        (RunStatus.SUSPENDED, RunStatus.RUNNING),
        (RunStatus.RUNNING, RunStatus.SUCCEEDED),
        (RunStatus.RUNNING, RunStatus.FAILED),
        (RunStatus.PENDING, RunStatus.CANCELLED),
    ],
)
def test_run_state_machine_accepts_declared_transitions(
    current: RunStatus,
    target: RunStatus,
) -> None:
    validate_run_transition(current, target)


def test_run_terminal_states_cannot_resume_in_place() -> None:
    assert is_run_terminal(RunStatus.SUCCEEDED)
    assert is_run_terminal(RunStatus.FAILED)
    assert is_run_terminal(RunStatus.CANCELLED)
    with pytest.raises(InvalidRunTransition):
        validate_run_transition(RunStatus.FAILED, RunStatus.RUNNING)
