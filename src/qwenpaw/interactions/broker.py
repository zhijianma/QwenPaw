# -*- coding: utf-8 -*-
"""Invocation-bound producer API for runtime-to-user interactions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from ..kernel import (
    ContinuationMode,
    InteractionKind,
    InteractionMode,
    InteractionOption,
    InteractionPort,
    InteractionRequest,
    InteractionResolution,
)


@dataclass(frozen=True, slots=True)
class RuntimeInteractionBroker:
    """Bind interaction creation to one ChatSpec invocation."""

    service: InteractionPort
    agent_id: str
    conversation_id: str
    invocation_id: UUID
    correlation_id: UUID | None = None
    deferred_interaction_ids: list[UUID] = field(default_factory=list)

    @property
    def has_deferred_user_input(self) -> bool:
        """Return whether this Invocation must end at its Tool safe point."""
        return bool(self.deferred_interaction_ids)

    async def ask_user(
        self,
        *,
        title: str,
        prompt: str,
        options: tuple[InteractionOption, ...] = (),
        response_schema: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        source_id: UUID | None = None,
        timeout_seconds: float | None = None,
        expires_at: datetime | None = None,
    ) -> InteractionResolution:
        """Persist and await one invocation-scoped user input request."""
        request = InteractionRequest(
            kind=InteractionKind.USER_INPUT,
            mode=InteractionMode.BLOCKING,
            agent_id=self.agent_id,
            conversation_id=self.conversation_id,
            invocation_id=self.invocation_id,
            correlation_id=self.correlation_id or self.invocation_id,
            source_id=source_id,
            title=title,
            prompt=prompt,
            options=options,
            response_schema=dict(response_schema or {}),
            metadata=dict(metadata or {}),
            expires_at=expires_at,
        )
        await self.service.open(request)
        return await self.service.wait(
            request.interaction_id,
            timeout_seconds=timeout_seconds,
        )

    async def suggest(
        self,
        *,
        title: str,
        prompt: str,
        options: tuple[InteractionOption, ...] = (),
        metadata: dict[str, Any] | None = None,
        source_id: UUID | None = None,
        expires_at: datetime | None = None,
    ) -> InteractionRequest:
        """Persist a non-blocking suggestion and return immediately."""
        request = InteractionRequest(
            kind=InteractionKind.SUGGESTION,
            mode=InteractionMode.NON_BLOCKING,
            agent_id=self.agent_id,
            conversation_id=self.conversation_id,
            invocation_id=self.invocation_id,
            correlation_id=self.correlation_id or self.invocation_id,
            source_id=source_id,
            title=title,
            prompt=prompt,
            options=options,
            metadata=dict(metadata or {}),
            expires_at=expires_at,
        )
        return await self.service.open(request)

    async def defer_user_input(
        self,
        *,
        title: str,
        prompt: str,
        options: tuple[InteractionOption, ...] = (),
        response_schema: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        source_id: UUID | None = None,
        expires_at: datetime | None = None,
    ) -> InteractionRequest:
        """Persist input and park execution at the next Tool safe point."""
        request = InteractionRequest(
            kind=InteractionKind.USER_INPUT,
            mode=InteractionMode.BLOCKING,
            agent_id=self.agent_id,
            conversation_id=self.conversation_id,
            invocation_id=self.invocation_id,
            correlation_id=self.correlation_id or self.invocation_id,
            source_id=source_id,
            continuation_mode=ContinuationMode.CONVERSATION_TURN,
            title=title,
            prompt=prompt,
            options=options,
            response_schema=dict(response_schema or {}),
            metadata=dict(metadata or {}),
            expires_at=expires_at,
        )
        opened = await self.service.open(request)
        if opened.interaction_id not in self.deferred_interaction_ids:
            self.deferred_interaction_ids.append(opened.interaction_id)
        return opened


# pylint: disable-next=too-many-return-statements
def runtime_interaction_broker_from_context(
    request_context: dict[str, Any],
) -> RuntimeInteractionBroker | None:
    """Bind only when all server-owned invocation identities are present."""
    service = request_context.get("_interaction_service")
    agent_id = request_context.get("agent_id")
    conversation_id = request_context.get("os_conversation_id")
    raw_invocation_id = request_context.get("os_invocation_id")
    if service is None:
        return None
    if not isinstance(agent_id, str) or not agent_id:
        return None
    if not isinstance(conversation_id, str) or not conversation_id:
        return None
    if not isinstance(raw_invocation_id, str):
        return None
    try:
        invocation_id = UUID(raw_invocation_id)
    except ValueError:
        return None
    correlation_id = invocation_id
    raw_correlation_id = request_context.get("os_correlation_id")
    if raw_correlation_id is not None:
        try:
            correlation_id = UUID(str(raw_correlation_id))
        except ValueError:
            return None
    return RuntimeInteractionBroker(
        service=service,
        agent_id=agent_id,
        conversation_id=conversation_id,
        invocation_id=invocation_id,
        correlation_id=correlation_id,
    )


__all__ = [
    "RuntimeInteractionBroker",
    "runtime_interaction_broker_from_context",
]
