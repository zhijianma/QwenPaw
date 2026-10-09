# -*- coding: utf-8 -*-
"""Pure aggregation over immutable token-usage query rows."""

from __future__ import annotations

import json
from collections.abc import Sequence

from .models import (
    TokenUsageByAgent,
    TokenUsageByConversation,
    TokenUsageByDateModel,
    TokenUsageByModel,
    TokenUsageByTurn,
    TokenUsageRecord,
    TokenUsageScopeRows,
    TokenUsageStats,
    TokenUsageSummary,
)

_UNATTRIBUTED_SCOPE = "__unattributed__"


def _scope_key(*parts: str | None) -> str:
    """Build a collision-free compatibility key for one ownership scope."""
    normalized = [part or _UNATTRIBUTED_SCOPE for part in parts]
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def _new_stats(**identity: str | None) -> dict:
    """Return a mutable aggregate with optional identity fields."""
    return {
        **identity,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cache_eligible_input_tokens": 0,
        "cache_observed_calls": 0,
        "context_input_tokens": 0,
        "context_window_tokens": 0,
        "context_observed_calls": 0,
        "near_compaction_calls": 0,
        "cost_micros": 0,
        "cost_unknown_calls": 0,
        "context_usage_ratio": None,
        "max_context_usage_ratio": None,
        "usage_observed_calls": 0,
        "usage_unobserved_calls": 0,
        "call_count": 0,
    }


def _add_stats(target: dict, record: TokenUsageRecord) -> None:
    """Accumulate one immutable usage record into a mutable aggregate."""
    target["prompt_tokens"] += record.prompt_tokens
    target["completion_tokens"] += record.completion_tokens
    target["cache_read_tokens"] += record.cache_read_tokens
    target["cache_write_tokens"] += record.cache_write_tokens
    target["cache_eligible_input_tokens"] += record.cache_eligible_input_tokens
    target["cache_observed_calls"] += record.cache_observed_calls
    target["context_input_tokens"] += record.context_input_tokens
    target["context_window_tokens"] += record.context_window_tokens
    target["context_observed_calls"] += record.context_observed_calls
    target["near_compaction_calls"] += record.near_compaction_calls
    target["cost_micros"] += record.cost_micros
    target["cost_unknown_calls"] += record.cost_unknown_calls
    target["context_usage_ratio"] = (
        target["context_input_tokens"] / target["context_window_tokens"] * 100
        if target["context_window_tokens"] > 0
        else None
    )
    maxima = (
        target.get("max_context_usage_ratio"),
        record.max_context_usage_ratio,
    )
    target["max_context_usage_ratio"] = (
        max(value for value in maxima if value is not None)
        if any(value is not None for value in maxima)
        else None
    )
    target["usage_observed_calls"] += record.usage_observed_calls
    target["usage_unobserved_calls"] += record.usage_unobserved_calls
    target["call_count"] += record.call_count


def summarize_usage(
    records: Sequence[TokenUsageRecord],
) -> TokenUsageSummary:
    """Aggregate one filtered record set across every public dimension."""
    total_stats = _new_stats()
    by_model_raw: dict[str, dict] = {}
    by_date_raw: dict[str, dict] = {}
    by_date_model_raw: dict[str, dict[str, dict]] = {}
    by_agent_raw: dict[str, dict] = {}
    by_chat_raw: dict[str, dict] = {}
    by_turn_raw: dict[str, dict] = {}

    for record in records:
        _add_stats(total_stats, record)
        model_key = (
            f"{record.provider_id}:{record.model}"
            if record.provider_id
            else record.model
        )
        model_identity = {
            "provider_id": record.provider_id,
            "model": record.model,
        }
        by_model = by_model_raw.setdefault(
            model_key,
            _new_stats(**model_identity),
        )
        _add_stats(by_model, record)

        by_date = by_date_raw.setdefault(
            record.date,
            _new_stats(),
        )
        _add_stats(by_date, record)

        date_models = by_date_model_raw.setdefault(record.date, {})
        by_date_model = date_models.setdefault(
            model_key,
            _new_stats(**model_identity),
        )
        _add_stats(by_date_model, record)

        agent_key = _scope_key(record.agent_id)
        by_agent = by_agent_raw.setdefault(
            agent_key,
            _new_stats(agent_id=record.agent_id),
        )
        _add_stats(by_agent, record)

        chat_key = _scope_key(record.agent_id, record.conversation_id)
        by_chat = by_chat_raw.setdefault(
            chat_key,
            _new_stats(
                agent_id=record.agent_id,
                conversation_id=record.conversation_id,
            ),
        )
        _add_stats(by_chat, record)

        turn_key = _scope_key(
            record.agent_id,
            record.conversation_id,
            record.turn_id,
        )
        by_turn = by_turn_raw.setdefault(
            turn_key,
            _new_stats(
                agent_id=record.agent_id,
                conversation_id=record.conversation_id,
                turn_id=record.turn_id,
            ),
        )
        _add_stats(by_turn, record)

    agents = {
        key: TokenUsageByAgent.model_validate(value)
        for key, value in sorted(by_agent_raw.items())
    }
    chats = {
        key: TokenUsageByConversation.model_validate(value)
        for key, value in sorted(by_chat_raw.items())
    }
    turns = {
        key: TokenUsageByTurn.model_validate(value)
        for key, value in sorted(by_turn_raw.items())
    }
    return TokenUsageSummary(
        total_prompt_tokens=total_stats["prompt_tokens"],
        total_completion_tokens=total_stats["completion_tokens"],
        total_cache_read_tokens=total_stats["cache_read_tokens"],
        total_cache_write_tokens=total_stats["cache_write_tokens"],
        total_cache_eligible_input_tokens=(
            total_stats["cache_eligible_input_tokens"]
        ),
        cache_observed_calls=total_stats["cache_observed_calls"],
        cache_hit_rate=(
            total_stats["cache_read_tokens"]
            / total_stats["cache_eligible_input_tokens"]
            * 100
            if total_stats["cache_eligible_input_tokens"] > 0
            else None
        ),
        total_context_input_tokens=total_stats["context_input_tokens"],
        total_context_window_tokens=total_stats["context_window_tokens"],
        context_observed_calls=total_stats["context_observed_calls"],
        near_compaction_calls=total_stats["near_compaction_calls"],
        total_cost_micros=total_stats["cost_micros"],
        cost_unknown_calls=total_stats["cost_unknown_calls"],
        context_usage_ratio=total_stats["context_usage_ratio"],
        max_context_usage_ratio=total_stats["max_context_usage_ratio"],
        total_calls=total_stats["call_count"],
        usage_observed_calls=total_stats["usage_observed_calls"],
        usage_unobserved_calls=total_stats["usage_unobserved_calls"],
        by_model={
            key: TokenUsageByModel.model_validate(value)
            for key, value in sorted(by_model_raw.items())
        },
        by_date={
            key: TokenUsageStats.model_validate(value)
            for key, value in sorted(by_date_raw.items())
        },
        by_date_model={
            date_key: {
                model_key: TokenUsageByDateModel.model_validate(value)
                for model_key, value in sorted(models.items())
            }
            for date_key, models in sorted(by_date_model_raw.items())
        },
        scopes=TokenUsageScopeRows(
            agents=list(agents.values()),
            chats=list(chats.values()),
            turns=list(turns.values()),
        ),
        by_agent=agents,
        by_chat=chats,
        by_turn=turns,
    )


__all__ = ["summarize_usage"]
