# -*- coding: utf-8 -*-
"""Stable Conversation branching contracts shared by every edition."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum

from pydantic import AliasChoices, AwareDatetime, Field, model_validator

from .models import KernelModel, NonEmptyStr, utc_now


class ConversationForkBoundary(str, Enum):
    """Stable message boundary selected for a Conversation fork."""

    AFTER_RESPONSE = "after_response"


def _canonical_chat_id(
    chat_id: object,
    conversation_id: object,
    *,
    field_name: str,
) -> object:
    """Resolve one ChatSpec.id while accepting a deprecated field."""
    if (
        chat_id is not None
        and conversation_id is not None
        and chat_id != conversation_id
    ):
        raise ValueError(f"{field_name} identities must identify one Chat")
    return chat_id if chat_id is not None else conversation_id


class ConversationForkCommand(KernelModel):
    """Idempotent request to branch one completed Conversation turn."""

    agent_id: NonEmptyStr
    parent_chat_id: NonEmptyStr = Field(
        validation_alias=AliasChoices(
            "parent_chat_id",
            "parent_conversation_id",
        ),
        description="Parent ChatSpec.id",
    )
    source_message_id: NonEmptyStr
    idempotency_key: NonEmptyStr = Field(max_length=200)
    boundary: ConversationForkBoundary = (
        ConversationForkBoundary.AFTER_RESPONSE
    )
    name: NonEmptyStr | None = Field(default=None, max_length=200)

    @model_validator(mode="before")
    @classmethod
    def validate_parent_identity(cls, value: object) -> object:
        """Reject conflicting canonical and deprecated identities."""
        if isinstance(value, Mapping):
            _canonical_chat_id(
                value.get("parent_chat_id"),
                value.get("parent_conversation_id"),
                field_name="parent fork",
            )
        return value

    @property
    def parent_conversation_id(self) -> str:
        """Return the deprecated Python alias during migration."""
        return self.parent_chat_id


class ConversationForkOrigin(KernelModel):
    """Immutable lineage attached to a forked Conversation."""

    agent_id: NonEmptyStr
    parent_chat_id: NonEmptyStr = Field(
        validation_alias=AliasChoices(
            "parent_chat_id",
            "parent_conversation_id",
        ),
        description="Parent ChatSpec.id",
    )
    root_chat_id: NonEmptyStr = Field(
        validation_alias=AliasChoices(
            "root_chat_id",
            "root_conversation_id",
        ),
        description="Root ChatSpec.id",
    )
    source_message_id: NonEmptyStr
    boundary: ConversationForkBoundary = (
        ConversationForkBoundary.AFTER_RESPONSE
    )
    depth: int = Field(ge=1)
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="before")
    @classmethod
    def validate_lineage_identities(cls, value: object) -> object:
        """Reject conflicting canonical and deprecated identities."""
        if isinstance(value, Mapping):
            _canonical_chat_id(
                value.get("parent_chat_id"),
                value.get("parent_conversation_id"),
                field_name="parent fork",
            )
            _canonical_chat_id(
                value.get("root_chat_id"),
                value.get("root_conversation_id"),
                field_name="root fork",
            )
        return value

    @property
    def parent_conversation_id(self) -> str:
        """Return the deprecated parent alias during migration."""
        return self.parent_chat_id

    @property
    def root_conversation_id(self) -> str:
        """Return the deprecated root alias during migration."""
        return self.root_chat_id


class ConversationForkResult(KernelModel):
    """Authoritative child identity and immutable branch origin."""

    child_chat_id: NonEmptyStr = Field(
        validation_alias=AliasChoices(
            "child_chat_id",
            "child_conversation_id",
        ),
        description="Child ChatSpec.id",
    )
    origin: ConversationForkOrigin

    @model_validator(mode="before")
    @classmethod
    def validate_child_identity(cls, value: object) -> object:
        """Reject conflicting canonical and deprecated identities."""
        if isinstance(value, Mapping):
            _canonical_chat_id(
                value.get("child_chat_id"),
                value.get("child_conversation_id"),
                field_name="child fork",
            )
        return value

    @property
    def child_conversation_id(self) -> str:
        """Return the deprecated Python alias during migration."""
        return self.child_chat_id


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
