# -*- coding: utf-8 -*-
"""Presentation-neutral semantic observations derived from runtime facts."""

from __future__ import annotations

import json
from enum import Enum
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from .events import assert_payload_safe
from .models import JsonObject, KernelModel, NamespacedId, NonEmptyStr

MAX_OBSERVATION_FACTS_BYTES = 16 * 1024


class ObservationCategory(str, Enum):
    """Stable semantic families shared by every runtime implementation."""

    MODEL = "model"
    ACTION = "action"
    CONTROL = "control"
    GUARDRAIL = "guardrail"
    COMPACTION = "compaction"
    HITL = "hitl"
    INTERRUPT = "interrupt"
    VERIFICATION = "verification"


class ObservationStage(str, Enum):
    """Responsibility boundary represented by one source fact."""

    INTENT = "intent"
    POLICY = "policy"
    EXECUTION = "execution"
    EVIDENCE = "evidence"


class ObservationStatus(str, Enum):
    """Presentation-neutral lifecycle state of one observation."""

    RECORDED = "recorded"
    STARTED = "started"
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"
    DENIED = "denied"
    ACCEPTED = "accepted"
    APPLIED = "applied"
    REJECTED = "rejected"
    CONFLICT = "conflict"
    RESOLVED = "resolved"
    EXPIRED = "expired"
    BLOCKED = "blocked"


class ObservationSource(KernelModel):
    """Immutable pointer to the authoritative fact being projected."""

    source_type: NamespacedId
    source_id: NonEmptyStr


class RuntimeObservation(KernelModel):
    """Content-safe semantic projection over an authoritative runtime fact."""

    schema_id: Literal["qwenpaw.runtime-observation.v1"] = Field(
        default="qwenpaw.runtime-observation.v1",
        alias="schema",
    )
    observation_id: UUID
    category: ObservationCategory
    stage: ObservationStage
    status: ObservationStatus
    source: ObservationSource
    task_id: UUID | None = None
    run_id: UUID | None = None
    conversation_id: NonEmptyStr | None = None
    invocation_id: UUID | None = None
    correlation_id: UUID | None = None
    registry_generation: int | None = Field(default=None, ge=1)
    title: NonEmptyStr
    facts: JsonObject = Field(default_factory=dict)
    occurred_at: AwareDatetime

    @model_validator(mode="after")
    def validate_owner(self) -> Self:
        """Require a Task or Conversation ownership boundary."""
        if self.task_id is None and self.conversation_id is None:
            raise ValueError("observation requires a task or conversation")
        if self.run_id is not None and self.task_id is None:
            raise ValueError("run-scoped observation requires a task")
        return self

    @field_validator("facts")
    @classmethod
    def validate_facts(cls, facts: JsonObject) -> JsonObject:
        """Keep derived facts bounded and free of hidden reasoning."""
        assert_payload_safe(facts, "facts")
        encoded = json.dumps(
            facts,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > MAX_OBSERVATION_FACTS_BYTES:
            raise ValueError("observation facts exceed the inline limit")
        return facts


class ObservationPage(KernelModel):
    """Stable cursor page over a fixed semantic-observation snapshot."""

    schema_id: Literal["qwenpaw.observation-page.v1"] = Field(
        default="qwenpaw.observation-page.v1",
        alias="schema",
    )
    items: tuple[RuntimeObservation, ...] = ()
    next_cursor: NonEmptyStr | None = None


__all__ = [
    "MAX_OBSERVATION_FACTS_BYTES",
    "ObservationCategory",
    "ObservationPage",
    "ObservationSource",
    "ObservationStage",
    "ObservationStatus",
    "RuntimeObservation",
]
