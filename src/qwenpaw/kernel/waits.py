# -*- coding: utf-8 -*-
"""Content-free contracts for durable runtime wait conditions."""

from __future__ import annotations

from datetime import timedelta
from enum import Enum
from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from .models import (
    KernelModel,
    ModelFailureClass,
    ModelOutputBoundary,
    NonEmptyStr,
    utc_now,
)


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
    CONVERSATION_TURN = "conversation_turn"


class ContinuationAvailability(str, Enum):
    """Whether a continuation executor is currently attached."""

    ATTACHED = "attached"
    DETACHED = "detached"


class ContinuationDispatchStatus(str, Enum):
    """Delivery state for a durable conversation continuation."""

    READY = "ready"
    DISPATCHED = "dispatched"


class ResourceWaitTrigger(str, Enum):
    """Fact that can make a model resource available again."""

    TIMER = "timer"
    EXTERNAL_EVENT = "external_event"


class ResourceWaitStatus(str, Enum):
    """Lifecycle of one model-resource continuation outbox entry."""

    WAITING = "waiting"
    READY = "ready"
    DISPATCHED = "dispatched"
    CANCELLED = "cancelled"
    RECOVERY_EXHAUSTED = "recovery_exhausted"


class ModelStepContinuationStatus(str, Enum):
    """Lifecycle of one partial-model-step continuation outbox entry."""

    READY = "ready"
    DISPATCHED = "dispatched"
    CANCELLED = "cancelled"
    ACTION_RECONCILIATION_REQUIRED = "action_reconciliation_required"
    RECOVERY_EXHAUSTED = "recovery_exhausted"


class ModelStepContinuation(KernelModel):
    """Content-free continuation after a partial model stream fails."""

    continuation_id: UUID
    attempt_id: UUID
    invocation_id: UUID
    correlation_id: UUID
    agent_id: NonEmptyStr
    conversation_id: NonEmptyStr
    output_boundary: ModelOutputBoundary
    status: ModelStepContinuationStatus = (
        ModelStepContinuationStatus.READY
    )
    submission_id: UUID | None = None
    revision: int = Field(default=1, ge=1)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_model_step_continuation(self) -> Self:
        """Require a partial boundary and an exact dispatch binding."""
        if self.output_boundary not in {
            ModelOutputBoundary.PARTIAL_STREAM,
            ModelOutputBoundary.INCOMPLETE_STREAM_END,
        }:
            raise ValueError(
                "model-step continuation requires partial output boundary",
            )
        dispatched = (
            self.status is ModelStepContinuationStatus.DISPATCHED
        )
        if dispatched != (self.submission_id is not None):
            raise ValueError(
                "dispatched model-step continuation requires submission_id",
            )
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


class ModelResourceWait(KernelModel):
    """Content-free durable wait created by a failed model attempt."""

    wait_id: UUID
    attempt_id: UUID
    invocation_id: UUID
    correlation_id: UUID
    agent_id: NonEmptyStr
    conversation_id: NonEmptyStr
    failure_class: ModelFailureClass
    trigger: ResourceWaitTrigger
    status: ResourceWaitStatus = ResourceWaitStatus.WAITING
    not_before: AwareDatetime | None = None
    submission_id: UUID | None = None
    revision: int = Field(default=1, ge=1)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_resource_wait(self) -> Self:
        """Keep trigger, failure, and dispatch state unambiguous."""
        supported = {
            ModelFailureClass.TRANSPORT_UNAVAILABLE,
            ModelFailureClass.PROVIDER_OVERLOADED,
            ModelFailureClass.RATE_LIMITED,
            ModelFailureClass.QUOTA_EXHAUSTED,
        }
        if self.failure_class not in supported:
            raise ValueError("unsupported model resource failure class")
        if self.trigger is ResourceWaitTrigger.TIMER:
            if self.not_before is None:
                raise ValueError("timer resource wait requires not_before")
        elif self.not_before is not None:
            raise ValueError(
                "external-event resource wait cannot set not_before",
            )
        dispatched = self.status is ResourceWaitStatus.DISPATCHED
        if dispatched != (self.submission_id is not None):
            raise ValueError(
                "dispatched resource wait requires submission_id",
            )
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self

    @classmethod
    def for_model_failure(
        cls,
        *,
        wait_id: UUID,
        attempt_id: UUID,
        invocation_id: UUID,
        correlation_id: UUID,
        agent_id: str,
        conversation_id: str,
        failure_class: ModelFailureClass,
        retry_delay_seconds: float = 60,
        created_at: AwareDatetime | None = None,
    ) -> "ModelResourceWait":
        """Build the default Lite wait without provider payloads."""
        if retry_delay_seconds < 1:
            raise ValueError("resource retry delay must be positive")
        created_at = created_at or utc_now()
        timed = failure_class is not ModelFailureClass.QUOTA_EXHAUSTED
        return cls(
            wait_id=wait_id,
            attempt_id=attempt_id,
            invocation_id=invocation_id,
            correlation_id=correlation_id,
            agent_id=agent_id,
            conversation_id=conversation_id,
            failure_class=failure_class,
            trigger=(
                ResourceWaitTrigger.TIMER
                if timed
                else ResourceWaitTrigger.EXTERNAL_EVENT
            ),
            not_before=(
                created_at + timedelta(seconds=retry_delay_seconds)
                if timed
                else None
            ),
            created_at=created_at,
            updated_at=created_at,
        )


class ConversationContinuation(KernelModel):
    """Content-free outbox entry that creates a later conversation turn."""

    interaction_id: UUID
    agent_id: NonEmptyStr
    conversation_id: NonEmptyStr
    response_revision: int = Field(ge=2)
    status: ContinuationDispatchStatus = ContinuationDispatchStatus.READY
    submission_id: UUID | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_dispatch(self) -> Self:
        """Require a submission identity exactly after dispatch."""
        dispatched = self.status is ContinuationDispatchStatus.DISPATCHED
        if dispatched != (self.submission_id is not None):
            raise ValueError(
                "dispatched continuation requires submission_id",
            )
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


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
        if (
            self.mode is ContinuationMode.CONVERSATION_TURN
            and self.checkpoint_id is not None
        ):
            raise ValueError(
                "conversation continuation cannot reference a checkpoint",
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
    not_before: AwareDatetime | None = None
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
    "ContinuationDispatchStatus",
    "ContinuationMode",
    "ContinuationRef",
    "ConversationContinuation",
    "ModelStepContinuation",
    "ModelStepContinuationStatus",
    "ModelResourceWait",
    "ResourceWaitStatus",
    "ResourceWaitTrigger",
    "WaitCondition",
    "WaitConditionKind",
    "WaitConditionStatus",
]
