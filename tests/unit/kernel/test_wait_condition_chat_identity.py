# -*- coding: utf-8 -*-
"""WaitCondition uses ChatSpec.id across legacy projections."""

from datetime import datetime, timezone
from uuid import uuid4

from qwenpaw.kernel import (
    ContinuationAvailability,
    ContinuationMode,
    ContinuationRef,
    WaitCondition,
    WaitConditionKind,
    WaitConditionStatus,
)


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
