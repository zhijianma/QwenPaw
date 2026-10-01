# -*- coding: utf-8 -*-
"""Versioned execution event envelope and payload safety rules."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import (
    AwareDatetime,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
)

from .models import (
    ActorRef,
    ApprovalDecision,
    ApprovalRequest,
    ArtifactRef,
    CausalIdentity,
    ExecutionCheckpoint,
    EvidenceRef,
    IdempotencyRecord,
    JsonObject,
    KernelModel,
    Plan,
    Run,
    SideEffectRecord,
    Task,
    utc_now,
)

MAX_INLINE_EVENT_PAYLOAD_BYTES = 32 * 1024

_FORBIDDEN_PAYLOAD_FIELDS = frozenset(
    {
        "chain_of_thought",
        "hidden_reasoning",
        "raw_prompt",
        "raw_reasoning",
        "reasoning_content",
    },
)

EventType = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=(r"^[a-z][a-z0-9_-]*" r"(?:\.[a-z][a-z0-9_-]*)+$"),
    ),
]


def assert_payload_safe(value: JsonValue, path: str = "payload") -> None:
    """Reject fields that could persist hidden model reasoning."""
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = key.strip().lower().replace("-", "_")
            if normalized in _FORBIDDEN_PAYLOAD_FIELDS:
                raise ValueError(f"forbidden field at {path}.{key}")
            assert_payload_safe(nested, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, nested in enumerate(value):
            assert_payload_safe(nested, f"{path}[{index}]")


class ExecutionEvent(CausalIdentity):
    """Canonical append-only event persisted by the execution ledger."""

    schema_id: Literal["qwenpaw.execution-event.v1"] = Field(
        default="qwenpaw.execution-event.v1",
        alias="schema",
    )
    event_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    run_id: UUID | None = None
    sequence: int = Field(ge=1)
    event_type: EventType
    occurred_at: AwareDatetime = Field(default_factory=utc_now)
    registry_generation: int = Field(ge=1)
    actor: ActorRef
    payload: JsonObject = Field(default_factory=dict)
    artifact_refs: tuple[ArtifactRef, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()

    @field_validator("payload")
    @classmethod
    def validate_payload(cls, payload: JsonObject) -> JsonObject:
        """Enforce privacy and bounded-inline-payload invariants."""
        assert_payload_safe(payload)
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > MAX_INLINE_EVENT_PAYLOAD_BYTES:
            raise ValueError(
                "event payload exceeds the inline limit; store it as an "
                "ArtifactRef",
            )
        return payload


@dataclass(frozen=True, slots=True)
class TaskProjectionSnapshot:
    """One transactionally consistent Task Workbench read snapshot."""

    task: Task | None
    runs: tuple[Run, ...]
    latest_plan: Plan | None
    approvals: tuple[
        tuple[ApprovalRequest, ApprovalDecision | None],
        ...,
    ]
    events: tuple[ExecutionEvent, ...]
    checkpoint: ExecutionCheckpoint | None
    last_sequence: int


class ExecutionCommit(KernelModel):
    """Atomic persistence unit for projections and their source events."""

    events: tuple[ExecutionEvent, ...] = Field(min_length=1)
    task: Task | None = None
    expected_task_version: int | None = Field(default=None, ge=1)
    create_task: bool = False
    plan: Plan | None = None
    run: Run | None = None
    create_run: bool = False
    approval_request: ApprovalRequest | None = None
    approval_decision: ApprovalDecision | None = None
    additional_approval_decisions: tuple[ApprovalDecision, ...] = ()
    checkpoint: ExecutionCheckpoint | None = None
    idempotency: IdempotencyRecord | None = None
    side_effect_record: SideEffectRecord | None = None

    @field_validator("events")
    @classmethod
    def validate_events(
        cls,
        events: tuple[ExecutionEvent, ...],
    ) -> tuple[ExecutionEvent, ...]:
        """Require one task and contiguous sequences inside a commit."""
        task_ids = {event.task_id for event in events}
        if len(task_ids) != 1:
            raise ValueError("commit events must belong to one task")
        sequences = [event.sequence for event in events]
        expected = list(range(sequences[0], sequences[0] + len(events)))
        if sequences != expected:
            raise ValueError("commit event sequences must be contiguous")
        return events

    @field_validator("expected_task_version")
    @classmethod
    def validate_expected_version(
        cls,
        expected: int | None,
    ) -> int | None:
        """Keep the validator explicit for generated schema documentation."""
        return expected

    # Pydantic's runtime signature includes this context despite its stub.
    # pylint: disable-next=arguments-differ
    def model_post_init(self, __context: object) -> None:
        """Validate aggregate IDs and create/update flags."""
        task_id = self.events[0].task_id
        related = (
            self.task,
            self.plan,
            self.run,
            self.approval_request,
            self.checkpoint,
            self.side_effect_record,
        )
        for model in related:
            if model is not None and model.task_id != task_id:
                raise ValueError("commit projections must match event task_id")
        if self.create_task and self.task is None:
            raise ValueError("create_task requires a task projection")
        if self.create_task and self.expected_task_version is not None:
            raise ValueError("create_task cannot set expected_task_version")
        if (
            self.task is not None
            and not self.create_task
            and self.expected_task_version is None
        ):
            raise ValueError("task update requires expected_task_version")
        if self.create_run and self.run is None:
            raise ValueError("create_run requires a run projection")
        decisions = (
            *(
                (self.approval_decision,)
                if self.approval_decision is not None
                else ()
            ),
            *self.additional_approval_decisions,
        )
        decision_ids = [decision.approval_id for decision in decisions]
        if len(decision_ids) != len(set(decision_ids)):
            raise ValueError("approval decisions must be unique per commit")
