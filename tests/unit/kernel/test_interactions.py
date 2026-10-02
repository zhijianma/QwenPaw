# -*- coding: utf-8 -*-
"""Tests for channel-neutral runtime interaction contracts."""

from uuid import uuid4

from pydantic import ValidationError
import pytest

from qwenpaw.kernel import (
    ActorRef,
    ActorType,
    ContinuationAvailability,
    ContinuationMode,
    ContinuationRef,
    InteractionKind,
    InteractionMode,
    InteractionPolicyError,
    InteractionOption,
    InteractionRequest,
    InteractionRecord,
    InteractionResolution,
    InteractionResponse,
    InteractionStatus,
    UserInputReason,
    validate_interaction_admission,
)


def _actor() -> ActorRef:
    return ActorRef(
        type=ActorType.USER,
        id="console:user-1",
    )


def _request(
    *,
    kind: InteractionKind,
    mode: InteractionMode,
) -> InteractionRequest:
    return InteractionRequest(
        kind=kind,
        mode=mode,
        agent_id="default",
        conversation_id="chat-1",
        invocation_id=uuid4(),
        title="Runtime needs input",
        prompt="Choose how to continue.",
    )


def test_approval_and_user_input_share_transport_contract() -> None:
    approval = InteractionRequest(
        kind=InteractionKind.APPROVAL,
        mode=InteractionMode.BLOCKING,
        agent_id="default",
        conversation_id="chat-1",
        invocation_id=uuid4(),
        source_id=uuid4(),
        title="Approve tool call",
        prompt="Allow this file write?",
        options=(
            InteractionOption(option_id="approve", label="Approve"),
            InteractionOption(option_id="deny", label="Deny"),
        ),
    )
    user_input = _request(
        kind=InteractionKind.USER_INPUT,
        mode=InteractionMode.BLOCKING,
    )

    assert approval.source_id is not None
    assert user_input.source_id is None
    assert approval.mode is user_input.mode


def test_new_user_input_requires_structured_blocking_reason() -> None:
    legacy = _request(
        kind=InteractionKind.USER_INPUT,
        mode=InteractionMode.BLOCKING,
    )

    with pytest.raises(
        InteractionPolicyError,
        match="requires an allowed input reason",
    ):
        validate_interaction_admission(legacy)

    admitted = legacy.model_copy(
        update={
            "user_input_reason": UserInputReason.MISSING_REQUIRED_FACT,
        },
    )
    assert validate_interaction_admission(admitted) is admitted


def test_only_user_input_may_declare_blocking_reason() -> None:
    with pytest.raises(
        ValidationError,
        match="only user input interactions",
    ):
        InteractionRequest(
            kind=InteractionKind.SUGGESTION,
            mode=InteractionMode.NON_BLOCKING,
            agent_id="default",
            conversation_id="chat-1",
            invocation_id=uuid4(),
            user_input_reason=UserInputReason.MATERIAL_PREFERENCE,
            title="Optional action",
            prompt="Consider a regression test.",
        )


def test_suggestion_cannot_pause_runtime() -> None:
    with pytest.raises(ValidationError, match="must be non-blocking"):
        _request(
            kind=InteractionKind.SUGGESTION,
            mode=InteractionMode.BLOCKING,
        )


def test_suggestion_cannot_reference_checkpoint_continuation() -> None:
    with pytest.raises(
        ValidationError,
        match="checkpoint continuation requires a blocking interaction",
    ):
        InteractionRequest(
            kind=InteractionKind.SUGGESTION,
            mode=InteractionMode.NON_BLOCKING,
            agent_id="default",
            conversation_id="chat-1",
            invocation_id=uuid4(),
            continuation_checkpoint_id=uuid4(),
            title="Optional action",
            prompt="You may continue later.",
        )


def test_approval_cannot_be_downgraded_to_notification() -> None:
    with pytest.raises(ValidationError, match="must be blocking"):
        _request(
            kind=InteractionKind.APPROVAL,
            mode=InteractionMode.NON_BLOCKING,
        )


def test_response_requires_content_and_revision() -> None:
    interaction_id = uuid4()
    with pytest.raises(ValidationError, match="cannot be empty"):
        InteractionResponse(
            interaction_id=interaction_id,
            idempotency_key="response-1",
            expected_revision=1,
            actor=_actor(),
        )

    response = InteractionResponse(
        interaction_id=interaction_id,
        idempotency_key="response-1",
        expected_revision=1,
        actor=_actor(),
        text="Use the safer option.",
    )
    resolution = InteractionResolution(
        interaction_id=interaction_id,
        status=InteractionStatus.RESOLVED,
        revision=2,
        response=response,
    )

    assert resolution.response == response


def test_cancelled_interaction_cannot_carry_user_response() -> None:
    interaction_id = uuid4()
    response = InteractionResponse(
        interaction_id=interaction_id,
        idempotency_key="response-2",
        expected_revision=1,
        actor=_actor(),
        selected_option_ids=("deny",),
    )

    with pytest.raises(ValidationError, match="cannot carry a response"):
        InteractionResolution(
            interaction_id=interaction_id,
            status=InteractionStatus.CANCELLED,
            revision=2,
            response=response,
        )


def test_interaction_record_rejects_foreign_resolution() -> None:
    request = _request(
        kind=InteractionKind.USER_INPUT,
        mode=InteractionMode.BLOCKING,
    )
    resolution = InteractionResolution(
        interaction_id=uuid4(),
        status=InteractionStatus.CANCELLED,
        revision=2,
    )

    with pytest.raises(ValidationError, match="identity mismatch"):
        InteractionRecord(request=request, resolution=resolution)


def test_checkpoint_continuation_requires_checkpoint_identity() -> None:
    with pytest.raises(
        ValidationError,
        match="checkpoint continuation requires checkpoint_id",
    ):
        ContinuationRef(
            mode=ContinuationMode.CHECKPOINT,
            availability=ContinuationAvailability.DETACHED,
            invocation_id=uuid4(),
        )


def test_conversation_continuation_rejects_non_blocking_request() -> None:
    with pytest.raises(
        ValidationError,
        match="user input interactions must be blocking",
    ):
        InteractionRequest(
            kind=InteractionKind.USER_INPUT,
            mode=InteractionMode.NON_BLOCKING,
            agent_id="default",
            conversation_id="chat-1",
            invocation_id=uuid4(),
            continuation_mode=ContinuationMode.CONVERSATION_TURN,
            title="Need input",
            prompt="Choose a path.",
        )
