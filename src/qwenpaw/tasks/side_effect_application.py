# -*- coding: utf-8 -*-
"""Transport-neutral recovery commands for Task side effects."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from ..kernel.models import ActorRef, SideEffectRecord
from .service import TaskService


@dataclass(frozen=True, slots=True)
class AuthorizeSideEffectRetryCommand:
    """Human assertion that one uncertain side effect may run again."""

    task_id: UUID
    record_id: UUID
    actor: ActorRef
    reason: str


class TaskSideEffectApplicationService:
    """Expose Task-owned side-effect recovery without leaking storage."""

    def __init__(self, service: TaskService) -> None:
        self._service = service

    async def list(
        self,
        task_id: UUID,
        *,
        run_id: UUID | None = None,
    ) -> tuple[SideEffectRecord, ...]:
        """Return authoritative side effects for one Task or Run."""
        return tuple(
            await self._service.list_side_effects(
                task_id,
                run_id=run_id,
            ),
        )

    async def authorize_retry(
        self,
        command: AuthorizeSideEffectRetryCommand,
    ) -> SideEffectRecord:
        """Persist one explicit retry authorization."""
        return await self._service.authorize_side_effect_retry(
            command.task_id,
            command.record_id,
            actor=command.actor,
            reason=command.reason,
        )


__all__ = [
    "AuthorizeSideEffectRetryCommand",
    "TaskSideEffectApplicationService",
]
