# -*- coding: utf-8 -*-
"""Stable state contracts for invocation-scoped Memory Providers."""

from __future__ import annotations

from enum import Enum

from pydantic import AwareDatetime, Field, JsonValue

from .models import KernelModel, NamespacedId, NonEmptyStr, utc_now


class MemoryStateScope(str, Enum):
    """Ownership boundary for one provider state namespace."""

    AGENT = "agent"
    CONVERSATION = "conversation"


class MemoryStateSnapshot(KernelModel):
    """One revisioned JSON value owned by a Memory Provider."""

    provider_id: NamespacedId
    scope: MemoryStateScope
    owner_id: NonEmptyStr
    key: str = Field(min_length=1, max_length=200)
    value: JsonValue
    revision: int = Field(ge=1)
    updated_at: AwareDatetime = Field(default_factory=utc_now)


class MemoryStateConflictError(RuntimeError):
    """Raised when a state mutation uses a stale revision."""


class MemoryStateUnavailableError(RuntimeError):
    """Raised when the requested state scope has no stable owner."""


__all__ = [
    "MemoryStateConflictError",
    "MemoryStateScope",
    "MemoryStateSnapshot",
    "MemoryStateUnavailableError",
]
