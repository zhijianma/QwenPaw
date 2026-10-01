# -*- coding: utf-8 -*-
"""Channel-neutral contracts for runtime-to-user interactions."""

from __future__ import annotations

from enum import Enum
from typing import Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, model_validator

from .models import (
    ActorRef,
    JsonObject,
    KernelModel,
    NonEmptyStr,
    utc_now,
)


class InteractionKind(str, Enum):
    """Semantic interaction types emitted by a live runtime."""

    APPROVAL = "approval"
    USER_INPUT = "user_input"
    SUGGESTION = "suggestion"


class InteractionStatus(str, Enum):
    """Durable lifecycle of a runtime interaction."""

    OPEN = "open"
    RESOLVED = "resolved"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class InteractionMode(str, Enum):
    """Whether execution waits for a response to the interaction."""

    BLOCKING = "blocking"
    NON_BLOCKING = "non_blocking"


class InteractionOption(KernelModel):
    """One stable response option rendered by any delivery adapter."""

    option_id: NonEmptyStr
    label: NonEmptyStr
    description: str = ""
    value: JsonObject = Field(default_factory=dict)


class InteractionRequest(KernelModel):
    """Durable, channel-neutral request presented to a user.

    Policy-specific records such as ``ApprovalRequest`` remain the source of
    truth. ``source_id`` links this delivery record to that policy fact.
    """

    interaction_id: UUID = Field(default_factory=uuid4)
    kind: InteractionKind
    mode: InteractionMode
    agent_id: NonEmptyStr
    conversation_id: NonEmptyStr
    invocation_id: UUID
    correlation_id: UUID = Field(default_factory=uuid4)
    source_id: UUID | None = None
    title: NonEmptyStr
    prompt: NonEmptyStr
    options: tuple[InteractionOption, ...] = ()
    response_schema: JsonObject = Field(default_factory=dict)
    metadata: JsonObject = Field(default_factory=dict)
    status: InteractionStatus = InteractionStatus.OPEN
    revision: int = Field(default=1, ge=1)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_kind_semantics(self) -> Self:
        """Keep blocking behavior stable across delivery adapters."""
        if (
            self.kind is InteractionKind.SUGGESTION
            and self.mode is not InteractionMode.NON_BLOCKING
        ):
            raise ValueError("suggestion interactions must be non-blocking")
        if (
            self.kind is InteractionKind.APPROVAL
            and self.mode is not InteractionMode.BLOCKING
        ):
            raise ValueError("approval interactions must be blocking")
        option_ids = [option.option_id for option in self.options]
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("interaction option IDs must be unique")
        if self.expires_at is not None and self.expires_at < self.created_at:
            raise ValueError("expires_at cannot precede created_at")
        return self


class InteractionResponse(KernelModel):
    """One idempotent user response submitted through any channel."""

    interaction_id: UUID
    idempotency_key: NonEmptyStr
    expected_revision: int = Field(ge=1)
    actor: ActorRef
    selected_option_ids: tuple[NonEmptyStr, ...] = ()
    text: str = ""
    values: JsonObject = Field(default_factory=dict)
    responded_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_answer(self) -> Self:
        """Require an actual answer rather than an empty transport event."""
        if not (self.selected_option_ids or self.text or self.values):
            raise ValueError("interaction response cannot be empty")
        if len(self.selected_option_ids) != len(
            set(self.selected_option_ids),
        ):
            raise ValueError("selected interaction options must be unique")
        return self


class InteractionResolution(KernelModel):
    """Authoritative terminal receipt after response validation."""

    interaction_id: UUID
    status: InteractionStatus
    revision: int = Field(ge=2)
    response: InteractionResponse | None = None
    detail: str = ""
    resolved_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_terminal_state(self) -> Self:
        """Only a resolved interaction may carry a user response."""
        if self.status is InteractionStatus.OPEN:
            raise ValueError("interaction resolution must be terminal")
        if self.status is InteractionStatus.RESOLVED and self.response is None:
            raise ValueError("resolved interaction requires a response")
        if (
            self.status is not InteractionStatus.RESOLVED
            and self.response is not None
        ):
            raise ValueError(
                "expired or cancelled interaction cannot carry a response",
            )
        return self


class InteractionRecord(KernelModel):
    """Queryable request and optional authoritative terminal resolution."""

    request: InteractionRequest
    resolution: InteractionResolution | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        """Require request and resolution to identify one interaction."""
        if (
            self.resolution is not None
            and self.resolution.interaction_id != self.request.interaction_id
        ):
            raise ValueError("interaction resolution identity mismatch")
        return self


__all__ = [
    "InteractionKind",
    "InteractionMode",
    "InteractionOption",
    "InteractionRecord",
    "InteractionRequest",
    "InteractionResolution",
    "InteractionResponse",
    "InteractionStatus",
]
