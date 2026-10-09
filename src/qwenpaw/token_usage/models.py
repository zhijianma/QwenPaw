# -*- coding: utf-8 -*-
"""Stable query contracts for model-call usage accounting."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Optional, Self

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)


def _validate_chat_identity_aliases(value: object) -> None:
    """Reject ambiguous canonical and legacy Chat identities."""
    if not isinstance(value, Mapping):
        return
    chat_id = value.get("chat_id")
    conversation_id = value.get("conversation_id")
    if (
        chat_id is not None
        and conversation_id is not None
        and chat_id != conversation_id
    ):
        raise ValueError("chat_id and conversation_id must identify one Chat")


class TurnModelUsageRoute(BaseModel):
    """Validated usage for one concrete Provider/Model route in a turn."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str
    model_name: str
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    call_count: int = Field(ge=1)
    usage_unobserved_calls: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_totals(self) -> Self:
        """Keep route totals and measurement coverage reconcilable."""
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError("turn route token total does not reconcile")
        if self.usage_unobserved_calls > self.call_count:
            raise ValueError("unobserved route calls exceed call count")
        return self


class TurnUsageEvidence(BaseModel):
    """Validated, content-free usage snapshot for one live Chat turn."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str = ""
    model_name: str = ""
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    cache_eligible_input_tokens: int = Field(default=0, ge=0)
    cache_observed: bool = False
    cache_hit_rate: float | None = Field(default=None, ge=0, le=100)
    session_cache_read_tokens: int = Field(default=0, ge=0)
    session_cache_eligible_input_tokens: int = Field(default=0, ge=0)
    session_cache_observed: bool = False
    session_cache_hit_rate: float | None = Field(
        default=None,
        ge=0,
        le=100,
    )
    context_size: int = Field(default=0, ge=0)
    compact_threshold: float | None = Field(
        default=None,
        gt=0,
        le=1,
    )
    estimated: bool = False
    measurement: Literal[
        "provider_reported",
        "local_estimate",
        "partial",
        "unavailable",
    ] | None = None
    usage_unobserved_calls: int = Field(default=0, ge=0)
    chat_id: str | None = None
    turn_id: str | None = None
    observed_at: str | None = None
    model_routes: tuple[TurnModelUsageRoute, ...] = Field(
        default_factory=tuple,
    )

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        """Reject internally contradictory live-turn measurements."""
        expected_total = self.prompt_tokens + self.completion_tokens
        if (
            self.total_tokens is not None
            and self.total_tokens != expected_total
        ):
            raise ValueError("turn token total does not reconcile")
        if self.measurement == "provider_reported" and (
            self.usage_unobserved_calls > 0
        ):
            raise ValueError("provider-reported turn cannot be unobserved")
        if self.measurement == "local_estimate" and not self.estimated:
            raise ValueError("local estimate must be marked estimated")
        if self.measurement == "partial" and (
            self.usage_unobserved_calls == 0 or expected_total == 0
        ):
            raise ValueError(
                "partial turn requires measured and missing usage",
            )
        if self.measurement == "unavailable" and (
            self.usage_unobserved_calls == 0 or expected_total > 0
        ):
            raise ValueError("unavailable turn cannot contain measured tokens")
        return self

    def to_payload(self) -> dict:
        """Return the compatibility dictionary without injecting defaults."""
        return self.model_dump(mode="json", exclude_unset=True)


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
    chat_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("chat_id", "conversation_id"),
        description="Owning ChatSpec.id; null for legacy or unscoped calls",
    )
    turn_id: Optional[str] = Field(
        None,
        description="Owning OS invocation; null for legacy calls",
    )

    @property
    def conversation_id(self) -> str | None:
        """Return the deprecated Python alias during migration."""
        return self.chat_id

    @model_validator(mode="before")
    @classmethod
    def validate_chat_identity(cls, value: object) -> object:
        """Reject conflicting canonical and legacy input fields."""
        _validate_chat_identity_aliases(value)
        return value


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


class TokenUsageByChat(TokenUsageByAgent):
    """Aggregate owned by one ChatSpec within an Agent namespace."""

    chat_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("chat_id", "conversation_id"),
    )

    @property
    def conversation_id(self) -> str | None:
        """Return the deprecated Python alias during migration."""
        return self.chat_id

    @model_validator(mode="before")
    @classmethod
    def validate_chat_identity(cls, value: object) -> object:
        """Reject conflicting canonical and legacy input fields."""
        _validate_chat_identity_aliases(value)
        return value


# Source compatibility for extensions importing the pre-ChatSpec type name.
TokenUsageByConversation = TokenUsageByChat


class TokenUsageByTurn(TokenUsageByChat):
    """Aggregate owned by one OS turn/invocation."""

    turn_id: Optional[str] = None


class TokenUsageScopeRows(BaseModel):
    """Structured ownership dimensions without encoded dictionary keys."""

    agents: list[TokenUsageByAgent] = Field(default_factory=list)
    chats: list[TokenUsageByChat] = Field(default_factory=list)
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
    by_chat: dict[str, TokenUsageByChat] = Field(
        default_factory=dict,
        description="Compatibility map; prefer scopes.chats",
    )
    by_turn: dict[str, TokenUsageByTurn] = Field(
        default_factory=dict,
        description="Compatibility map; prefer scopes.turns",
    )


__all__ = [
    "TokenUsageByAgent",
    "TokenUsageByChat",
    "TokenUsageByConversation",
    "TokenUsageByDateModel",
    "TokenUsageByModel",
    "TokenUsageByTurn",
    "TokenUsageRecord",
    "TokenUsageScopeRows",
    "TokenUsageStats",
    "TokenUsageSummary",
    "TurnModelUsageRoute",
    "TurnUsageEvidence",
]
