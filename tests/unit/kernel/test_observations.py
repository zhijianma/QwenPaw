# -*- coding: utf-8 -*-
"""Contract tests for semantic runtime observations."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from qwenpaw.kernel import (
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
        "conversation_id": "chat-1",
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


def test_observation_rejects_hidden_reasoning() -> None:
    with pytest.raises(ValueError, match="forbidden field"):
        _observation(facts={"hidden_reasoning": "private"})


def test_observation_requires_task_or_conversation_ownership() -> None:
    with pytest.raises(ValueError, match="requires a task or conversation"):
        _observation(conversation_id=None)


def test_run_observation_requires_task_ownership() -> None:
    with pytest.raises(ValueError, match="requires a task"):
        _observation(conversation_id="chat-1", run_id=uuid4())
