# -*- coding: utf-8 -*-
"""Content-free contracts for durable runtime wait conditions."""

from __future__ import annotations

from datetime import timedelta
from enum import Enum
from typing import Annotated, Self
from uuid import UUID

from pydantic import (
    AliasChoices,
    AwareDatetime,
    Field,
    StringConstraints,
    model_validator,
)

from .models import (
    ActionRetryInputCheckpoint,
    CommittedActionItem,
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


class HarnessStepContinuationStatus(str, Enum):
    """Lifecycle of one Harness recovery continuation outbox entry."""

    READY = "ready"
    DISPATCHED = "dispatched"
    CANCELLED = "cancelled"
    RECOVERY_EXHAUSTED = "recovery_exhausted"


class BackgroundActionContinuationStatus(str, Enum):
    """Lifecycle of one completed background Action continuation."""

    WAITING_SOURCE = "waiting_source"
    READY = "ready"
    DISPATCHED = "dispatched"
    CANCELLED = "cancelled"
    RECOVERY_EXHAUSTED = "recovery_exhausted"


class ActionRetryContinuationStatus(str, Enum):
    """Lifecycle of one Host-admitted Action retry outbox entry."""

    WAITING_DELAY = "waiting_delay"
    READY = "ready"
    DISPATCHED = "dispatched"
    CANCELLED = "cancelled"


class ModelStepReconciliationReason(str, Enum):
    """Why a partial model step cannot continue automatically."""

    PENDING_ACTION_RESULT = "pending_action_result"
    UNCERTAIN_SIDE_EFFECT = "uncertain_side_effect"
    DURABLE_CONTEXT_REQUIRED = "durable_context_required"


class ModelStepReconciliation(KernelModel):
    """Content-free assessment of Actions owned by a failed model step."""

    reason: ModelStepReconciliationReason
    action_count: int = Field(ge=1)
    pending_result_count: int = Field(ge=0)
    uncertain_side_effect_count: int = Field(ge=0)
    terminal_result_count: int = Field(ge=0)
    assessed_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        """Require every Action to be pending or terminal exactly once."""
        if (
            self.pending_result_count + self.terminal_result_count
            != self.action_count
        ):
            raise ValueError(
                "pending and terminal action counts must equal action_count",
            )
        if self.uncertain_side_effect_count > self.terminal_result_count:
            raise ValueError(
                "uncertain side effects cannot exceed terminal actions",
            )
        if (
            self.reason is ModelStepReconciliationReason.UNCERTAIN_SIDE_EFFECT
            and self.uncertain_side_effect_count == 0
        ):
            raise ValueError(
                "uncertain reason requires an uncertain side effect",
            )
        if (
            self.reason is ModelStepReconciliationReason.PENDING_ACTION_RESULT
            and (
                self.pending_result_count == 0
                or self.uncertain_side_effect_count != 0
            )
        ):
            raise ValueError(
                "pending reason requires pending results and no uncertainty",
            )
        if (
            self.reason
            is ModelStepReconciliationReason.DURABLE_CONTEXT_REQUIRED
            and (
                self.pending_result_count != 0
                or self.uncertain_side_effect_count != 0
            )
        ):
            raise ValueError(
                "durable context reason requires terminal certain actions",
            )
        return self


class ModelStepContextCheckpoint(KernelModel):
    """Content-safe reference to an immutable private context snapshot."""

    checkpoint_id: UUID
    continuation_id: UUID
    invocation_id: UUID
    conversation_id: NonEmptyStr
    source_submission_id: UUID
    action_evidence_digest: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            pattern=r"^sha256:[0-9a-f]{64}$",
        ),
    ]
    action_count: int = Field(ge=1)
    created_at: AwareDatetime = Field(default_factory=utc_now)


class ModelStepRetryAuthorization(KernelModel):
    """Exact user authorization to retry after uncertain side effects."""

    interaction_id: UUID
    response_revision: int = Field(ge=2)
    continuation_id: UUID
    invocation_id: UUID
    conversation_id: NonEmptyStr
    action_evidence_digest: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            pattern=r"^sha256:[0-9a-f]{64}$",
        ),
    ]
    action_count: int = Field(ge=1)
    authorized_at: AwareDatetime = Field(default_factory=utc_now)


class HarnessRecoveryContextCheckpoint(KernelModel):
    """Content-safe proof that a Harness context can continue safely."""

    checkpoint_id: UUID
    invocation_id: UUID
    conversation_id: NonEmptyStr
    source_submission_id: UUID
    backend: NonEmptyStr
    provider_context_digest: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            pattern=r"^sha256:[0-9a-f]{64}$",
        ),
    ]
    provider_item_digest: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            pattern=r"^sha256:[0-9a-f]{64}$",
        ),
    ]
    action_evidence_digest: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            pattern=r"^sha256:[0-9a-f]{64}$",
        ),
    ]
    action_count: int = Field(ge=1)
    created_at: AwareDatetime = Field(default_factory=utc_now)


class HarnessStepContinuation(KernelModel):
    """Content-safe durable continuation for an interrupted Harness turn."""

    continuation_id: UUID
    checkpoint: HarnessRecoveryContextCheckpoint
    correlation_id: UUID
    agent_id: NonEmptyStr
    recovery_cycle: int = Field(ge=1)
    status: HarnessStepContinuationStatus = HarnessStepContinuationStatus.READY
    submission_id: UUID | None = None
    revision: int = Field(default=1, ge=1)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_harness_continuation(self) -> Self:
        """Require an exact dispatch binding and coherent timestamps."""
        dispatched = self.status is HarnessStepContinuationStatus.DISPATCHED
        if dispatched != (self.submission_id is not None):
            raise ValueError(
                "dispatched Harness continuation requires submission_id",
            )
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


class BackgroundActionContextCheckpoint(KernelModel):
    """Content-safe reference to a private background result snapshot."""

    checkpoint_id: UUID
    continuation_id: UUID
    committed_item: CommittedActionItem
    source_submission_id: UUID
    correlation_id: UUID
    agent_id: NonEmptyStr
    recovery_cycle: int = Field(ge=1)
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_background_action_checkpoint(self) -> Self:
        """Require the committed result to belong to one Conversation."""
        if self.committed_item.conversation_id is None:
            raise ValueError(
                "background Action checkpoint requires conversation_id",
            )
        return self


class BackgroundActionContinuation(KernelModel):
    """Durable continuation after one offloaded Action completes."""

    continuation_id: UUID
    checkpoint: BackgroundActionContextCheckpoint
    status: BackgroundActionContinuationStatus = (
        BackgroundActionContinuationStatus.WAITING_SOURCE
    )
    submission_id: UUID | None = None
    revision: int = Field(default=1, ge=1)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_background_action_continuation(self) -> Self:
        """Require an exact dispatch binding and coherent timestamps."""
        dispatched = (
            self.status is BackgroundActionContinuationStatus.DISPATCHED
        )
        if dispatched != (self.submission_id is not None):
            raise ValueError(
                "dispatched background continuation requires submission_id",
            )
        if self.continuation_id != self.checkpoint.continuation_id:
            raise ValueError("background continuation checkpoint mismatch")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


class ActionRetryContinuation(KernelModel):
    """Content-safe durable work item for one exact Action retry."""

    continuation_id: UUID
    checkpoint: ActionRetryInputCheckpoint
    source_observation_digest: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            pattern=r"^sha256:[0-9a-f]{64}$",
        ),
    ]
    ready_at: AwareDatetime
    status: ActionRetryContinuationStatus
    dispatch_id: UUID | None = None
    recovered_dispatch_id: UUID | None = None
    revision: int = Field(default=1, ge=1)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_action_retry_continuation(self) -> Self:
        """Require coherent dispatch binding and durable timestamps."""
        dispatched = self.status is ActionRetryContinuationStatus.DISPATCHED
        if dispatched != (self.dispatch_id is not None):
            raise ValueError(
                "dispatched Action retry requires dispatch_id",
            )
        if (
            self.recovered_dispatch_id is not None
            and self.status is not ActionRetryContinuationStatus.READY
        ):
            raise ValueError(
                "recovered Action retry must return to ready",
            )
        if self.ready_at < self.created_at:
            raise ValueError("Action retry ready_at precedes creation")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


class ModelStepContinuation(KernelModel):
    """Content-free continuation after a partial model stream fails."""

    continuation_id: UUID
    attempt_id: UUID
    invocation_id: UUID
    correlation_id: UUID
    agent_id: NonEmptyStr
    conversation_id: NonEmptyStr
    output_boundary: ModelOutputBoundary
    status: ModelStepContinuationStatus = ModelStepContinuationStatus.READY
    reconciliation: ModelStepReconciliation | None = None
    context_checkpoint: ModelStepContextCheckpoint | None = None
    retry_authorization: ModelStepRetryAuthorization | None = None
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
        dispatched = self.status is ModelStepContinuationStatus.DISPATCHED
        if dispatched != (self.submission_id is not None):
            raise ValueError(
                "dispatched model-step continuation requires submission_id",
            )
        if (
            self.reconciliation is not None
            and self.status
            is not ModelStepContinuationStatus.ACTION_RECONCILIATION_REQUIRED
        ):
            raise ValueError(
                "model-step reconciliation requires blocked status",
            )
        checkpoint = self.context_checkpoint
        if checkpoint is not None and (
            checkpoint.continuation_id != self.continuation_id
            or checkpoint.invocation_id != self.invocation_id
            or checkpoint.conversation_id != self.conversation_id
        ):
            raise ValueError(
                "model-step checkpoint identity does not match continuation",
            )
        authorization = self.retry_authorization
        if authorization is not None:
            if (
                authorization.continuation_id != self.continuation_id
                or authorization.invocation_id != self.invocation_id
                or authorization.conversation_id != self.conversation_id
            ):
                raise ValueError(
                    "model-step retry authorization identity mismatch",
                )
            if self.context_checkpoint is not None:
                raise ValueError(
                    "retry authorization cannot use committed Action context",
                )
            if self.reconciliation is not None:
                raise ValueError(
                    "authorized model-step retry cannot remain blocked",
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
    provider_id: NonEmptyStr | None = None
    model_id: NonEmptyStr | None = None
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
        if (self.provider_id is None) != (self.model_id is None):
            raise ValueError(
                "model resource identity must include provider and model",
            )
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
        provider_id: str | None = None,
        model_id: str | None = None,
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
            provider_id=provider_id,
            model_id=model_id,
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
    chat_id: NonEmptyStr = Field(
        validation_alias=AliasChoices("chat_id", "conversation_id"),
        description="Owning ChatSpec.id",
    )
    source_type: NonEmptyStr
    source_id: UUID
    policy_source_id: UUID | None = None
    continuation: ContinuationRef
    revision: int = Field(ge=1)
    created_at: AwareDatetime
    not_before: AwareDatetime | None = None
    resolved_at: AwareDatetime | None = None

    @property
    def conversation_id(self) -> str:
        """Return the deprecated Python alias during migration."""
        return self.chat_id

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
    "ActionRetryContinuation",
    "ActionRetryContinuationStatus",
    "BackgroundActionContextCheckpoint",
    "BackgroundActionContinuation",
    "BackgroundActionContinuationStatus",
    "ContinuationAvailability",
    "ContinuationDispatchStatus",
    "ContinuationMode",
    "ContinuationRef",
    "ConversationContinuation",
    "HarnessRecoveryContextCheckpoint",
    "HarnessStepContinuation",
    "HarnessStepContinuationStatus",
    "ModelStepContextCheckpoint",
    "ModelStepRetryAuthorization",
    "ModelStepContinuation",
    "ModelStepContinuationStatus",
    "ModelStepReconciliation",
    "ModelStepReconciliationReason",
    "ModelResourceWait",
    "ResourceWaitStatus",
    "ResourceWaitTrigger",
    "WaitCondition",
    "WaitConditionKind",
    "WaitConditionStatus",
]
