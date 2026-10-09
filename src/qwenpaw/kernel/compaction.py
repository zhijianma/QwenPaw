# -*- coding: utf-8 -*-
"""Stable records for observable context compaction outcomes."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Self
from uuid import UUID, uuid4

from pydantic import AliasChoices, AwareDatetime, Field, model_validator

from .models import KernelModel, NamespacedId, NonEmptyStr, utc_now


class CompactionTrigger(str, Enum):
    """Reason one context compaction attempt ran."""

    AUTOMATIC = "automatic"
    MANUAL = "manual"
    OVERFLOW_RECOVERY = "overflow_recovery"


class CompactionStatus(str, Enum):
    """Terminal status of one material compaction attempt."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


class CompactionRecord(KernelModel):
    """Content-free evidence for one material compaction attempt."""

    compaction_id: UUID = Field(default_factory=uuid4)
    agent_id: NonEmptyStr
    chat_id: NonEmptyStr = Field(
        validation_alias=AliasChoices("chat_id", "conversation_id"),
        description="Owning ChatSpec.id",
    )
    invocation_id: UUID
    correlation_id: UUID | None = None
    registry_generation: int = Field(ge=1)
    strategy_id: NamespacedId
    trigger: CompactionTrigger
    status: CompactionStatus
    before_message_count: int = Field(ge=0)
    after_message_count: int = Field(ge=0)
    evicted_messages: int = Field(default=0, ge=0)
    folded_items: int = Field(default=0, ge=0)
    context_changed: bool = False
    summary_changed: bool = False
    error_code: NonEmptyStr | None = None
    started_at: AwareDatetime = Field(default_factory=utc_now)
    completed_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="before")
    @classmethod
    def validate_chat_identity(cls, value: object) -> object:
        """Reject ambiguous canonical and legacy Chat identities."""
        if isinstance(value, Mapping):
            chat_id = value.get("chat_id")
            conversation_id = value.get("conversation_id")
            if (
                chat_id is not None
                and conversation_id is not None
                and chat_id != conversation_id
            ):
                raise ValueError(
                    "chat_id and conversation_id must identify one Chat",
                )
        return value

    @property
    def conversation_id(self) -> str:
        """Return the deprecated Python alias during migration."""
        return self.chat_id

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        """Reject no-op successes and ambiguous failure evidence."""
        if self.completed_at < self.started_at:
            raise ValueError("compaction completed before it started")
        changed = bool(
            self.before_message_count != self.after_message_count
            or self.evicted_messages
            or self.folded_items
            or self.context_changed
            or self.summary_changed,
        )
        if self.status is CompactionStatus.SUCCEEDED and not changed:
            raise ValueError("successful compaction must change context")
        if self.status is CompactionStatus.SUCCEEDED and self.error_code:
            raise ValueError("successful compaction cannot have error_code")
        if self.status is CompactionStatus.FAILED and not self.error_code:
            raise ValueError("failed compaction requires error_code")
        return self


__all__ = [
    "CompactionRecord",
    "CompactionStatus",
    "CompactionTrigger",
]
