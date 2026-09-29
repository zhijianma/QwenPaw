# -*- coding: utf-8 -*-
"""Durable non-Task facts emitted by host and plugin services."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import AwareDatetime, Field, model_validator

from .models import (
    JsonObject,
    KernelModel,
    NamespacedId,
    NonEmptyStr,
    utc_now,
)


class OperationalStatus(str, Enum):
    """Outcome reported by one operational source fact."""

    SUCCESS = "success"
    ERROR = "error"


class OperationalSeverity(str, Enum):
    """Presentation-neutral importance assigned by the producer."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class OperationalEventConflictError(RuntimeError):
    """Raised when one idempotency identity is reused for another fact."""


class OperationalEvent(KernelModel):
    """Immutable fact for work that is not a Task or Conversation turn."""

    event_id: UUID | None = None
    agent_id: NonEmptyStr
    producer_id: NamespacedId
    event_type: NamespacedId
    idempotency_key: NonEmptyStr = Field(max_length=200)
    source_type: NonEmptyStr = Field(max_length=100)
    source_id: str = Field(default="", max_length=500)
    status: OperationalStatus
    source_status: NonEmptyStr | None = Field(default=None, max_length=100)
    severity: OperationalSeverity = OperationalSeverity.INFO
    title: NonEmptyStr = Field(max_length=500)
    body: str = Field(default="", max_length=20_000)
    payload: JsonObject = Field(default_factory=dict)
    registry_generation: int = Field(default=1, ge=1)
    occurred_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="before")
    @classmethod
    def derive_stable_event_id(cls, value: Any) -> Any:
        """Derive identity from producer ownership and idempotency facts."""
        if not isinstance(value, Mapping) or value.get("event_id"):
            return value
        identity_values = (
            value.get("agent_id"),
            value.get("producer_id"),
            value.get("idempotency_key"),
        )
        if not all(
            isinstance(item, str) and item.strip() for item in identity_values
        ):
            return value
        values = dict(value)
        identity = ":".join(str(item) for item in identity_values)
        values["event_id"] = uuid5(
            NAMESPACE_URL,
            f"qwenpaw:operational-event:{identity}",
        )
        return values

    @model_validator(mode="after")
    def validate_event_id(self) -> "OperationalEvent":
        """Require the deterministic source identity after validation."""
        if self.event_id is None:
            raise ValueError("operational event_id could not be derived")
        return self

    def same_fact(self, other: "OperationalEvent") -> bool:
        """Compare immutable content while ignoring observation time."""
        return self.model_copy(update={"occurred_at": other.occurred_at}) == (
            other
        )


__all__ = [
    "OperationalEvent",
    "OperationalEventConflictError",
    "OperationalSeverity",
    "OperationalStatus",
]
