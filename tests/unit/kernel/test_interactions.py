# -*- coding: utf-8 -*-
"""Tests for channel-neutral runtime interaction contracts."""

from uuid import uuid4

from pydantic import ValidationError
import pytest

from qwenpaw.kernel import (
    ActorRef,
    ActorType,
    InteractionKind,
    InteractionMode,
    InteractionOption,
    InteractionRequest,
    InteractionRecord,
    InteractionResolution,
    InteractionResponse,
    InteractionStatus,
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


def test_suggestion_cannot_pause_runtime() -> None:
    with pytest.raises(ValidationError, match="must be non-blocking"):
        _request(
            kind=InteractionKind.SUGGESTION,
            mode=InteractionMode.BLOCKING,
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
