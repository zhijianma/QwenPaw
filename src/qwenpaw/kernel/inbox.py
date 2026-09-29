# -*- coding: utf-8 -*-
"""Inbox read-model contracts derived from durable Delivery facts."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import AwareDatetime, Field

from .delivery import DeliveryKind, DeliveryStatus
from .models import (
    ArtifactRef,
    EvidenceRef,
    JsonObject,
    KernelModel,
    NonEmptyStr,
    utc_now,
)


class InboxProjectionConflictError(RuntimeError):
    """Raised when source identity or optimistic revision conflicts."""


class InboxItemNotFoundError(LookupError):
    """Raised when a projection-local mutation targets no item."""


class InboxItem(KernelModel):
    """Delivery-backed notification plus projection-local read state."""

    item_id: UUID
    delivery_id: UUID
    source_event_id: UUID
    agent_id: NonEmptyStr
    source_type: NonEmptyStr = "task"
    source_id: str = Field(default="", max_length=500)
    event_type: NonEmptyStr = "result"
    source_status: NonEmptyStr = "success"
    severity: NonEmptyStr = "info"
    kind: DeliveryKind
    delivery_status: DeliveryStatus
    delivery_attempt: int = Field(ge=1)
    conversation_id: NonEmptyStr | None = None
    task_id: UUID | None = None
    run_id: UUID | None = None
    invocation_id: UUID | None = None
    correlation_id: UUID | None = None
    artifact_refs: tuple[ArtifactRef, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()
    source_payload: JsonObject = Field(default_factory=dict)
    title: NonEmptyStr
    summary: str = Field(default="", max_length=2_000)
    revision: int = Field(default=1, ge=1)
    read_at: AwareDatetime | None = None
    handled_at: AwareDatetime | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @property
    def read(self) -> bool:
        """Return projection-local read state."""
        return self.read_at is not None

    @property
    def handled(self) -> bool:
        """Return projection-local handled state."""
        return self.handled_at is not None


def inbox_item_timestamp(value: datetime | None = None) -> datetime:
    """Expose one timezone-aware timestamp factory to Store adapters."""
    return value or utc_now()


__all__ = [
    "InboxItem",
    "InboxItemNotFoundError",
    "InboxProjectionConflictError",
    "inbox_item_timestamp",
]
