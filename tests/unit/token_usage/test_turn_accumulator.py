# -*- coding: utf-8 -*-
"""Tests for the live Chat-turn usage accumulator."""

import pytest
from pydantic import ValidationError

from qwenpaw.token_usage.turn_accumulator import TurnUsageAccumulator


def _usage(
    provider: str,
    model: str,
    prompt: int,
    completion: int,
) -> dict:
    return {
        "provider_id": provider,
        "model_name": model,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cache_eligible_input_tokens": 0,
        "cache_observed": False,
        "model_routes": [
            {
                "provider_id": provider,
                "model_name": model,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
                "call_count": 1,
            },
        ],
    }


def test_accumulator_merges_model_calls_within_one_turn() -> None:
    accumulator = TurnUsageAccumulator()
    accumulator.record(
        "chat-1",
        _usage("provider-a", "model-a", 10, 2),
        invocation_id="turn-1",
    )
    accumulator.record(
        "chat-1",
        _usage("provider-b", "model-b", 20, 4),
        invocation_id="turn-1",
    )

    snapshot = accumulator.peek("chat-1", invocation_id="turn-1")

    assert snapshot is not None
    assert snapshot["prompt_tokens"] == 30
    assert snapshot["completion_tokens"] == 6
    assert snapshot["total_tokens"] == 36
    assert len(snapshot["model_routes"]) == 2


def test_accumulator_keeps_unavailable_calls_out_of_token_totals() -> None:
    accumulator = TurnUsageAccumulator()
    accumulator.record(
        "chat-1",
        _usage("provider-a", "model-a", 10, 2),
        invocation_id="turn-1",
    )
    accumulator.record(
        "chat-1",
        {
            "provider_id": "provider-b",
            "model_name": "model-b",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "measurement": "unavailable",
            "usage_unobserved_calls": 1,
            "model_routes": [
                {
                    "provider_id": "provider-b",
                    "model_name": "model-b",
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "call_count": 1,
                    "usage_unobserved_calls": 1,
                },
            ],
        },
        invocation_id="turn-1",
    )

    snapshot = accumulator.peek("chat-1", invocation_id="turn-1")

    assert snapshot is not None
    assert snapshot["total_tokens"] == 12
    assert snapshot["measurement"] == "partial"
    assert snapshot["usage_unobserved_calls"] == 1
    assert snapshot["model_routes"][1]["total_tokens"] == 0
    assert snapshot["model_routes"][1]["usage_unobserved_calls"] == 1


def test_accumulator_isolates_concurrent_turns_in_one_chat() -> None:
    accumulator = TurnUsageAccumulator()
    accumulator.record(
        "chat-1",
        _usage("provider-a", "model-a", 10, 2),
        invocation_id="turn-1",
    )
    accumulator.record(
        "chat-1",
        _usage("provider-a", "model-a", 100, 20),
        invocation_id="turn-2",
    )

    assert accumulator.peek("chat-1") is None
    first = accumulator.pop("chat-1", invocation_id="turn-1")
    second = accumulator.pop("chat-1", invocation_id="turn-2")

    assert first is not None and first["total_tokens"] == 12
    assert second is not None and second["total_tokens"] == 120


def test_accumulator_supports_unambiguous_protocol_session() -> None:
    accumulator = TurnUsageAccumulator()
    accumulator.record(
        "acp-session",
        _usage("provider-a", "model-a", 10, 2),
    )

    snapshot = accumulator.pop("acp-session")

    assert snapshot is not None
    assert snapshot["total_tokens"] == 12
    assert accumulator.pop("acp-session") is None


def test_accumulator_uses_payload_turn_identity_when_key_omits_it() -> None:
    accumulator = TurnUsageAccumulator()
    usage = {**_usage("provider-a", "model-a", 10, 2), "turn_id": "turn-1"}

    accumulator.record("chat-1", usage)

    assert accumulator.peek("chat-1", invocation_id="turn-1") is not None


@pytest.mark.parametrize(
    ("usage_identity", "invocation_id", "message"),
    (
        ({"chat_id": "chat-2"}, "turn-1", "chat_id"),
        ({"turn_id": "turn-2"}, "turn-1", "turn_id"),
    ),
)
def test_accumulator_rejects_mismatched_owner_identity(
    usage_identity: dict,
    invocation_id: str,
    message: str,
) -> None:
    accumulator = TurnUsageAccumulator()
    usage = {
        **_usage("provider-a", "model-a", 10, 2),
        **usage_identity,
    }

    with pytest.raises(ValueError, match=message):
        accumulator.record(
            "chat-1",
            usage,
            invocation_id=invocation_id,
        )


def test_discard_chat_removes_every_turn_without_touching_other_chat() -> None:
    accumulator = TurnUsageAccumulator()
    usage = _usage("provider-a", "model-a", 10, 2)
    accumulator.record("chat-1", usage, invocation_id="turn-1")
    accumulator.record("chat-1", usage, invocation_id="turn-2")
    accumulator.record("chat-2", usage, invocation_id="turn-3")

    accumulator.discard_chat("chat-1")

    assert accumulator.peek("chat-1", invocation_id="turn-1") is None
    assert accumulator.peek("chat-1", invocation_id="turn-2") is None
    assert accumulator.peek("chat-2", invocation_id="turn-3") is not None


def test_accumulator_rejects_inconsistent_token_total() -> None:
    accumulator = TurnUsageAccumulator()

    with pytest.raises(ValidationError, match="total does not reconcile"):
        accumulator.record(
            "chat-1",
            {
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "total_tokens": 99,
            },
            invocation_id="turn-1",
        )


def test_accumulator_rejects_contradictory_measurement() -> None:
    accumulator = TurnUsageAccumulator()

    with pytest.raises(ValidationError, match="cannot be unobserved"):
        accumulator.record(
            "chat-1",
            {
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "total_tokens": 12,
                "measurement": "provider_reported",
                "usage_unobserved_calls": 1,
            },
            invocation_id="turn-1",
        )
