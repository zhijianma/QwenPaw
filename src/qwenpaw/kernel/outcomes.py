# -*- coding: utf-8 -*-
"""Explicit business outcomes for long-running conversation intents."""

from __future__ import annotations

from enum import Enum
from typing import Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, model_validator

from .models import (
    CapabilityProviderKind,
    KernelModel,
    NamespacedId,
    NonEmptyStr,
    utc_now,
)


class ConversationOutcomeStatus(str, Enum):
    """Business disposition declared for one correlation-scoped intent."""

    ACHIEVED = "achieved"
    PARTIAL = "partial"
    NOT_ACHIEVED = "not_achieved"
    ABANDONED = "abandoned"


class OutcomeProducerRegistration(KernelModel):
    """Host-approved identity allowed to request Conversation outcomes."""

    producer_id: NamespacedId
    provider_kind: CapabilityProviderKind
    allow_task_outcomes: bool = False


class ConversationOutcomeDeclaration(KernelModel):
    """Untrusted producer request admitted and materialized by the Host."""

    outcome_id: UUID = Field(default_factory=uuid4)
    agent_id: NonEmptyStr
    conversation_id: NonEmptyStr
    correlation_id: UUID
    status: ConversationOutcomeStatus
    producer_id: NamespacedId
    summary: str = Field(min_length=1, max_length=2000)
    invocation_id: UUID | None = None
    task_id: UUID | None = None
    run_id: UUID | None = None
    artifact_ids: tuple[UUID, ...] = ()
    evidence_ids: tuple[UUID, ...] = ()
    verification_ids: tuple[UUID, ...] = ()
    supersedes_outcome_id: UUID | None = None
    declared_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_declaration(self) -> Self:
        """Reject ambiguous ownership and duplicate reference claims."""
        if (self.task_id is None) != (self.run_id is None):
            raise ValueError("outcome task_id and run_id must be paired")
        if self.supersedes_outcome_id == self.outcome_id:
            raise ValueError("outcome cannot supersede itself")
        for label, identifiers in (
            ("artifact", self.artifact_ids),
            ("evidence", self.evidence_ids),
            ("verification", self.verification_ids),
        ):
            if len(identifiers) != len(set(identifiers)):
                raise ValueError(f"outcome {label} IDs must be unique")
        return self


class ConversationOutcome(KernelModel):
    """Immutable, explicit outcome independent from Invocation completion."""

    outcome_id: UUID = Field(default_factory=uuid4)
    agent_id: NonEmptyStr
    conversation_id: NonEmptyStr
    correlation_id: UUID
    status: ConversationOutcomeStatus
    producer_id: NamespacedId
    summary: str = Field(min_length=1, max_length=2000)
    invocation_id: UUID | None = None
    task_id: UUID | None = None
    run_id: UUID | None = None
    artifact_ids: tuple[UUID, ...] = ()
    evidence_ids: tuple[UUID, ...] = ()
    verification_ids: tuple[UUID, ...] = ()
    supersedes_outcome_id: UUID | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        """Keep causal ownership and referenced evidence unambiguous."""
        if (self.task_id is None) != (self.run_id is None):
            raise ValueError("outcome task_id and run_id must be paired")
        if self.supersedes_outcome_id == self.outcome_id:
            raise ValueError("outcome cannot supersede itself")
        for label, identifiers in (
            ("artifact", self.artifact_ids),
            ("evidence", self.evidence_ids),
            ("verification", self.verification_ids),
        ):
            if len(identifiers) != len(set(identifiers)):
                raise ValueError(f"outcome {label} IDs must be unique")
        return self


__all__ = [
    "ConversationOutcome",
    "ConversationOutcomeDeclaration",
    "ConversationOutcomeStatus",
    "OutcomeProducerRegistration",
]
