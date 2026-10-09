# -*- coding: utf-8 -*-
"""Durable long-running Goal execution state for one Chat."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import AliasChoices, AwareDatetime, Field, model_validator

from .models import KernelModel, NonEmptyStr, utc_now
from .outcomes import ConversationOutcomeStatus


class GoalExecutionStatus(str, Enum):
    """Durable lifecycle independent from one Invocation or response."""

    ACTIVE = "active"
    OUTCOME_PENDING = "outcome_pending"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    ABANDONED = "abandoned"
    EXHAUSTED = "exhausted"


class GoalExecution(KernelModel):
    """Revisioned current Goal owned by a stable ChatSpec identity."""

    schema_id: Literal["qwenpaw.goal-execution.v1"] = Field(
        default="qwenpaw.goal-execution.v1",
        alias="schema",
    )
    goal_id: UUID = Field(default_factory=uuid4)
    agent_id: NonEmptyStr
    chat_id: NonEmptyStr = Field(
        validation_alias=AliasChoices("chat_id", "conversation_id"),
        description="Owning ChatSpec.id",
    )
    correlation_id: UUID
    objective: str = Field(min_length=1, max_length=20000)
    status: GoalExecutionStatus = GoalExecutionStatus.ACTIVE
    iteration: int = Field(default=0, ge=0)
    max_iterations: int = Field(ge=1)
    tokens_used: int = Field(default=0, ge=0)
    token_budget: int = Field(ge=1)
    last_verdict: str = Field(default="", max_length=2000)
    last_feedback: str = Field(default="", max_length=4000)
    outcome_id: UUID | None = None
    outcome_status: ConversationOutcomeStatus | None = None
    revision: int = Field(default=0, ge=0)
    started_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="before")
    @classmethod
    def validate_chat_identity(cls, value: object) -> object:
        """Reject ambiguous canonical and legacy Chat identities."""
        if isinstance(value, Mapping):
            chat_id = value.get("chat_id")
            conversation_id = value.get("conversation_id")
            if chat_id and conversation_id and chat_id != conversation_id:
                raise ValueError(
                    "chat_id and conversation_id must identify one Chat",
                )
        return value

    @property
    def conversation_id(self) -> str:
        """Return the deprecated Python alias during migration."""
        return self.chat_id

    @model_validator(mode="after")
    def validate_terminal_intent(self) -> Self:
        """Keep pending/final outcome identity explicit and paired."""
        has_outcome = self.outcome_id is not None
        if has_outcome != (self.outcome_status is not None):
            raise ValueError("goal outcome identity and status must be paired")
        outcome_owned = self.status in {
            GoalExecutionStatus.OUTCOME_PENDING,
            GoalExecutionStatus.COMPLETED,
            GoalExecutionStatus.BLOCKED,
        }
        if outcome_owned != has_outcome:
            raise ValueError(
                "goal pending/completed/blocked state requires an outcome",
            )
        if (
            self.status is GoalExecutionStatus.COMPLETED
            and self.outcome_status is not ConversationOutcomeStatus.ACHIEVED
        ):
            raise ValueError("completed goal requires achieved outcome")
        if (
            self.status is GoalExecutionStatus.BLOCKED
            and self.outcome_status
            is not ConversationOutcomeStatus.NOT_ACHIEVED
        ):
            raise ValueError("blocked goal requires not-achieved outcome")
        if self.updated_at < self.started_at:
            raise ValueError("goal updated_at cannot predate started_at")
        return self


class GoalExecutionConflictError(RuntimeError):
    """Raised when a Goal compare-and-swap revision is stale."""


__all__ = [
    "GoalExecution",
    "GoalExecutionConflictError",
    "GoalExecutionStatus",
]
