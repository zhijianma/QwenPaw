# -*- coding: utf-8 -*-
"""Tests for the transport-neutral Task Workbench read projections."""

from datetime import timedelta
from uuid import uuid4

from qwenpaw.kernel.events import ExecutionEvent
from qwenpaw.kernel.models import (
    ActorRef,
    ActorType,
    ApprovalDecision,
    ApprovalDecisionValue,
    ApprovalRequest,
    ApprovalStatus,
    RiskLevel,
    utc_now,
)
from qwenpaw.tasks.workbench import (
    project_approval_items,
    project_conversation_messages,
    project_tool_activities,
)


def _event(
    event_type: str,
    sequence: int,
    *,
    run_id=None,
    payload: dict | None = None,
) -> ExecutionEvent:
    return ExecutionEvent(
        task_id=uuid4(),
        run_id=run_id,
        sequence=sequence,
        event_type=event_type,
        occurred_at=utc_now() + timedelta(seconds=sequence),
        registry_generation=1,
        actor=ActorRef(type=ActorType.RUNNER, id="runner.tests"),
        payload=payload or {},
    )


def _approval(*, decided: bool = False):
    approval_id = uuid4()
    request = ApprovalRequest(
        approval_id=approval_id,
        task_id=uuid4(),
        action="tools.shell",
        risk=RiskLevel.HIGH,
        requester=ActorRef(type=ActorType.RUNNER, id="runner.tests"),
        policy="policy.strict",
    )
    decision = None
    if decided:
        decision = ApprovalDecision(
            approval_id=approval_id,
            decision=ApprovalDecisionValue.APPROVED,
            actor=ActorRef(type=ActorType.USER, id="user.tests"),
            reason="Approved in the Workbench",
        )
    return request, decision


def test_conversation_projection_merges_same_run_assistant_deltas() -> None:
    run_id = uuid4()
    events = (
        _event(
            "conversation.user",
            1,
            run_id=run_id,
            payload={"role": "user", "text": "Inspect"},
        ),
        _event(
            "conversation.assistant.delta",
            2,
            run_id=run_id,
            payload={"role": "assistant", "text": "Work"},
        ),
        _event(
            "conversation.assistant.delta",
            3,
            run_id=run_id,
            payload={"role": "assistant", "text": "bench"},
        ),
    )

    messages = project_conversation_messages(events)

    assert [message.text for message in messages] == [
        "Inspect",
        "Workbench",
    ]
    assert messages[1].created_at == events[1].occurred_at
    assert messages[1].completed_at == events[2].occurred_at


def test_conversation_projection_keeps_runs_separate_and_ignores_noise(
) -> None:
    first_run = uuid4()
    second_run = uuid4()
    events = (
        _event(
            "conversation.assistant.delta",
            1,
            run_id=first_run,
            payload={"role": "assistant", "text": "First"},
        ),
        _event(
            "tool.started",
            2,
            run_id=first_run,
            payload={"name": "shell"},
        ),
        _event(
            "conversation.assistant.delta",
            3,
            run_id=second_run,
            payload={"role": "assistant", "text": "Second"},
        ),
        _event(
            "conversation.user",
            4,
            run_id=second_run,
            payload={"role": "system", "text": "Hidden"},
        ),
        _event(
            "conversation.user",
            5,
            run_id=second_run,
            payload={"role": "user", "text": 42},
        ),
    )

    messages = project_conversation_messages(events)

    assert [message.text for message in messages] == ["First", "Second"]
    assert [message.run_id for message in messages] == [first_run, second_run]
    assert project_tool_activities(events) == (events[1],)


def test_approval_projection_splits_pending_and_bounds_decisions() -> None:
    pending_record = _approval()
    decided_records = tuple(_approval(decided=True) for _ in range(22))

    pending, recent = project_approval_items(
        (pending_record, *decided_records),
    )

    assert len(pending) == 1
    assert pending[0].status is ApprovalStatus.PENDING
    assert pending[0].to_public_dict()["decision"] is None
    assert len(recent) == 20
    assert recent[0].request == decided_records[2][0]
    assert recent[-1].status is ApprovalStatus.APPROVED
    serialized = recent[-1].to_public_dict()
    assert serialized["status"] == "approved"
    assert serialized["decision"]["approval_id"] == str(
        recent[-1].request.approval_id,
    )
