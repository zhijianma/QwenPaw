# -*- coding: utf-8 -*-
"""Tests for server-authoritative invocation control contracts."""

from uuid import uuid4

from pydantic import ValidationError
import pytest

from qwenpaw.kernel import (
    ControlCommand,
    ControlCommandKind,
    InvalidSubmissionTransition,
    QueueProjection,
    SteerSafePoint,
    SubmissionStatus,
    TurnSubmission,
    TurnSubmissionRequest,
    validate_submission_transition,
)


def _submission(
    *,
    sequence: int,
    status: SubmissionStatus = SubmissionStatus.QUEUED,
    invocation_id=None,
) -> TurnSubmission:
    return TurnSubmission(
        agent_id="default",
        conversation_id="chat-1",
        sequence=sequence,
        queue_position=sequence,
        content=f"message {sequence}",
        idempotency_key=f"message-{sequence}",
        status=status,
        invocation_id=invocation_id,
    )


def test_queue_projection_allows_one_active_invocation() -> None:
    invocation_id = uuid4()
    active = _submission(
        sequence=1,
        status=SubmissionStatus.RUNNING,
        invocation_id=invocation_id,
    )
    queued = _submission(sequence=2)

    projection = QueueProjection(
        agent_id="default",
        conversation_id="chat-1",
        revision=2,
        active_submission_id=active.submission_id,
        submissions=(active, queued),
    )

    assert projection.active_submission_id == active.submission_id


def test_queue_projection_rejects_two_active_invocations() -> None:
    first = _submission(
        sequence=1,
        status=SubmissionStatus.RUNNING,
        invocation_id=uuid4(),
    )
    second = _submission(
        sequence=2,
        status=SubmissionStatus.ADMITTED,
        invocation_id=uuid4(),
    )

    with pytest.raises(
        ValidationError,
        match="one active invocation",
    ):
        QueueProjection(
            agent_id="default",
            conversation_id="chat-1",
            revision=2,
            active_submission_id=first.submission_id,
            submissions=(first, second),
        )


def test_queue_projection_rejects_unordered_position() -> None:
    with pytest.raises(ValidationError, match="position ordered"):
        later = _submission(sequence=1)
        earlier = _submission(sequence=2)
        later = later.model_copy(update={"queue_position": 2})
        earlier = earlier.model_copy(update={"queue_position": 1})
        QueueProjection(
            agent_id="default",
            conversation_id="chat-1",
            revision=2,
            submissions=(later, earlier),
        )


def test_steer_requires_active_invocation_and_instruction() -> None:
    with pytest.raises(ValidationError, match="steer requires"):
        ControlCommand(
            kind=ControlCommandKind.STEER,
            agent_id="default",
            conversation_id="chat-1",
            idempotency_key="steer-1",
            expected_revision=3,
        )


def test_cancel_queued_is_distinct_from_interrupt_current() -> None:
    queued = _submission(sequence=2)
    cancel = ControlCommand(
        kind=ControlCommandKind.CANCEL_QUEUED,
        agent_id="default",
        conversation_id="chat-1",
        idempotency_key="cancel-1",
        expected_revision=3,
        target_submission_id=queued.submission_id,
    )
    interrupt = ControlCommand(
        kind=ControlCommandKind.INTERRUPT_CURRENT,
        agent_id="default",
        conversation_id="chat-1",
        idempotency_key="interrupt-1",
        expected_revision=3,
        target_invocation_id=uuid4(),
    )

    assert cancel.target_invocation_id is None
    assert interrupt.target_submission_id is None


def test_admitted_submission_requires_invocation_identity() -> None:
    with pytest.raises(ValidationError, match="require invocation_id"):
        _submission(sequence=1, status=SubmissionStatus.ADMITTED)


def test_submission_request_has_no_client_sequence_or_status() -> None:
    request = TurnSubmissionRequest(
        agent_id="default",
        conversation_id="chat-1",
        content="hello",
        idempotency_key="message-1",
    )

    payload = request.model_dump(mode="json")
    assert "sequence" not in payload
    assert "status" not in payload


def test_submission_transition_requires_interrupting_state() -> None:
    validate_submission_transition(
        SubmissionStatus.ADMITTED,
        SubmissionStatus.INTERRUPTED,
    )
    validate_submission_transition(
        SubmissionStatus.RUNNING,
        SubmissionStatus.INTERRUPTING,
    )
    validate_submission_transition(
        SubmissionStatus.INTERRUPTING,
        SubmissionStatus.INTERRUPTED,
    )

    with pytest.raises(InvalidSubmissionTransition):
        validate_submission_transition(
            SubmissionStatus.RUNNING,
            SubmissionStatus.INTERRUPTED,
        )


def test_control_command_rejects_cross_kind_payload() -> None:
    with pytest.raises(ValidationError, match="ambiguous"):
        ControlCommand(
            kind=ControlCommandKind.INTERRUPT_CURRENT,
            agent_id="default",
            conversation_id="chat-1",
            idempotency_key="interrupt-2",
            expected_revision=3,
            target_invocation_id=uuid4(),
            instruction="this belongs to steer",
        )


def test_stop_and_clear_accepts_only_captured_invocation_target() -> None:
    invocation_id = uuid4()
    command = ControlCommand(
        kind=ControlCommandKind.STOP_AND_CLEAR,
        agent_id="default",
        conversation_id="chat-1",
        idempotency_key="stop-all-1",
        expected_revision=3,
        target_invocation_id=invocation_id,
    )

    assert command.target_invocation_id == invocation_id

    with pytest.raises(ValidationError, match="captured invocation"):
        ControlCommand(
            kind=ControlCommandKind.STOP_AND_CLEAR,
            agent_id="default",
            conversation_id="chat-1",
            idempotency_key="stop-all-2",
            expected_revision=3,
            target_submission_id=uuid4(),
        )


def test_only_applied_steer_receipt_can_record_safe_point() -> None:
    from qwenpaw.kernel import ControlCommandStatus, ControlReceipt

    command_id = uuid4()
    receipt = ControlReceipt(
        command_id=command_id,
        kind=ControlCommandKind.STEER,
        status=ControlCommandStatus.APPLIED,
        agent_id="default",
        conversation_id="chat-1",
        revision=4,
        applied_at_safe_point=SteerSafePoint.BEFORE_REASONING,
    )

    assert receipt.applied_at_safe_point is SteerSafePoint.BEFORE_REASONING

    with pytest.raises(ValidationError, match="only an applied steer"):
        ControlReceipt(
            command_id=uuid4(),
            kind=ControlCommandKind.INTERRUPT_CURRENT,
            status=ControlCommandStatus.APPLIED,
            agent_id="default",
            conversation_id="chat-1",
            revision=5,
            applied_at_safe_point=SteerSafePoint.AFTER_TOOL_BATCH,
        )
