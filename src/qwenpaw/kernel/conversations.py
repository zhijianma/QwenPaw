# -*- coding: utf-8 -*-
"""Stable Conversation branching contracts shared by every edition."""

from __future__ import annotations

from enum import Enum

from pydantic import AwareDatetime, Field

from .models import KernelModel, NonEmptyStr, utc_now


class ConversationForkBoundary(str, Enum):
    """Stable message boundary selected for a Conversation fork."""

    AFTER_RESPONSE = "after_response"


class ConversationForkCommand(KernelModel):
    """Idempotent request to branch one completed Conversation turn."""

    agent_id: NonEmptyStr
    parent_conversation_id: NonEmptyStr
    source_message_id: NonEmptyStr
    idempotency_key: NonEmptyStr = Field(max_length=200)
    boundary: ConversationForkBoundary = (
        ConversationForkBoundary.AFTER_RESPONSE
    )
    name: NonEmptyStr | None = Field(default=None, max_length=200)


class ConversationForkOrigin(KernelModel):
    """Immutable lineage attached to a forked Conversation."""

    agent_id: NonEmptyStr
    parent_conversation_id: NonEmptyStr
    root_conversation_id: NonEmptyStr
    source_message_id: NonEmptyStr
    boundary: ConversationForkBoundary = (
        ConversationForkBoundary.AFTER_RESPONSE
    )
    depth: int = Field(ge=1)
    created_at: AwareDatetime = Field(default_factory=utc_now)


class ConversationForkResult(KernelModel):
    """Authoritative child identity and immutable branch origin."""

    child_conversation_id: NonEmptyStr
    origin: ConversationForkOrigin


class ConversationForkConflictError(RuntimeError):
    """Raised when an idempotency key or destination conflicts."""


class ConversationForkInvalidAnchorError(RuntimeError):
    """Raised when a message does not close a completed response."""


class ConversationForkNotFoundError(LookupError):
    """Raised when a Conversation or source message does not exist."""


__all__ = [
    "ConversationForkBoundary",
    "ConversationForkCommand",
    "ConversationForkConflictError",
    "ConversationForkInvalidAnchorError",
    "ConversationForkNotFoundError",
    "ConversationForkOrigin",
    "ConversationForkResult",
]
