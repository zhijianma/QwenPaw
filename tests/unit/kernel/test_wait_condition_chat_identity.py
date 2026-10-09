# -*- coding: utf-8 -*-
"""WaitCondition uses ChatSpec.id across legacy projections."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from qwenpaw.kernel import (
    ContinuationAvailability,
    ContinuationMode,
    ContinuationRef,
    ConversationContinuation,
    HarnessRecoveryContextCheckpoint,
    ModelFailureClass,
    ModelResourceWait,
    ModelStepContextCheckpoint,
    ModelStepContinuation,
    ModelStepRetryAuthorization,
    WaitCondition,
    WaitConditionKind,
    WaitConditionStatus,
)


@pytest.mark.parametrize(
    "model",
    (
        ConversationContinuation,
        HarnessRecoveryContextCheckpoint,
        ModelResourceWait,
        ModelStepContextCheckpoint,
        ModelStepContinuation,
        ModelStepRetryAuthorization,
        WaitCondition,
    ),
)
def test_durable_wait_contracts_publish_only_chat_id(model) -> None:
    properties = model.model_json_schema()["properties"]

    assert "chat_id" in properties
    assert "conversation_id" not in properties


def test_wait_condition_serializes_canonical_chat_identity() -> None:
    invocation_id = uuid4()
    wait = WaitCondition(
        condition_id=uuid4(),
        kind=WaitConditionKind.USER_INPUT,
        status=WaitConditionStatus.WAITING,
        agent_id="default",
        conversation_id="chat-legacy",
        source_type="interaction",
        source_id=uuid4(),
        continuation=ContinuationRef(
            mode=ContinuationMode.CONVERSATION_TURN,
            availability=ContinuationAvailability.DETACHED,
            invocation_id=invocation_id,
        ),
        revision=1,
        created_at=datetime.now(timezone.utc),
    )

    payload = wait.model_dump(mode="json")
    assert payload["chat_id"] == "chat-legacy"
    assert "conversation_id" not in payload

    legacy_payload = dict(payload)
    legacy_payload["conversation_id"] = legacy_payload.pop("chat_id")
    restored = WaitCondition.model_validate(legacy_payload)
    assert restored.chat_id == "chat-legacy"
    assert restored.conversation_id == "chat-legacy"

    with pytest.raises(ValidationError, match="must identify one Chat"):
        WaitCondition.model_validate(
            {
                **payload,
                "conversation_id": "chat-other",
            },
        )


def test_model_resource_wait_factory_accepts_only_one_chat_identity() -> None:
    values = {
        "wait_id": uuid4(),
        "attempt_id": uuid4(),
        "invocation_id": uuid4(),
        "correlation_id": uuid4(),
        "agent_id": "default",
        "failure_class": ModelFailureClass.RATE_LIMITED,
    }

    canonical = ModelResourceWait.for_model_failure(
        **values,
        chat_id="chat-a",
    )
    legacy = ModelResourceWait.for_model_failure(
        **values,
        conversation_id="chat-a",
    )

    assert canonical.chat_id == legacy.chat_id == "chat-a"
    with pytest.raises(ValueError, match="must identify one Chat"):
        ModelResourceWait.for_model_failure(
            **values,
            chat_id="chat-a",
            conversation_id="chat-b",
        )
