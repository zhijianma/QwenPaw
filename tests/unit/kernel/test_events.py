# -*- coding: utf-8 -*-
"""Contract tests for the execution event envelope."""

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from qwenpaw.kernel.events import (
    MAX_INLINE_EVENT_PAYLOAD_BYTES,
    ExecutionEvent,
)
from qwenpaw.kernel.models import ActorRef, ActorType, ArtifactRef


def _event(**overrides) -> ExecutionEvent:
    values = {
        "task_id": uuid4(),
        "sequence": 1,
        "event_type": "task.created",
        "occurred_at": datetime.now(timezone.utc),
        "registry_generation": 1,
        "actor": ActorRef(type=ActorType.SYSTEM, id="task-service"),
        "payload": {"objective_summary": "Prepare a report"},
    }
    values.update(overrides)
    return ExecutionEvent(**values)


def test_event_round_trip_preserves_schema_and_types() -> None:
    artifact = ArtifactRef(
        kind="report",
        uri="qwenpaw://artifacts/report.md",
        media_type="text/markdown",
        content_hash=f"sha256:{'b' * 64}",
        size_bytes=7,
    )
    event = _event(artifact_refs=(artifact,))

    restored = ExecutionEvent.model_validate_json(event.model_dump_json())

    assert restored == event
    payload = json.loads(event.model_dump_json())
    assert restored.schema_id == "qwenpaw.execution-event.v1"
    assert payload["schema"] == "qwenpaw.execution-event.v1"
    assert "schema_id" not in payload


def test_event_round_trip_preserves_causal_identity() -> None:
    invocation_id = uuid4()
    step_id = uuid4()
    cause_event_id = uuid4()
    correlation_id = uuid4()
    event = _event(
        invocation_id=invocation_id,
        step_id=step_id,
        source="runner.local",
        cause_event_id=cause_event_id,
        correlation_id=correlation_id,
    )

    restored = ExecutionEvent.model_validate_json(event.model_dump_json())

    assert restored.invocation_id == invocation_id
    assert restored.step_id == step_id
    assert restored.source == "runner.local"
    assert restored.cause_event_id == cause_event_id
    assert restored.correlation_id == correlation_id


def test_legacy_event_payload_defaults_causal_identity() -> None:
    payload = _event().model_dump(mode="json")
    for field in (
        "invocation_id",
        "step_id",
        "source",
        "cause_event_id",
        "correlation_id",
    ):
        payload.pop(field)

    restored = ExecutionEvent.model_validate(payload)

    assert restored.invocation_id is None
    assert restored.step_id is None
    assert restored.source is None
    assert restored.cause_event_id is None
    assert restored.correlation_id is None


@pytest.mark.parametrize(
    "event_type",
    ["TaskCreated", "task", "task..created", "task created"],
)
def test_event_type_requires_namespaced_lowercase_value(
    event_type: str,
) -> None:
    with pytest.raises(ValidationError):
        _event(event_type=event_type)


@pytest.mark.parametrize(
    "payload",
    [
        {"chain_of_thought": "secret"},
        {"nested": {"hidden_reasoning": "secret"}},
        {"items": [{"reasoning_content": "secret"}]},
        {"raw_prompt": "system prompt"},
    ],
)
def test_event_rejects_hidden_reasoning_and_raw_prompt_fields(
    payload: dict,
) -> None:
    with pytest.raises(ValidationError, match="forbidden field"):
        _event(payload=payload)


def test_event_rejects_oversized_inline_payload() -> None:
    oversized = "x" * MAX_INLINE_EVENT_PAYLOAD_BYTES

    with pytest.raises(ValidationError, match="ArtifactRef"):
        _event(payload={"output": oversized})


def test_event_requires_positive_sequence_and_generation() -> None:
    with pytest.raises(ValidationError):
        _event(sequence=0)
    with pytest.raises(ValidationError):
        _event(registry_generation=0)
