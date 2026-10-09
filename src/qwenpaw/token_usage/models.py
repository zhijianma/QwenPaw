# -*- coding: utf-8 -*-
"""Stable query contracts for model-call usage accounting."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class TokenUsageStats(BaseModel):
    """Token, prompt-cache, context, cost, and coverage statistics."""

    prompt_tokens: int = Field(0, ge=0)
    completion_tokens: int = Field(0, ge=0)
    cache_read_tokens: int = Field(0, ge=0)
    cache_write_tokens: int = Field(0, ge=0)
    cache_eligible_input_tokens: int = Field(0, ge=0)
    cache_observed_calls: int = Field(0, ge=0)
    context_input_tokens: int = Field(0, ge=0)
    context_window_tokens: int = Field(0, ge=0)
    context_observed_calls: int = Field(0, ge=0)
    near_compaction_calls: int = Field(0, ge=0)
    cost_micros: int = Field(0, ge=0)
    cost_unknown_calls: int = Field(0, ge=0)
    context_usage_ratio: Optional[float] = Field(None, ge=0)
    max_context_usage_ratio: Optional[float] = Field(None, ge=0)
    usage_observed_calls: int = Field(0, ge=0)
    usage_unobserved_calls: int = Field(0, ge=0)
    call_count: int = Field(0, ge=0)


class TokenUsageRecord(TokenUsageStats):
    """One immutable query row with optional legacy ownership."""

    date: str = Field(..., description="Date (YYYY-MM-DD)")
    provider_id: str = Field("", description="Provider ID")
    model: str = Field(..., description="Model name")
    cost_micros: int = Field(..., ge=0)
    cost_unknown_calls: int = Field(..., ge=0)
    agent_id: Optional[str] = Field(
        None,
        description=(
            "Owning agent ID; null if the stored row predates agent tracking"
        ),
    )
    conversation_id: Optional[str] = Field(
        None,
        description="Owning ChatSpec.id; null for legacy or unscoped calls",
    )
    turn_id: Optional[str] = Field(
        None,
        description="Owning OS invocation; null for legacy calls",
    )


class TokenUsageByModel(TokenUsageStats):
    """Per-model aggregate in summary (provider + model + counts)."""

    provider_id: str = Field("", description="Provider ID")
    model: str = Field(..., description="Model name")


class TokenUsageByDateModel(TokenUsageStats):
    """Per-date per-model aggregate in summary."""

    provider_id: str = Field("", description="Provider ID")
    model: str = Field(..., description="Model name")


class TokenUsageByAgent(TokenUsageStats):
    """Aggregate owned by one Agent, including unattributed legacy rows."""

    agent_id: Optional[str] = None


class TokenUsageByConversation(TokenUsageByAgent):
    """Aggregate owned by one ChatSpec within an Agent namespace."""

    conversation_id: Optional[str] = None


class TokenUsageByTurn(TokenUsageByConversation):
    """Aggregate owned by one OS turn/invocation."""

    turn_id: Optional[str] = None


class TokenUsageScopeRows(BaseModel):
    """Structured ownership dimensions without encoded dictionary keys."""

    agents: list[TokenUsageByAgent] = Field(default_factory=list)
    chats: list[TokenUsageByConversation] = Field(default_factory=list)
    turns: list[TokenUsageByTurn] = Field(default_factory=list)


class TokenUsageSummary(BaseModel):
    """Aggregated usage summary returned by the query service."""

    total_prompt_tokens: int = Field(0, ge=0)
    total_completion_tokens: int = Field(0, ge=0)
    total_cache_read_tokens: int = Field(0, ge=0)
    total_cache_write_tokens: int = Field(0, ge=0)
    total_cache_eligible_input_tokens: int = Field(0, ge=0)
    cache_observed_calls: int = Field(0, ge=0)
    cache_hit_rate: Optional[float] = Field(None, ge=0, le=100)
    total_context_input_tokens: int = Field(0, ge=0)
    total_context_window_tokens: int = Field(0, ge=0)
    context_observed_calls: int = Field(0, ge=0)
    near_compaction_calls: int = Field(0, ge=0)
    total_cost_micros: int = Field(0, ge=0)
    cost_unknown_calls: int = Field(0, ge=0)
    context_usage_ratio: Optional[float] = Field(None, ge=0)
    max_context_usage_ratio: Optional[float] = Field(None, ge=0)
    total_calls: int = Field(0, ge=0)
    usage_observed_calls: int = Field(0, ge=0)
    usage_unobserved_calls: int = Field(0, ge=0)
    by_model: dict[str, TokenUsageByModel] = Field(
        default_factory=dict,
        description="Per model (provider:model key) aggregation",
    )
    by_date: dict[str, TokenUsageStats] = Field(
        default_factory=dict,
        description="Per date (YYYY-MM-DD) - all models combined",
    )
    by_date_model: dict[str, dict[str, TokenUsageByDateModel]] = Field(
        default_factory=dict,
        description="Per date, then provider:model aggregation",
    )
    scopes: TokenUsageScopeRows = Field(
        default_factory=TokenUsageScopeRows,
        description="Structured Agent, ChatSpec, and Invocation aggregates",
    )
    by_agent: dict[str, TokenUsageByAgent] = Field(
        default_factory=dict,
        description="Compatibility map; prefer scopes.agents",
    )
    by_chat: dict[str, TokenUsageByConversation] = Field(
        default_factory=dict,
        description="Compatibility map; prefer scopes.chats",
    )
    by_turn: dict[str, TokenUsageByTurn] = Field(
        default_factory=dict,
        description="Compatibility map; prefer scopes.turns",
    )


__all__ = [
    "TokenUsageByAgent",
    "TokenUsageByConversation",
    "TokenUsageByDateModel",
    "TokenUsageByModel",
    "TokenUsageByTurn",
    "TokenUsageRecord",
    "TokenUsageScopeRows",
    "TokenUsageStats",
    "TokenUsageSummary",
]
