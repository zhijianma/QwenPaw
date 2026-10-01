# -*- coding: utf-8 -*-
"""Content-free contracts for durable runtime wait conditions."""

from __future__ import annotations

from enum import Enum
from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from .models import KernelModel, NonEmptyStr


class WaitConditionKind(str, Enum):
    """External facts that may suspend runtime progress."""

    APPROVAL = "approval"
    USER_INPUT = "user_input"
    TIMER = "timer"
    EXTERNAL_EVENT = "external_event"
    RESOURCE = "resource"


class WaitConditionStatus(str, Enum):
    """Normalized lifecycle shared by every wait-condition provider."""

    WAITING = "waiting"
    SATISFIED = "satisfied"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ContinuationMode(str, Enum):
    """How execution can continue after a condition becomes satisfied."""

    LIVE_INVOCATION = "live_invocation"
    CHECKPOINT = "checkpoint"


class ContinuationAvailability(str, Enum):
    """Whether a continuation executor is currently attached."""

    ATTACHED = "attached"
    DETACHED = "detached"


class ContinuationRef(KernelModel):
    """Content-free pointer to execution state awaiting a condition."""

    mode: ContinuationMode
    availability: ContinuationAvailability
    invocation_id: UUID
    checkpoint_id: UUID | None = None

    @model_validator(mode="after")
    def validate_checkpoint(self) -> Self:
        """Require a durable checkpoint only for checkpoint continuation."""
        if (
            self.mode is ContinuationMode.CHECKPOINT
            and self.checkpoint_id is None
        ):
            raise ValueError("checkpoint continuation requires checkpoint_id")
        if (
            self.mode is ContinuationMode.LIVE_INVOCATION
            and self.checkpoint_id is not None
        ):
            raise ValueError(
                "live invocation continuation cannot reference a checkpoint",
            )
        return self


class WaitCondition(KernelModel):
    """One durable blocker projected from an authoritative source fact."""

    condition_id: UUID
    kind: WaitConditionKind
    status: WaitConditionStatus
    agent_id: NonEmptyStr
    conversation_id: NonEmptyStr
    source_type: NonEmptyStr
    source_id: UUID
    policy_source_id: UUID | None = None
    continuation: ContinuationRef
    revision: int = Field(ge=1)
    created_at: AwareDatetime
    resolved_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_terminal_time(self) -> Self:
        """Keep lifecycle timestamps coherent without copying source data."""
        terminal = self.status is not WaitConditionStatus.WAITING
        if terminal != (self.resolved_at is not None):
            raise ValueError("terminal wait condition requires resolved_at")
        if self.resolved_at is not None and self.resolved_at < self.created_at:
            raise ValueError("resolved_at cannot precede created_at")
        return self


__all__ = [
    "ContinuationAvailability",
    "ContinuationMode",
    "ContinuationRef",
    "WaitCondition",
    "WaitConditionKind",
    "WaitConditionStatus",
]
