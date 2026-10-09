# -*- coding: utf-8 -*-
"""Tests for the legacy token-usage compatibility adapter."""

from datetime import date

from qwenpaw.token_usage.compatibility import merge_cutover_usage
from qwenpaw.token_usage.models import TokenUsageRecord


def _record(
    usage_date: str,
    *,
    prompt_tokens: int,
    agent_id: str | None = "agent-a",
    chat_id: str | None = "chat-a",
    turn_id: str | None = "turn-a",
    context_input_tokens: int = 0,
    context_window_tokens: int = 0,
    cost_micros: int = 0,
) -> TokenUsageRecord:
    context_ratio = (
        context_input_tokens / context_window_tokens * 100
        if context_window_tokens
        else None
    )
    return TokenUsageRecord(
        date=usage_date,
        provider_id="provider-a",
        model="model-a",
        prompt_tokens=prompt_tokens,
        completion_tokens=5,
        context_input_tokens=context_input_tokens,
        context_window_tokens=context_window_tokens,
        context_observed_calls=int(context_window_tokens > 0),
        context_usage_ratio=context_ratio,
        max_context_usage_ratio=context_ratio,
        cost_micros=cost_micros,
        cost_unknown_calls=int(cost_micros == 0),
        usage_observed_calls=1,
        call_count=1,
        agent_id=agent_id,
        conversation_id=chat_id,
        turn_id=turn_id,
    )


def test_pre_cutover_shadow_only_overlays_fact_fields() -> None:
    legacy = _record("2026-10-08", prompt_tokens=100)
    shadow = _record(
        "2026-10-08",
        prompt_tokens=100,
        context_input_tokens=80,
        context_window_tokens=200,
        cost_micros=25,
    )

    [merged] = merge_cutover_usage(
        [legacy],
        [shadow],
        date(2026, 10, 9),
    )

    assert merged.prompt_tokens == 100
    assert merged.call_count == 1
    assert merged.context_input_tokens == 80
    assert merged.context_usage_ratio == 40
    assert merged.cost_micros == 25


def test_cutover_prefers_fact_rows_but_keeps_unscoped_compatibility() -> None:
    scoped_legacy = _record("2026-10-09", prompt_tokens=100)
    unscoped_legacy = _record(
        "2026-10-09",
        prompt_tokens=7,
        agent_id=None,
        chat_id=None,
        turn_id=None,
    )
    fact = _record("2026-10-09", prompt_tokens=120)

    merged = merge_cutover_usage(
        [scoped_legacy, unscoped_legacy],
        [fact],
        date(2026, 10, 9),
    )

    assert [record.prompt_tokens for record in merged] == [7, 120]
    assert merged[0].conversation_id is None
    assert merged[1].conversation_id == "chat-a"
