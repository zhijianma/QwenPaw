# -*- coding: utf-8 -*-
"""Pydantic data models for token usage tracking."""

from typing import Optional

from pydantic import BaseModel, Field


class TokenUsageStats(BaseModel):
    """Prompt/completion tokens and call count."""

    prompt_tokens: int = Field(0, ge=0)
    completion_tokens: int = Field(0, ge=0)
    call_count: int = Field(0, ge=0)


class TokenUsageRecord(TokenUsageStats):
    """Single row from token usage query (per date + provider + model)."""

    date: str = Field(..., description="Date (YYYY-MM-DD)")
    provider_id: str = Field("", description="Provider ID")
    model: str = Field(..., description="Model name")


class TokenUsageByModel(TokenUsageStats):
    """Per-model aggregate in summary (provider + model + counts)."""

    provider_id: str = Field("", description="Provider ID")
    model: str = Field(..., description="Model name")


class TokenUsageSessionStats(TokenUsageStats):
    """Token usage stats for a single conversation session."""

    agent_id: str = Field("", description="Agent ID that served this session")
    last_updated: Optional[str] = Field(
        None, description="ISO datetime of last update"
    )


class TokenUsageAgentStats(TokenUsageStats):
    """Token usage stats aggregated per agent."""

    agent_id: str = Field(..., description="Agent ID")


class TokenUsageSummary(BaseModel):
    """Aggregated token usage summary returned by get_summary()."""

    total_prompt_tokens: int = Field(0, ge=0)
    total_completion_tokens: int = Field(0, ge=0)
    total_calls: int = Field(0, ge=0)
    by_model: dict[str, TokenUsageByModel] = Field(
        default_factory=dict,
        description="Per composite key (provider:model)",
    )
    by_provider: dict[str, TokenUsageStats] = Field(
        default_factory=dict,
        description="Per provider_id",
    )
    by_date: dict[str, TokenUsageStats] = Field(
        default_factory=dict,
        description="Per date (YYYY-MM-DD)",
    )
    by_session: dict[str, TokenUsageSessionStats] = Field(
        default_factory=dict,
        description="Per session_id",
    )
    by_agent: dict[str, TokenUsageAgentStats] = Field(
        default_factory=dict,
        description="Per agent_id",
    )


__all__ = [
    "TokenUsageStats",
    "TokenUsageRecord",
    "TokenUsageByModel",
    "TokenUsageSessionStats",
    "TokenUsageAgentStats",
    "TokenUsageSummary",
]
