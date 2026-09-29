# -*- coding: utf-8 -*-
"""Tests for the Lite SQLite execution ledger."""

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from qwenpaw.kernel.events import ExecutionCommit, ExecutionEvent
from qwenpaw.kernel.models import (
    ActorRef,
    ActorType,
    ExecutionCheckpoint,
    Task,
    TaskSource,
    TaskStatus,
)
from qwenpaw.tasks.ledger import (
    LEDGER_SCHEMA_VERSION,
    EventCausalityError,
    EventConflictError,
    EventSequenceError,
    SQLiteExecutionLedger,
    TaskVersionConflictError,
)


def _event(
    *,
    task_id: UUID,
    sequence: int,
    event_type: str = "task.created",
    event_id: UUID | None = None,
    run_id: UUID | None = None,
    cause_event_id: UUID | None = None,
) -> ExecutionEvent:
    values = {
        "task_id": task_id,
        "run_id": run_id,
        "sequence": sequence,
        "event_type": event_type,
        "occurred_at": datetime.now(timezone.utc),
        "registry_generation": 1,
        "actor": ActorRef(type=ActorType.SYSTEM, id="test"),
        "cause_event_id": cause_event_id,
        "payload": {"authorization": "[REDACTED]"},
    }
    if event_id is not None:
        values["event_id"] = event_id
    return ExecutionEvent(**values)


@pytest.mark.asyncio
async def test_ledger_uses_wal_and_explicit_schema_version(
    tmp_path: Path,
) -> None:
    ledger = SQLiteExecutionLedger(tmp_path / "ledger.db")

    await ledger.initialize()

    assert await ledger.journal_mode() == "wal"
    assert await ledger.schema_version() == LEDGER_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_events_survive_a_new_ledger_instance(tmp_path: Path) -> None:
    database_path = tmp_path / "ledger.db"
    task_id = uuid4()
    first = SQLiteExecutionLedger(database_path)
    await first.initialize()
    assert await first.append(_event(task_id=task_id, sequence=1))
    assert await first.append(
        _event(
            task_id=task_id,
            sequence=2,
            event_type="task.planned",
        ),
    )

    reopened = SQLiteExecutionLedger(database_path)
    await reopened.initialize()
    events = await reopened.list_events(task_id)

    assert [event.sequence for event in events] == [1, 2]
    assert [event.event_type for event in events] == [
        "task.created",
        "task.planned",
    ]
    assert await reopened.latest_sequence(task_id) == 2


@pytest.mark.asyncio
async def test_duplicate_event_is_idempotent_but_conflict_is_rejected(
    tmp_path: Path,
) -> None:
    ledger = SQLiteExecutionLedger(tmp_path / "ledger.db")
    await ledger.initialize()
    task_id = uuid4()
    event = _event(task_id=task_id, sequence=1)

    assert await ledger.append(event)
    assert not await ledger.append(event)

    conflicting = event.model_copy(update={"event_type": "task.planned"})
    with pytest.raises(EventConflictError):
        await ledger.append(conflicting)


@pytest.mark.asyncio
async def test_ledger_rejects_sequence_gaps_and_collisions(
    tmp_path: Path,
) -> None:
    ledger = SQLiteExecutionLedger(tmp_path / "ledger.db")
    await ledger.initialize()
    task_id = uuid4()

    with pytest.raises(EventSequenceError, match="expected 1"):
        await ledger.append(_event(task_id=task_id, sequence=2))

    await ledger.append(_event(task_id=task_id, sequence=1))
    with pytest.raises(EventSequenceError, match="expected 2"):
        await ledger.append(
            _event(
                task_id=task_id,
                sequence=1,
                event_type="task.planned",
            ),
        )


@pytest.mark.asyncio
async def test_ledger_rejects_missing_cross_task_and_future_causes(
    tmp_path: Path,
) -> None:
    ledger = SQLiteExecutionLedger(tmp_path / "ledger.db")
    first_task_id = uuid4()
    second_task_id = uuid4()
    first = _event(task_id=first_task_id, sequence=1)
    other = _event(task_id=second_task_id, sequence=1)
    await ledger.append(first)
    await ledger.append(other)

    with pytest.raises(EventCausalityError):
        await ledger.append(
            _event(
                task_id=first_task_id,
                sequence=2,
                cause_event_id=uuid4(),
            ),
        )
    with pytest.raises(EventCausalityError):
        await ledger.append(
            _event(
                task_id=first_task_id,
                sequence=2,
                cause_event_id=other.event_id,
            ),
        )
    future = _event(task_id=first_task_id, sequence=3)
    effect = _event(
        task_id=first_task_id,
        sequence=2,
        cause_event_id=future.event_id,
    )
    with pytest.raises(EventCausalityError):
        await ledger.commit(ExecutionCommit(events=(effect, future)))


@pytest.mark.asyncio
async def test_ledger_accepts_prior_and_same_commit_causes(
    tmp_path: Path,
) -> None:
    ledger = SQLiteExecutionLedger(tmp_path / "ledger.db")
    task_id = uuid4()
    first = _event(task_id=task_id, sequence=1)
    await ledger.append(first)
    second = _event(
        task_id=task_id,
        sequence=2,
        cause_event_id=first.event_id,
    )
    third = _event(
        task_id=task_id,
        sequence=3,
        cause_event_id=second.event_id,
    )

    await ledger.commit(ExecutionCommit(events=(second, third)))

    assert await ledger.list_events(task_id) == [first, second, third]


@pytest.mark.asyncio
async def test_latest_safe_checkpoint_survives_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "ledger.db"
    task_id = uuid4()
    run_id = uuid4()
    ledger = SQLiteExecutionLedger(database_path)
    await ledger.initialize()
    await ledger.append(_event(task_id=task_id, sequence=1))
    await ledger.append(
        _event(
            task_id=task_id,
            run_id=run_id,
            sequence=2,
            event_type="run.started",
        ),
    )
    unsafe = ExecutionCheckpoint(
        task_id=task_id,
        run_id=run_id,
        sequence=1,
        safe_to_resume=False,
        runner_cursor={"step": 0},
    )
    safe = ExecutionCheckpoint(
        task_id=task_id,
        run_id=run_id,
        sequence=2,
        safe_to_resume=True,
        runner_cursor={"step": 1},
        workspace_checkpoint_ref="refs/qwenpaw/snap/test",
    )
    await ledger.save_checkpoint(unsafe)
    await ledger.save_checkpoint(safe)

    reopened = SQLiteExecutionLedger(database_path)
    await reopened.initialize()
    restored = await reopened.latest_resumable(task_id)

    assert restored == safe


@pytest.mark.asyncio
async def test_checkpoint_cannot_reference_uncommitted_sequence(
    tmp_path: Path,
) -> None:
    ledger = SQLiteExecutionLedger(tmp_path / "ledger.db")
    await ledger.initialize()
    task_id = uuid4()

    with pytest.raises(EventSequenceError, match="uncommitted"):
        await ledger.save_checkpoint(
            ExecutionCheckpoint(
                task_id=task_id,
                run_id=uuid4(),
                sequence=1,
                safe_to_resume=True,
            ),
        )


@pytest.mark.asyncio
async def test_execution_commit_rolls_back_event_on_projection_conflict(
    tmp_path: Path,
) -> None:
    ledger = SQLiteExecutionLedger(tmp_path / "ledger.db")
    await ledger.initialize()
    task = Task(
        objective="Create atomically",
        source=TaskSource.USER,
        agent_id="default",
    )
    created = _event(task_id=task.task_id, sequence=1)
    await ledger.commit(
        ExecutionCommit(
            events=(created,),
            task=task,
            create_task=True,
        ),
    )
    stale = task.model_copy(
        update={
            "status": TaskStatus.PLANNED,
            "version": 3,
        },
    )
    planned = _event(
        task_id=task.task_id,
        sequence=2,
        event_type="task.planned",
    )

    with pytest.raises(TaskVersionConflictError):
        await ledger.commit(
            ExecutionCommit(
                events=(planned,),
                task=stale,
                expected_task_version=1,
            ),
        )

    assert await ledger.latest_sequence(task.task_id) == 1
    assert await ledger.get_task(task.task_id) == task


@pytest.mark.asyncio
async def test_execution_commit_is_idempotent_as_one_unit(
    tmp_path: Path,
) -> None:
    ledger = SQLiteExecutionLedger(tmp_path / "ledger.db")
    task = Task(
        objective="Retry atomically",
        source=TaskSource.USER,
        agent_id="default",
    )
    commit = ExecutionCommit(
        events=(_event(task_id=task.task_id, sequence=1),),
        task=task,
        create_task=True,
    )

    assert await ledger.commit(commit)
    assert not await ledger.commit(commit)
    assert await ledger.list_tasks(cursor=None, limit=10) == [task]


@pytest.mark.asyncio
async def test_workbench_projection_reads_one_bounded_snapshot(
    tmp_path: Path,
) -> None:
    ledger = SQLiteExecutionLedger(tmp_path / "ledger.db")
    task = Task(
        objective="Read one snapshot",
        source=TaskSource.USER,
        agent_id="default",
    )
    event = _event(task_id=task.task_id, sequence=1)
    await ledger.commit(
        ExecutionCommit(
            events=(event,),
            task=task,
            create_task=True,
        ),
    )

    snapshot = await ledger.read_projection(task.task_id)

    assert snapshot.task == task
    assert snapshot.events == (event,)
    assert snapshot.last_sequence == 1
    assert snapshot.runs == ()
    assert snapshot.latest_plan is None
    assert snapshot.approvals == ()
    assert snapshot.checkpoint is None
