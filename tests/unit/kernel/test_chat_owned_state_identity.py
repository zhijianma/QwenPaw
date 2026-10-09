# -*- coding: utf-8 -*-
"""Chat identity contracts for provider state and compaction facts."""

from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from qwenpaw.kernel import (
    AgentModeState,
    CompactionRecord,
    CompactionStatus,
    CompactionTrigger,
)
from qwenpaw.kernel.models import utc_now


def _mode_state(**identity: str) -> AgentModeState:
    return AgentModeState(
        provider_id="example.mode",
        agent_id="default",
        value={"step": 1},
        writer_registry_epoch_id=uuid4(),
        writer_generation=1,
        **identity,
    )


def _compaction(**identity: str) -> CompactionRecord:
    now = utc_now()
    return CompactionRecord(
        agent_id="default",
        invocation_id=uuid4(),
        registry_generation=1,
        strategy_id="qwenpaw.context.scroll",
        trigger=CompactionTrigger.MANUAL,
        status=CompactionStatus.SUCCEEDED,
        before_message_count=2,
        after_message_count=1,
        evicted_messages=1,
        context_changed=True,
        started_at=now,
        completed_at=now + timedelta(milliseconds=1),
        **identity,
    )


@pytest.mark.parametrize("factory", (_mode_state, _compaction))
def test_chat_owned_state_publishes_only_chat_id(factory) -> None:
    canonical = factory(chat_id="chat-a")
    legacy = factory(conversation_id="chat-a")

    assert canonical.chat_id == legacy.chat_id == "chat-a"
    assert canonical.conversation_id == "chat-a"
    assert "chat_id" in canonical.model_json_schema()["properties"]
    assert "conversation_id" not in canonical.model_json_schema()[
        "properties"
    ]
    payload = canonical.model_dump(mode="json")
    assert payload["chat_id"] == "chat-a"
    assert "conversation_id" not in payload


@pytest.mark.parametrize("factory", (_mode_state, _compaction))
def test_chat_owned_state_rejects_conflicting_identity(factory) -> None:
    with pytest.raises(ValidationError, match="must identify one Chat"):
        factory(chat_id="chat-a", conversation_id="chat-b")
