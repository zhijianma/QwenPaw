# -*- coding: utf-8 -*-
"""Contract tests for semantic runtime observations."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from qwenpaw.kernel import (
    ConversationTrajectoryPage,
    ObservationCategory,
    ObservationSource,
    ObservationStage,
    ObservationStatus,
    RuntimeObservation,
)


def _observation(**updates) -> RuntimeObservation:
    values = {
        "observation_id": uuid4(),
        "category": ObservationCategory.MODEL,
        "stage": ObservationStage.EVIDENCE,
        "status": ObservationStatus.SUCCEEDED,
        "source": ObservationSource(
            source_type="qwenpaw.model.result",
            source_id=str(uuid4()),
        ),
        "chat_id": "chat-1",
        "invocation_id": uuid4(),
        "correlation_id": uuid4(),
        "registry_generation": 3,
        "title": "Model request completed",
        "facts": {"input_tokens": 7},
        "occurred_at": datetime.now(timezone.utc),
    }
    values.update(updates)
    return RuntimeObservation(**values)


def test_observation_contract_is_versioned_and_content_safe() -> None:
    observation = _observation()

    assert observation.schema_id == "qwenpaw.runtime-observation.v1"
    assert observation.facts == {"input_tokens": 7}
    payload = observation.model_dump(mode="json")
    assert payload["chat_id"] == "chat-1"
    assert "conversation_id" not in payload
    properties = RuntimeObservation.model_json_schema()["properties"]
    assert "chat_id" in properties
    assert "conversation_id" not in properties


def test_observation_rejects_hidden_reasoning() -> None:
    with pytest.raises(ValueError, match="forbidden field"):
        _observation(facts={"hidden_reasoning": "private"})


def test_observation_requires_task_or_conversation_ownership() -> None:
    with pytest.raises(ValueError, match="requires a task or conversation"):
        _observation(chat_id=None)


def test_run_observation_requires_task_ownership() -> None:
    with pytest.raises(ValueError, match="requires a task"):
        _observation(chat_id="chat-1", run_id=uuid4())


def test_observation_restores_legacy_chat_identity() -> None:
    payload = _observation().model_dump(mode="json")
    payload["conversation_id"] = payload.pop("chat_id")

    restored = RuntimeObservation.model_validate(payload)

    assert restored.chat_id == "chat-1"
    assert restored.conversation_id == "chat-1"


def test_observation_rejects_conflicting_chat_aliases() -> None:
    with pytest.raises(ValidationError, match="must identify one Chat"):
        RuntimeObservation.model_validate(
            {
                **_observation().model_dump(mode="json"),
                "conversation_id": "chat-2",
            },
        )


def test_trajectory_page_restores_legacy_chat_identity() -> None:
    correlation_id = uuid4()

    restored = ConversationTrajectoryPage.model_validate(
        {
            "conversation_id": "chat-1",
            "correlation_id": str(correlation_id),
        },
    )

    assert restored.chat_id == "chat-1"
    assert restored.conversation_id == "chat-1"
    payload = restored.model_dump(mode="json")
    assert payload["chat_id"] == "chat-1"
    assert "conversation_id" not in payload


def test_trajectory_page_rejects_conflicting_chat_aliases() -> None:
    with pytest.raises(ValidationError, match="must identify one Chat"):
        ConversationTrajectoryPage.model_validate(
            {
                "chat_id": "chat-1",
                "conversation_id": "chat-2",
                "correlation_id": str(uuid4()),
            },
        )
