# -*- coding: utf-8 -*-
"""Tests for Chat-owned reads over Task verification events."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from qwenpaw.kernel import (
    AcceptanceVerification,
    ActorRef,
    ActorType,
    Task,
    TaskSource,
    VerificationResult,
    VerificationStatus,
)
from qwenpaw.kernel.events import ExecutionEvent
from qwenpaw.tasks.verification_history import TaskVerificationHistory


class _TaskLedger:
    def __init__(self, tasks, events) -> None:
        self._tasks = sorted(tasks, key=lambda task: str(task.task_id))
        self._events = events

    async def list_tasks(self, *, cursor, limit):
        tasks = [
            task
            for task in self._tasks
            if cursor is None or str(task.task_id) > cursor
        ]
        return tasks[:limit]

    async def list_events(self, task_id, *, after_sequence=0, limit=200):
        return [
            event
            for event in self._events.get(task_id, ())
            if event.sequence > after_sequence
        ][:limit]


def _verification_event(task: Task, occurred_at: datetime) -> ExecutionEvent:
    run_id = uuid4()
    result = VerificationResult(
        task_id=task.task_id,
        run_id=run_id,
        verifier_id="qwenpaw.verifier.tests",
        status=VerificationStatus.PASSED,
        acceptance=(
            AcceptanceVerification(
                criterion="PRIVATE ACCEPTANCE TEXT",
                passed=True,
                reason="PRIVATE VERIFIER REASON",
            ),
        ),
        created_at=occurred_at - timedelta(seconds=2),
    )
    return ExecutionEvent(
        task_id=task.task_id,
        run_id=run_id,
        sequence=1,
        event_type="verification.completed",
        occurred_at=occurred_at,
        invocation_id=uuid4(),
        correlation_id=uuid4(),
        registry_generation=7,
        actor=ActorRef(type=ActorType.SYSTEM, id="verifier"),
        source="qwenpaw.verifier.tests",
        payload={"result": result.model_dump(mode="json")},
    )


@pytest.mark.asyncio
async def test_history_filters_conversation_and_preserves_host_causality():
    now = datetime.now(timezone.utc)
    direct = Task(
        objective="Direct",
        source=TaskSource.USER,
        agent_id="default",
        metadata={"conversation_id": "chat-target"},
    )
    compatible = Task(
        objective="Compatible",
        source=TaskSource.API,
        agent_id="default",
        metadata={"chat_id": "chat-target"},
    )
    unrelated = Task(
        objective="Unrelated",
        source=TaskSource.USER,
        agent_id="default",
        metadata={"conversation_id": ["chat-target"]},
    )
    direct_event = _verification_event(direct, now)
    compatible_event = _verification_event(
        compatible,
        now + timedelta(seconds=1),
    )
    unrelated_event = _verification_event(
        unrelated,
        now + timedelta(seconds=2),
    )
    ledger = _TaskLedger(
        (direct, compatible, unrelated),
        {
            direct.task_id: (direct_event,),
            compatible.task_id: (compatible_event,),
            unrelated.task_id: (unrelated_event,),
        },
    )

    records = await TaskVerificationHistory(
        ledger,
        ledger,
    ).list_for_conversation("chat-target")

    assert [record.task_id for record in records] == [
        compatible.task_id,
        direct.task_id,
    ]
    assert records[0].event_id == compatible_event.event_id
    assert records[0].invocation_id == compatible_event.invocation_id
    assert records[0].registry_generation == 7
    assert records[0].occurred_at == compatible_event.occurred_at


@pytest.mark.asyncio
async def test_history_rejects_invalid_query_bounds():
    history = TaskVerificationHistory(_TaskLedger((), {}), _TaskLedger((), {}))

    with pytest.raises(ValueError, match="cannot be empty"):
        await history.list_for_conversation(" ")
    with pytest.raises(ValueError, match="between 1 and 1000"):
        await history.list_for_conversation("chat", limit=1001)
