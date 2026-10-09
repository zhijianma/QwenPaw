# -*- coding: utf-8 -*-
"""Compatibility adapter for the pre-Model-Call usage projection."""

from __future__ import annotations

from datetime import date, timedelta

from .models import TokenUsageRecord


def _matches_filters(
    *,
    model: str,
    provider_id: str,
    agent_id: str | None,
    conversation_id: str | None,
    turn_id: str | None,
    expected_model: str | None,
    expected_provider: str | None,
    expected_agent: str | None,
    expected_conversation: str | None,
    expected_turn: str | None,
) -> bool:
    """Return whether one compatibility row belongs to the query scope."""
    return all(
        (
            expected_model is None or model == expected_model,
            expected_provider is None or provider_id == expected_provider,
            expected_agent is None or agent_id == expected_agent,
            expected_conversation is None
            or conversation_id == expected_conversation,
            expected_turn is None or turn_id == expected_turn,
        ),
    )


def query_legacy_usage(
    merged: dict,
    start_date: date,
    end_date: date,
    *,
    model_name: str | None = None,
    provider_id: str | None = None,
    agent_id: str | None = None,
    conversation_id: str | None = None,
    turn_id: str | None = None,
) -> list[TokenUsageRecord]:
    """Decode and filter rows from the legacy aggregate JSON shape."""
    results: list[TokenUsageRecord] = []
    current = start_date
    while current <= end_date:
        date_str = current.isoformat()
        by_key = merged.get(date_str, {})
        for raw_key, entry in by_key.items():
            record_provider = entry.get("provider_id", "") or ""
            record_agent = (
                None
                if "agent_id" not in entry
                else entry.get("agent_id") or ""
            )
            record_conversation = entry.get("conversation_id") or None
            record_turn = entry.get("turn_id") or None
            record_model = entry.get("model_name") or ""
            if not record_model:
                key = str(raw_key)
                if "\x1f" in key:
                    record_model = key.rsplit("\x1f", 1)[-1]
                elif ":" in key:
                    record_model = key.split(":", 1)[1]
                else:
                    record_model = key
            if not _matches_filters(
                model=record_model,
                provider_id=record_provider,
                agent_id=record_agent,
                conversation_id=record_conversation,
                turn_id=record_turn,
                expected_model=model_name,
                expected_provider=provider_id,
                expected_agent=agent_id,
                expected_conversation=conversation_id,
                expected_turn=turn_id,
            ):
                continue
            call_count = entry.get("call_count", 0)
            results.append(
                TokenUsageRecord(
                    date=date_str,
                    provider_id=record_provider,
                    model=record_model,
                    prompt_tokens=entry.get("prompt_tokens", 0),
                    completion_tokens=entry.get("completion_tokens", 0),
                    cache_read_tokens=entry.get("cache_read_tokens", 0),
                    cache_write_tokens=entry.get("cache_write_tokens", 0),
                    cache_eligible_input_tokens=entry.get(
                        "cache_eligible_input_tokens",
                        0,
                    ),
                    cache_observed_calls=entry.get(
                        "cache_observed_calls",
                        0,
                    ),
                    cost_micros=0,
                    cost_unknown_calls=call_count,
                    usage_observed_calls=call_count,
                    usage_unobserved_calls=0,
                    call_count=call_count,
                    agent_id=record_agent,
                    conversation_id=record_conversation,
                    turn_id=record_turn,
                ),
            )
        current += timedelta(days=1)
    return results


def _record_identity(record: TokenUsageRecord) -> tuple[str | None, ...]:
    """Return the exact identity shared by legacy and shadow rows."""
    return (
        record.date,
        record.agent_id,
        record.conversation_id,
        record.turn_id,
        record.provider_id,
        record.model,
    )


def _overlay_fact_stats(
    legacy: TokenUsageRecord,
    shadow: TokenUsageRecord,
) -> TokenUsageRecord:
    """Add fact-only fields without duplicating compatibility totals."""
    return legacy.model_copy(
        update={
            "context_input_tokens": shadow.context_input_tokens,
            "context_window_tokens": shadow.context_window_tokens,
            "context_observed_calls": shadow.context_observed_calls,
            "near_compaction_calls": shadow.near_compaction_calls,
            "context_usage_ratio": shadow.context_usage_ratio,
            "max_context_usage_ratio": shadow.max_context_usage_ratio,
            "cost_micros": shadow.cost_micros,
            "cost_unknown_calls": shadow.cost_unknown_calls,
        },
    )


def merge_cutover_usage(
    legacy: list[TokenUsageRecord],
    projected: list[TokenUsageRecord],
    cutover: date,
) -> list[TokenUsageRecord]:
    """Merge compatibility rows with authoritative fact projections once."""
    shadow_by_identity = {
        _record_identity(record): record
        for record in projected
        if date.fromisoformat(record.date) < cutover
    }
    compatible: list[TokenUsageRecord] = []
    for record in legacy:
        before_cutover = date.fromisoformat(record.date) < cutover
        unscoped = record.conversation_id is None and record.turn_id is None
        if not before_cutover and not unscoped:
            continue
        shadow = shadow_by_identity.get(_record_identity(record))
        compatible.append(
            _overlay_fact_stats(record, shadow)
            if before_cutover and shadow is not None
            else record,
        )
    authoritative = [
        record
        for record in projected
        if date.fromisoformat(record.date) >= cutover
    ]
    return [*compatible, *authoritative]


__all__ = ["merge_cutover_usage", "query_legacy_usage"]
