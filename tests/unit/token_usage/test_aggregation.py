# -*- coding: utf-8 -*-
"""Tests for pure token-usage aggregation."""

from qwenpaw.token_usage.aggregation import summarize_usage
from qwenpaw.token_usage.models import TokenUsageRecord


def _record(
    *,
    agent_id: str,
    chat_id: str,
    turn_id: str,
    input_tokens: int,
    context_window_tokens: int,
    observed: bool = True,
) -> TokenUsageRecord:
    return TokenUsageRecord(
        date="2026-10-09",
        provider_id="provider-a",
        model="model-a",
        prompt_tokens=input_tokens,
        completion_tokens=5 if observed else 0,
        context_input_tokens=input_tokens if observed else 0,
        context_window_tokens=(context_window_tokens if observed else 0),
        context_observed_calls=int(observed),
        max_context_usage_ratio=(
            input_tokens / context_window_tokens * 100 if observed else None
        ),
        cost_micros=0,
        cost_unknown_calls=1,
        usage_observed_calls=int(observed),
        usage_unobserved_calls=int(not observed),
        call_count=1,
        agent_id=agent_id,
        conversation_id=chat_id,
        turn_id=turn_id,
    )


def test_summary_uses_weighted_context_ratio_and_preserves_peak() -> None:
    summary = summarize_usage(
        [
            _record(
                agent_id="agent-a",
                chat_id="chat-a",
                turn_id="turn-a",
                input_tokens=50,
                context_window_tokens=100,
            ),
            _record(
                agent_id="agent-a",
                chat_id="chat-a",
                turn_id="turn-b",
                input_tokens=100,
                context_window_tokens=400,
            ),
        ],
    )

    assert summary.context_usage_ratio == 30
    assert summary.max_context_usage_ratio == 50
    assert summary.total_calls == 2
    assert len(summary.scopes.turns) == 2


def test_summary_keeps_unobserved_calls_in_every_scope() -> None:
    summary = summarize_usage(
        [
            _record(
                agent_id="agent-a",
                chat_id="chat-a",
                turn_id="turn-a",
                input_tokens=0,
                context_window_tokens=100,
                observed=False,
            ),
        ],
    )

    assert summary.total_calls == 1
    assert summary.usage_observed_calls == 0
    assert summary.usage_unobserved_calls == 1
    assert summary.context_usage_ratio is None
    assert summary.scopes.agents[0].usage_unobserved_calls == 1
    assert summary.scopes.chats[0].usage_unobserved_calls == 1
    assert summary.scopes.turns[0].usage_unobserved_calls == 1
