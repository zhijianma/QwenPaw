# -*- coding: utf-8 -*-
"""Tests for deterministic task event replay."""

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from qwenpaw.kernel.events import ExecutionEvent
from qwenpaw.kernel.models import ActorRef, ActorType, RunStatus, TaskStatus
from qwenpaw.tasks.replay import ReplayError, replay_execution


def _event(
    *,
    task_id: UUID,
    sequence: int,
    event_type: str,
    run_id: UUID | None = None,
) -> ExecutionEvent:
    return ExecutionEvent(
        task_id=task_id,
        run_id=run_id,
        sequence=sequence,
        event_type=event_type,
        occurred_at=datetime.now(timezone.utc),
        registry_generation=1,
        actor=ActorRef(type=ActorType.SYSTEM, id="test"),
    )


def test_replay_reconstructs_approval_and_completion_path() -> None:
    task_id = uuid4()
    run_id = uuid4()
    events = [
        _event(task_id=task_id, sequence=1, event_type="task.created"),
        _event(task_id=task_id, sequence=2, event_type="task.planned"),
        _event(
            task_id=task_id,
            run_id=run_id,
            sequence=3,
            event_type="run.started",
        ),
        _event(
            task_id=task_id,
            run_id=run_id,
            sequence=4,
            event_type="approval.requested",
        ),
        _event(
            task_id=task_id,
            run_id=run_id,
            sequence=5,
            event_type="approval.decided",
        ),
        _event(
            task_id=task_id,
            run_id=run_id,
            sequence=6,
            event_type="run.resumed",
        ),
        _event(
            task_id=task_id,
            run_id=run_id,
            sequence=7,
            event_type="run.completed",
        ),
    ]

    state = replay_execution(events)

    assert state.task_id == task_id
    assert state.run_id == run_id
    assert state.task_status is TaskStatus.COMPLETED
    assert state.run_status is RunStatus.SUCCEEDED
    assert state.latest_sequence == 7


def test_replay_rejects_sequence_gaps_and_mixed_tasks() -> None:
    task_id = uuid4()
    with pytest.raises(ReplayError, match="sequence"):
        replay_execution(
            [
                _event(
                    task_id=task_id,
                    sequence=1,
                    event_type="task.created",
                ),
                _event(
                    task_id=task_id,
                    sequence=3,
                    event_type="task.planned",
                ),
            ],
        )

    with pytest.raises(ReplayError, match="task"):
        replay_execution(
            [
                _event(
                    task_id=task_id,
                    sequence=1,
                    event_type="task.created",
                ),
                _event(
                    task_id=uuid4(),
                    sequence=2,
                    event_type="task.planned",
                ),
            ],
        )


def test_replay_rejects_invalid_domain_transition() -> None:
    task_id = uuid4()
    run_id = uuid4()
    with pytest.raises(ReplayError, match="transition"):
        replay_execution(
            [
                _event(
                    task_id=task_id,
                    sequence=1,
                    event_type="task.created",
                ),
                _event(
                    task_id=task_id,
                    run_id=run_id,
                    sequence=2,
                    event_type="run.completed",
                ),
            ],
        )
