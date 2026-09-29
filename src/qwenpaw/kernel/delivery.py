# -*- coding: utf-8 -*-
"""Stable delivery contracts that never own source domain facts."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any, Self
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import AwareDatetime, Field, model_validator

from .models import (
    ArtifactRef,
    EvidenceRef,
    JsonObject,
    KernelModel,
    NamespacedId,
    NonEmptyStr,
    utc_now,
)


class DeliveryKind(str, Enum):
    """Stable fact categories eligible for external projection."""

    REPLY = "reply"
    RESULT = "result"
    APPROVAL = "approval"
    ACTIVITY = "activity"
    EXCEPTION = "exception"
    ARTIFACT_READY = "artifact_ready"


class DeliveryMode(str, Enum):
    """How an adapter should expose one delivery projection."""

    STREAM = "stream"
    FINAL = "final"
    SILENT = "silent"


class DeliveryStatus(str, Enum):
    """Terminal outcome of one delivery attempt."""

    DELIVERED = "delivered"
    FAILED = "failed"
    SUPPRESSED = "suppressed"
    UNCERTAIN = "uncertain"


class DeliveryAttemptStatus(str, Enum):
    """Lease lifecycle for one explicit delivery attempt."""

    CLAIMED = "claimed"
    DELIVERED = "delivered"
    FAILED = "failed"
    SUPPRESSED = "suppressed"
    UNCERTAIN = "uncertain"


class DeliveryRequestConflictError(RuntimeError):
    """Raised when one stable identity is reused for another projection."""


class DeliveryAttemptConflictError(RuntimeError):
    """Raised when attempt ownership, order, or revision is invalid."""


class DeliveryAttemptNotFoundError(LookupError):
    """Raised when an attempt mutation targets an unknown record."""


class DeliveryDestination(KernelModel):
    """Adapter-owned address without leaking transport session identity."""

    adapter_id: NamespacedId
    address: NonEmptyStr
    conversation_id: NonEmptyStr | None = None
    metadata: JsonObject = Field(default_factory=dict)


class DeliveryPolicy(KernelModel):
    """Projection policy selected independently from source domain state."""

    destination: DeliveryDestination
    mode: DeliveryMode = DeliveryMode.FINAL
    kinds: tuple[DeliveryKind, ...] = (
        DeliveryKind.REPLY,
        DeliveryKind.RESULT,
        DeliveryKind.APPROVAL,
        DeliveryKind.ACTIVITY,
        DeliveryKind.EXCEPTION,
        DeliveryKind.ARTIFACT_READY,
    )
    suppress_empty_text: bool = False
    suppress_exact_text: tuple[NonEmptyStr, ...] = ()

    def suppresses_text(self, text: str) -> bool:
        """Return whether one completed result is intentionally quiet."""
        normalized = text.strip()
        if not normalized and self.suppress_empty_text:
            return True
        return any(
            normalized == candidate.strip()
            for candidate in self.suppress_exact_text
        )


class DeliveryRequest(KernelModel):
    """Immutable projection request derived from a committed source fact."""

    delivery_id: UUID | None = None
    source_event_id: UUID
    idempotency_key: NonEmptyStr = Field(max_length=200)
    agent_id: NonEmptyStr
    registry_generation: int = Field(default=1, ge=1)
    kind: DeliveryKind
    mode: DeliveryMode
    destination: DeliveryDestination
    conversation_id: NonEmptyStr | None = None
    task_id: UUID | None = None
    run_id: UUID | None = None
    invocation_id: UUID | None = None
    correlation_id: UUID | None = None
    artifact_refs: tuple[ArtifactRef, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()
    payload: JsonObject = Field(default_factory=dict)
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="before")
    @classmethod
    def derive_stable_delivery_id(cls, value: Any) -> Any:
        """Derive identity from stable routing and idempotency facts."""
        if not isinstance(value, Mapping) or value.get("delivery_id"):
            return value
        destination = value.get("destination")
        if isinstance(destination, DeliveryDestination):
            adapter_id = destination.adapter_id
            address = destination.address
        elif isinstance(destination, Mapping):
            adapter_id = destination.get("adapter_id")
            address = destination.get("address")
        else:
            return value
        agent_id = value.get("agent_id")
        idempotency_key = value.get("idempotency_key")
        identity_values = (
            agent_id,
            adapter_id,
            address,
            idempotency_key,
        )
        if not all(
            isinstance(item, str) and item.strip() for item in identity_values
        ):
            return value
        values = dict(value)
        identity = ":".join(str(item) for item in identity_values)
        values["delivery_id"] = uuid5(
            NAMESPACE_URL,
            f"qwenpaw:delivery:{identity}",
        )
        return values

    @model_validator(mode="after")
    def validate_source_identity(self) -> Self:
        """Reject a Run reference detached from its owning Task."""
        if self.delivery_id is None:
            raise ValueError("delivery_id could not be derived")
        if self.run_id is not None and self.task_id is None:
            raise ValueError("delivery run_id requires task_id")
        if (
            self.conversation_id is not None
            and self.destination.conversation_id is not None
            and self.conversation_id != self.destination.conversation_id
        ):
            raise ValueError(
                "delivery destination conversation does not match source",
            )
        return self

    def same_projection(self, other: "DeliveryRequest") -> bool:
        """Compare logical facts while ignoring observation time."""
        return (
            self.model_copy(update={"created_at": other.created_at}) == other
        )


class DeliveryReceipt(KernelModel):
    """Terminal adapter receipt; it never mutates the source event."""

    delivery_id: UUID
    adapter_id: NamespacedId
    status: DeliveryStatus
    attempt: int = Field(default=1, ge=1)
    finished_at: AwareDatetime = Field(default_factory=utc_now)
    error_code: str = Field(default="", max_length=100)
    metadata: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_terminal_result(self) -> Self:
        """Keep failure diagnostics exclusive to failed attempts."""
        if self.status in {
            DeliveryStatus.FAILED,
            DeliveryStatus.UNCERTAIN,
        }:
            if not self.error_code.strip():
                raise ValueError(
                    "failed or uncertain delivery requires error_code",
                )
        elif self.error_code:
            raise ValueError(
                "successful or suppressed delivery cannot contain an error",
            )
        return self


class DeliveryAttempt(KernelModel):
    """Revisioned ownership proof for one explicit delivery attempt."""

    delivery_id: UUID
    attempt: int = Field(ge=1)
    owner_id: NonEmptyStr
    status: DeliveryAttemptStatus = DeliveryAttemptStatus.CLAIMED
    revision: int = Field(default=1, ge=1)
    acquired_at: AwareDatetime = Field(default_factory=utc_now)
    expires_at: AwareDatetime
    receipt: DeliveryReceipt | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        """Keep claim expiry and terminal receipt in sync."""
        if self.expires_at <= self.acquired_at:
            raise ValueError("delivery lease must expire after acquisition")
        terminal = self.status is not DeliveryAttemptStatus.CLAIMED
        if terminal != (self.receipt is not None):
            raise ValueError("terminal delivery attempt requires receipt")
        if self.receipt is not None:
            expected_status = DeliveryAttemptStatus(self.receipt.status.value)
            if self.receipt.delivery_id != self.delivery_id:
                raise ValueError("delivery receipt identity does not match")
            if self.receipt.attempt != self.attempt:
                raise ValueError("delivery receipt attempt does not match")
            if expected_status is not self.status:
                raise ValueError("delivery receipt status does not match")
        return self


__all__ = [
    "DeliveryAttempt",
    "DeliveryAttemptConflictError",
    "DeliveryAttemptNotFoundError",
    "DeliveryAttemptStatus",
    "DeliveryDestination",
    "DeliveryKind",
    "DeliveryMode",
    "DeliveryPolicy",
    "DeliveryReceipt",
    "DeliveryRequest",
    "DeliveryRequestConflictError",
    "DeliveryStatus",
]
