# -*- coding: utf-8 -*-
"""Namespaced durable state owned by one Agent Mode Provider."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Literal, Self
from uuid import UUID

from pydantic import AliasChoices, AwareDatetime, Field, model_validator

from .models import (
    JsonObject,
    KernelModel,
    NamespacedId,
    NonEmptyStr,
    utc_now,
)

MAX_MODE_STATE_BYTES = 65536


class AgentModeState(KernelModel):
    """One revisioned plugin-owned state value scoped to a ChatSpec."""

    schema_id: Literal["qwenpaw.agent-mode-state.v1"] = Field(
        default="qwenpaw.agent-mode-state.v1",
        alias="schema",
    )
    provider_id: NamespacedId
    agent_id: NonEmptyStr
    chat_id: NonEmptyStr = Field(
        validation_alias=AliasChoices("chat_id", "conversation_id"),
        description="Owning ChatSpec.id",
    )
    state_key: NamespacedId = "default"
    value: JsonObject = Field(default_factory=dict)
    state_schema_version: int = Field(default=1, ge=1)
    revision: int = Field(default=0, ge=0)
    writer_registry_epoch_id: UUID
    writer_generation: int = Field(ge=1)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

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
    def validate_payload_size(self) -> Self:
        """Bound synchronous serialization work and local state growth."""
        encoded = json.dumps(
            self.value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > MAX_MODE_STATE_BYTES:
            raise ValueError(
                f"agent mode state exceeds {MAX_MODE_STATE_BYTES} bytes",
            )
        return self


class AgentModeStateConflictError(RuntimeError):
    """Raised when a state writer observed a stale revision."""


__all__ = [
    "AgentModeState",
    "AgentModeStateConflictError",
    "MAX_MODE_STATE_BYTES",
]
