# -*- coding: utf-8 -*-
"""Current Invocation access to a Host-admitted outcome producer."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator
from uuid import UUID

from ..kernel.ports import OutcomeHost


@dataclass(frozen=True)
class RuntimeOutcomeContext:
    """Content-free outcome authority bound to one Chat invocation."""

    conversation_id: str | None = None
    agent_id: str | None = None
    correlation_id: UUID | None = None
    host: OutcomeHost | None = None


_current_outcome_context: ContextVar[RuntimeOutcomeContext] = ContextVar(
    "qwenpaw_runtime_outcome_context",
    default=RuntimeOutcomeContext(),
)


def current_outcome_context() -> RuntimeOutcomeContext:
    """Return the outcome authority for the current Invocation."""
    return _current_outcome_context.get()


def set_current_outcome_context(
    conversation_id: str | None,
    host: OutcomeHost | None,
    *,
    agent_id: str | None = None,
    correlation_id: UUID | None = None,
) -> None:
    """Replace stale request state with the current Invocation authority."""
    _current_outcome_context.set(
        RuntimeOutcomeContext(
            conversation_id=conversation_id,
            agent_id=agent_id,
            correlation_id=correlation_id,
            host=host,
        ),
    )


@contextmanager
def scoped_outcome_context(
    conversation_id: str | None,
    host: OutcomeHost | None,
    *,
    agent_id: str | None = None,
    correlation_id: UUID | None = None,
) -> Iterator[None]:
    """Temporarily bind an outcome authority for tests and adapters."""
    token = _current_outcome_context.set(
        RuntimeOutcomeContext(
            conversation_id=conversation_id,
            agent_id=agent_id,
            correlation_id=correlation_id,
            host=host,
        ),
    )
    try:
        yield
    finally:
        _current_outcome_context.reset(token)


__all__ = [
    "RuntimeOutcomeContext",
    "current_outcome_context",
    "scoped_outcome_context",
    "set_current_outcome_context",
]
