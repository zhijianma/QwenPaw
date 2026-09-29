# -*- coding: utf-8 -*-
"""Transport-neutral queries for Task execution events."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

from ..kernel.events import ExecutionEvent
from ..kernel.state_machine import is_task_terminal
from .service import TaskNotFoundError, TaskService


@dataclass(frozen=True, slots=True)
class TaskEventPage:
    """One ordered page from the append-only Task ledger."""

    items: tuple[ExecutionEvent, ...]
    next_sequence: int


class TaskEventApplicationService:
    """Read and follow Task events without transport dependencies."""

    def __init__(self, service: TaskService) -> None:
        self._service = service

    async def page(
        self,
        task_id: UUID,
        *,
        after_sequence: int = 0,
        limit: int = 200,
    ) -> TaskEventPage:
        """Return one event page after validating Task ownership."""
        await self._required_task(task_id)
        items = tuple(
            await self._service.list_events(
                task_id,
                after_sequence=after_sequence,
                limit=limit,
            ),
        )
        return TaskEventPage(
            items=items,
            next_sequence=(items[-1].sequence if items else after_sequence),
        )

    async def follow(
        self,
        task_id: UUID,
        *,
        after_sequence: int = 0,
        page_size: int = 200,
        poll_interval: float = 0.25,
    ) -> AsyncIterator[ExecutionEvent]:
        """Replay committed events and follow until the Task is terminal."""
        await self._required_task(task_id)
        sequence = after_sequence
        while True:
            events = await self._service.list_events(
                task_id,
                after_sequence=sequence,
                limit=page_size,
            )
            for event in events:
                sequence = event.sequence
                yield event
            task = await self._service.get_task(task_id)
            if task is None:
                raise TaskNotFoundError(str(task_id))
            if is_task_terminal(task.status):
                return
            await asyncio.sleep(poll_interval)

    async def _required_task(self, task_id: UUID):
        task = await self._service.get_task(task_id)
        if task is None:
            raise TaskNotFoundError(str(task_id))
        return task


__all__ = ["TaskEventApplicationService", "TaskEventPage"]
