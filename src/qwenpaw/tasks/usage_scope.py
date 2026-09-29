# -*- coding: utf-8 -*-
"""Process-local leases for sharing one Task usage budget over HTTP."""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass

from ..kernel.models import UsageMeter

USAGE_SCOPE_CONTEXT_KEY = "os_usage_scope_id"
USAGE_SCOPE_LEASE_CONTEXT_KEY = "_task_usage_scope_lease"
RECORD_MODEL_USAGE_CONTEXT_KEY = "_task_record_model_usage"


class UsageScopeUnavailableError(LookupError):
    """Raised when a scope is absent, expired, or belongs to another agent."""


@dataclass(slots=True)
class _UsageScopeEntry:
    meter: UsageMeter
    agent_id: str
    references: int


class UsageScopeLease:
    """Keep one shared usage scope alive until this execution finishes."""

    __slots__ = ("_registry", "scope_id", "meter", "_closed")

    def __init__(
        self,
        registry: "UsageScopeRegistry",
        scope_id: str,
        meter: UsageMeter,
    ) -> None:
        self._registry = registry
        self.scope_id = scope_id
        self.meter = meter
        self._closed = False

    def close(self) -> None:
        """Release this reference exactly once."""
        if self._closed:
            return
        self._closed = True
        self._registry.release(self.scope_id)


class UsageScopeRegistry:
    """Resolve host-issued opaque scope IDs to shared usage meters."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[str, _UsageScopeEntry] = {}

    def open_root(
        self,
        meter: UsageMeter,
        *,
        agent_id: str,
    ) -> UsageScopeLease:
        """Create a new unguessable root scope with one owning reference."""
        scope_id = secrets.token_urlsafe(32)
        with self._lock:
            self._entries[scope_id] = _UsageScopeEntry(
                meter=meter,
                agent_id=agent_id,
                references=1,
            )
        return UsageScopeLease(self, scope_id, meter)

    def acquire(
        self,
        scope_id: str,
        *,
        agent_id: str,
    ) -> UsageScopeLease:
        """Acquire a child lease only for the scope's bound agent."""
        with self._lock:
            entry = self._entries.get(scope_id)
            if entry is None or entry.agent_id != agent_id:
                raise UsageScopeUnavailableError(scope_id)
            entry.references += 1
            return UsageScopeLease(self, scope_id, entry.meter)

    def release(self, scope_id: str) -> None:
        """Release one reference and erase an unused scope."""
        with self._lock:
            entry = self._entries.get(scope_id)
            if entry is None:
                return
            entry.references -= 1
            if entry.references <= 0:
                self._entries.pop(scope_id, None)

    def contains(self, scope_id: str) -> bool:
        """Return whether a scope remains live; intended for diagnostics."""
        with self._lock:
            return scope_id in self._entries


lite_usage_scope_registry = UsageScopeRegistry()


def close_usage_scope_in_context(request_context: object) -> None:
    """Release and remove a lease from an internal request context."""
    if not isinstance(request_context, dict):
        return
    lease = request_context.pop(USAGE_SCOPE_LEASE_CONTEXT_KEY, None)
    close = getattr(lease, "close", None)
    if callable(close):
        close()


__all__ = [
    "RECORD_MODEL_USAGE_CONTEXT_KEY",
    "USAGE_SCOPE_CONTEXT_KEY",
    "USAGE_SCOPE_LEASE_CONTEXT_KEY",
    "UsageScopeLease",
    "UsageScopeRegistry",
    "UsageScopeUnavailableError",
    "close_usage_scope_in_context",
    "lite_usage_scope_registry",
]
