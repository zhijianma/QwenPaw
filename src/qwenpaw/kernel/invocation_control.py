# -*- coding: utf-8 -*-
"""Server-authoritative queue and invocation-control contracts."""

from __future__ import annotations

from enum import Enum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AliasChoices, AwareDatetime, Field, model_validator

from .communications import CommunicationContract
from .models import (
    ArtifactRef,
    JsonObject,
    KernelModel,
    NonEmptyStr,
    utc_now,
)
from .interactions import InteractionRequest
from .observations import ObservationPage
from .outcomes import ConversationOutcome, ConversationOutcomeStatus


class SubmissionStatus(str, Enum):
    """Durable lifecycle for one submitted conversation turn."""

    QUEUED = "queued"
    ADMITTED = "admitted"
    RUNNING = "running"
    INTERRUPTING = "interrupting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class ConversationExecutionState(str, Enum):
    """Derived state of one intent-scoped conversation execution chain."""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    INACTIVE = "inactive"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    ACHIEVED = "achieved"
    PARTIAL = "partial"
    NOT_ACHIEVED = "not_achieved"
    ABANDONED = "abandoned"


def outcome_execution_state(
    status: ConversationOutcomeStatus,
) -> ConversationExecutionState:
    """Map one explicit business outcome to its execution-chain state."""
    return {
        ConversationOutcomeStatus.ACHIEVED: (
            ConversationExecutionState.ACHIEVED
        ),
        ConversationOutcomeStatus.PARTIAL: (
            ConversationExecutionState.PARTIAL
        ),
        ConversationOutcomeStatus.NOT_ACHIEVED: (
            ConversationExecutionState.NOT_ACHIEVED
        ),
        ConversationOutcomeStatus.ABANDONED: (
            ConversationExecutionState.ABANDONED
        ),
    }[status]


class ControlCommandKind(str, Enum):
    """Explicit user intents supported by the invocation control plane."""

    STEER = "steer"
    INTERRUPT_CURRENT = "interrupt_current"
    CANCEL_QUEUED = "cancel_queued"
    STOP_AND_CLEAR = "stop_and_clear"
    REORDER = "reorder"


class ControlCommandStatus(str, Enum):
    """Durable acknowledgement state for one control command."""

    ACCEPTED = "accepted"
    APPLIED = "applied"
    REJECTED = "rejected"
    CONFLICT = "conflict"


class SteerSafePoint(str, Enum):
    """Runtime boundaries where a steer may change future execution."""

    BEFORE_REASONING = "before_reasoning"
    AFTER_REASONING = "after_reasoning"
    BEFORE_TOOL_BATCH = "before_tool_batch"
    AFTER_TOOL_BATCH = "after_tool_batch"


ACTIVE_SUBMISSION_STATUSES = frozenset(
    {
        SubmissionStatus.ADMITTED,
        SubmissionStatus.RUNNING,
        SubmissionStatus.INTERRUPTING,
    },
)
TERMINAL_SUBMISSION_STATUSES = frozenset(
    {
        SubmissionStatus.SUCCEEDED,
        SubmissionStatus.FAILED,
        SubmissionStatus.INTERRUPTED,
        SubmissionStatus.CANCELLED,
    },
)
SUBMISSION_TRANSITIONS: dict[
    SubmissionStatus,
    frozenset[SubmissionStatus],
] = {
    SubmissionStatus.QUEUED: frozenset(
        {
            SubmissionStatus.ADMITTED,
            SubmissionStatus.CANCELLED,
        },
    ),
    SubmissionStatus.ADMITTED: frozenset(
        {
            SubmissionStatus.QUEUED,
            SubmissionStatus.RUNNING,
            SubmissionStatus.INTERRUPTED,
            SubmissionStatus.CANCELLED,
        },
    ),
    SubmissionStatus.RUNNING: frozenset(
        {
            SubmissionStatus.INTERRUPTING,
            SubmissionStatus.SUCCEEDED,
            SubmissionStatus.FAILED,
        },
    ),
    SubmissionStatus.INTERRUPTING: frozenset(
        {
            SubmissionStatus.INTERRUPTED,
            SubmissionStatus.SUCCEEDED,
            SubmissionStatus.FAILED,
        },
    ),
    SubmissionStatus.SUCCEEDED: frozenset(),
    SubmissionStatus.FAILED: frozenset(),
    SubmissionStatus.INTERRUPTED: frozenset(),
    SubmissionStatus.CANCELLED: frozenset(),
}


class InvalidSubmissionTransition(ValueError):
    """Raised when a turn submission skips its durable lifecycle."""

    def __init__(
        self,
        current: SubmissionStatus,
        target: SubmissionStatus,
    ) -> None:
        self.current = current
        self.target = target
        super().__init__(
            f"invalid submission transition: {current.value} -> "
            f"{target.value}",
        )


def validate_submission_transition(
    current: SubmissionStatus,
    target: SubmissionStatus,
) -> None:
    """Validate one server-owned submission transition."""
    if target not in SUBMISSION_TRANSITIONS[current]:
        raise InvalidSubmissionTransition(current, target)


class SubmissionInputEnvelope(KernelModel):
    """Versioned and durable execution input attached to one submission."""

    kind: NonEmptyStr
    payload: JsonObject


class _ChatIdentity(KernelModel):
    """Canonical ChatSpec identity with legacy input compatibility."""

    chat_id: NonEmptyStr = Field(
        validation_alias=AliasChoices("chat_id", "conversation_id"),
        description="Owning ChatSpec.id",
    )

    @property
    def conversation_id(self) -> str:
        """Return the deprecated Python alias during migration."""
        return self.chat_id


class TurnSubmissionRequest(_ChatIdentity):
    """Validated input accepted before the server assigns queue order."""

    agent_id: NonEmptyStr
    priority: int = Field(default=20, ge=0, le=100)
    content: NonEmptyStr
    artifact_refs: tuple[ArtifactRef, ...] = ()
    request_context: JsonObject = Field(default_factory=dict)
    input_envelope: SubmissionInputEnvelope | None = None
    idempotency_key: NonEmptyStr
    correlation_id: UUID = Field(default_factory=uuid4)


class TurnSubmission(TurnSubmissionRequest):
    """One durable user turn owned and sequenced by the server."""

    submission_id: UUID = Field(default_factory=uuid4)
    sequence: int = Field(ge=1)
    queue_position: int = Field(default=1, ge=1)
    invocation_id: UUID | None = None
    status: SubmissionStatus = SubmissionStatus.QUEUED
    revision: int = Field(default=1, ge=1)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_invocation_identity(self) -> "TurnSubmission":
        """Require an invocation identity after admission starts."""
        requires_invocation = self.status in ACTIVE_SUBMISSION_STATUSES or (
            self.status
            in {
                SubmissionStatus.SUCCEEDED,
                SubmissionStatus.FAILED,
                SubmissionStatus.INTERRUPTED,
            }
        )
        if requires_invocation and self.invocation_id is None:
            raise ValueError(
                "admitted and executed submissions require invocation_id",
            )
        if (
            self.status
            in {
                SubmissionStatus.QUEUED,
                SubmissionStatus.CANCELLED,
            }
            and self.invocation_id is not None
        ):
            raise ValueError(
                "queued or cancelled submissions cannot own invocation_id",
            )
        return self


class ControlCommand(_ChatIdentity):
    """One idempotent request to change server-side runtime state."""

    command_id: UUID = Field(default_factory=uuid4)
    kind: ControlCommandKind
    agent_id: NonEmptyStr
    idempotency_key: NonEmptyStr
    expected_revision: int = Field(ge=0)
    target_submission_id: UUID | None = None
    target_invocation_id: UUID | None = None
    instruction: NonEmptyStr | None = None
    ordered_submission_ids: tuple[UUID, ...] = ()
    requested_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_kind_payload(self) -> "ControlCommand":
        """Reject ambiguous command payloads before they reach an adapter."""
        validators = {
            ControlCommandKind.STEER: self._validate_steer,
            ControlCommandKind.INTERRUPT_CURRENT: self._validate_interrupt,
            ControlCommandKind.CANCEL_QUEUED: self._validate_cancel_queued,
            ControlCommandKind.REORDER: self._validate_reorder,
            ControlCommandKind.STOP_AND_CLEAR: self._validate_stop_and_clear,
        }
        validators[self.kind]()
        return self

    def _validate_steer(self) -> None:
        if self.target_invocation_id is None or self.instruction is None:
            raise ValueError(
                "steer requires target_invocation_id and instruction",
            )
        if self.target_submission_id or self.ordered_submission_ids:
            raise ValueError("steer payload is ambiguous")

    def _validate_interrupt(self) -> None:
        if self.target_invocation_id is None:
            raise ValueError(
                "interrupt_current requires target_invocation_id",
            )
        if (
            self.target_submission_id
            or self.instruction
            or self.ordered_submission_ids
        ):
            raise ValueError("interrupt_current payload is ambiguous")

    def _validate_cancel_queued(self) -> None:
        if self.target_submission_id is None:
            raise ValueError(
                "cancel_queued requires target_submission_id",
            )
        if (
            self.target_invocation_id
            or self.instruction
            or self.ordered_submission_ids
        ):
            raise ValueError("cancel_queued payload is ambiguous")

    def _validate_reorder(self) -> None:
        if not self.ordered_submission_ids:
            raise ValueError("reorder requires ordered_submission_ids")
        if len(set(self.ordered_submission_ids)) != len(
            self.ordered_submission_ids,
        ):
            raise ValueError("reorder submission IDs must be unique")
        if (
            self.target_submission_id
            or self.target_invocation_id
            or self.instruction
        ):
            raise ValueError("reorder payload is ambiguous")

    def _validate_stop_and_clear(self) -> None:
        payload = (
            self.target_submission_id,
            self.instruction,
            self.ordered_submission_ids,
        )
        if any(payload):
            raise ValueError(
                "stop_and_clear only accepts a captured invocation target",
            )


class ControlReceipt(_ChatIdentity):
    """Authoritative acknowledgement for submission or control mutation."""

    receipt_id: UUID = Field(default_factory=uuid4)
    command_id: UUID | None = None
    submission_id: UUID | None = None
    kind: ControlCommandKind | Literal["enqueue"]
    status: ControlCommandStatus
    agent_id: NonEmptyStr
    revision: int = Field(ge=1)
    detail: str = ""
    applied_at_safe_point: SteerSafePoint | None = None
    recorded_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_source_identity(self) -> "ControlReceipt":
        """Require the identity matching the acknowledged operation."""
        if self.kind == "enqueue" and self.submission_id is None:
            raise ValueError("enqueue receipt requires submission_id")
        if self.kind != "enqueue" and self.command_id is None:
            raise ValueError("control receipt requires command_id")
        if self.applied_at_safe_point is not None and (
            self.kind is not ControlCommandKind.STEER
            or self.status is not ControlCommandStatus.APPLIED
        ):
            raise ValueError(
                "only an applied steer may record a safe point",
            )
        return self


class ControlRecord(KernelModel):
    """Queryable control request and its latest authoritative receipt."""

    command: ControlCommand
    receipt: ControlReceipt

    @model_validator(mode="after")
    def validate_identity(self) -> "ControlRecord":
        """Require the command and receipt to describe one mutation."""
        if self.receipt.command_id != self.command.command_id:
            raise ValueError("control receipt command identity mismatch")
        if self.receipt.kind != self.command.kind:
            raise ValueError("control receipt kind mismatch")
        if (
            self.receipt.agent_id != self.command.agent_id
            or self.receipt.chat_id != self.command.chat_id
        ):
            raise ValueError("control receipt owner mismatch")
        return self


class QueueProjection(_ChatIdentity):
    """Consistent server-side view of one conversation queue."""

    agent_id: NonEmptyStr
    revision: int = Field(ge=0)
    active_submission_id: UUID | None = None
    submissions: tuple[TurnSubmission, ...] = ()
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_queue_invariants(self) -> "QueueProjection":
        """Enforce stable ordering and a single active invocation."""
        identities = [item.submission_id for item in self.submissions]
        if len(identities) != len(set(identities)):
            raise ValueError("queue submissions must be unique")
        queued_positions = [
            item.queue_position
            for item in self.submissions
            if item.status is SubmissionStatus.QUEUED
        ]
        if queued_positions != sorted(queued_positions):
            raise ValueError("queued submissions must be position ordered")
        if len(queued_positions) != len(set(queued_positions)):
            raise ValueError("queued submission positions must be unique")
        active = [
            item
            for item in self.submissions
            if item.status in ACTIVE_SUBMISSION_STATUSES
        ]
        if len(active) > 1:
            raise ValueError("conversation queue allows one active invocation")
        expected_active_id = active[0].submission_id if active else None
        if self.active_submission_id != expected_active_id:
            raise ValueError(
                "active_submission_id must match the active submission",
            )
        for item in self.submissions:
            if item.agent_id != self.agent_id:
                raise ValueError("queue submission agent_id mismatch")
            if item.chat_id != self.chat_id:
                raise ValueError("queue submission chat_id mismatch")
        return self


class ConversationExecutionChain(_ChatIdentity):
    """Derived execution state for one correlation-scoped user intent.

    ``inactive`` means no Invocation is currently running. It deliberately
    does not claim that the user's business outcome has been achieved.
    """

    schema_id: Literal["qwenpaw.conversation-execution-chain.v1"] = Field(
        default="qwenpaw.conversation-execution-chain.v1",
        alias="schema",
    )
    correlation_id: UUID
    state: ConversationExecutionState
    submission_ids: tuple[UUID, ...] = Field(min_length=1)
    invocation_ids: tuple[UUID, ...] = ()
    head_submission_id: UUID
    head_invocation_id: UUID | None = None
    latest_submission_status: SubmissionStatus
    open_interaction_ids: tuple[UUID, ...] = ()
    outcome: ConversationOutcome | None = None
    accepted_at: AwareDatetime
    latest_submission_at: AwareDatetime

    @model_validator(mode="after")
    def validate_chain_identity(self) -> "ConversationExecutionChain":
        """Keep the derived causal chain ordered and internally coherent."""
        if len(self.submission_ids) != len(set(self.submission_ids)):
            raise ValueError("execution chain submissions must be unique")
        if len(self.invocation_ids) != len(set(self.invocation_ids)):
            raise ValueError("execution chain invocations must be unique")
        if self.head_submission_id != self.submission_ids[-1]:
            raise ValueError("head submission must be the newest submission")
        if (
            self.head_invocation_id is not None
            and self.head_invocation_id not in self.invocation_ids
        ):
            raise ValueError("head invocation must belong to the chain")
        if len(self.open_interaction_ids) != len(
            set(self.open_interaction_ids),
        ):
            raise ValueError("execution chain interactions must be unique")
        waiting = self.state is ConversationExecutionState.WAITING_USER
        if waiting != bool(self.open_interaction_ids):
            raise ValueError(
                "waiting_user requires at least one open interaction",
            )
        outcome_states = {
            outcome_execution_state(status)
            for status in ConversationOutcomeStatus
        }
        if self.outcome is None and self.state in outcome_states:
            raise ValueError("outcome state requires an explicit outcome")
        if self.outcome is not None:
            if (
                self.outcome.chat_id != self.chat_id
                or self.outcome.correlation_id != self.correlation_id
            ):
                raise ValueError("execution chain outcome ownership mismatch")
            if self.state is not outcome_execution_state(
                self.outcome.status,
            ):
                raise ValueError("execution chain outcome state mismatch")
        if self.latest_submission_at < self.accepted_at:
            raise ValueError(
                "execution chain submission time precedes accepted_at",
            )
        return self


class ConversationRuntimeProjection(_ChatIdentity):
    """Recoverable current-state projection for one conversation runtime."""

    agent_id: NonEmptyStr
    queue: QueueProjection
    interactions: tuple[InteractionRequest, ...] = ()
    execution_chains: tuple[ConversationExecutionChain, ...] = ()
    execution_window_truncated: bool = False
    activity: ObservationPage = Field(default_factory=ObservationPage)
    communication_contract: CommunicationContract
    cursor: NonEmptyStr
    observed_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_ownership(self) -> "ConversationRuntimeProjection":
        """Reject mixed conversation or agent state in one projection."""
        if self.queue.agent_id != self.agent_id:
            raise ValueError("runtime projection queue agent_id mismatch")
        if self.queue.chat_id != self.chat_id:
            raise ValueError(
                "runtime projection queue chat_id mismatch",
            )
        for interaction in self.interactions:
            if interaction.agent_id != self.agent_id:
                raise ValueError(
                    "runtime projection interaction agent_id mismatch",
                )
            if interaction.chat_id != self.chat_id:
                raise ValueError(
                    "runtime projection interaction chat_id mismatch",
                )
        for chain in self.execution_chains:
            if chain.chat_id != self.chat_id:
                raise ValueError(
                    "runtime projection execution chain owner mismatch",
                )
            if (
                chain.outcome is not None
                and chain.outcome.agent_id != self.agent_id
            ):
                raise ValueError(
                    "runtime projection outcome agent owner mismatch",
                )
        correlations = [item.correlation_id for item in self.execution_chains]
        if len(correlations) != len(set(correlations)):
            raise ValueError(
                "runtime projection execution chains must be unique",
            )
        for observation in self.activity.items:
            if observation.chat_id != self.chat_id:
                raise ValueError(
                    "runtime projection activity chat_id mismatch",
                )
        return self


__all__ = [
    "ACTIVE_SUBMISSION_STATUSES",
    "SUBMISSION_TRANSITIONS",
    "TERMINAL_SUBMISSION_STATUSES",
    "ControlCommand",
    "ControlCommandKind",
    "ControlCommandStatus",
    "ControlRecord",
    "ControlReceipt",
    "ConversationExecutionChain",
    "ConversationExecutionState",
    "ConversationRuntimeProjection",
    "InvalidSubmissionTransition",
    "QueueProjection",
    "SubmissionStatus",
    "SubmissionInputEnvelope",
    "SteerSafePoint",
    "TurnSubmission",
    "TurnSubmissionRequest",
    "outcome_execution_state",
    "validate_submission_transition",
]
