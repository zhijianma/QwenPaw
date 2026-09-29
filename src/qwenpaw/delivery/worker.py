# -*- coding: utf-8 -*-
"""Application worker connecting Task events to durable delivery attempts."""

from __future__ import annotations

from uuid import UUID

from ..kernel import DeliveryPolicy, InboxProjectionPort
from ..tasks.event_application import TaskEventApplicationService
from .dispatch import DeliveryDispatchResult, DeliveryDispatcher
from .projector import TaskDeliveryProjector


class TaskDeliveryWorker:
    """Follow committed events and dispatch selected projections."""

    def __init__(
        self,
        *,
        events: TaskEventApplicationService,
        projector: TaskDeliveryProjector,
        dispatcher: DeliveryDispatcher,
        inbox: InboxProjectionPort | None = None,
    ) -> None:
        self._events = events
        self._projector = projector
        self._dispatcher = dispatcher
        self._inbox = inbox

    async def follow(
        self,
        task_id: UUID,
        policy: DeliveryPolicy,
        *,
        owner_id: str,
        after_sequence: int = 0,
        poll_interval: float = 0.25,
    ) -> tuple[DeliveryDispatchResult, ...]:
        """Replay from a cursor, follow to terminal, and return receipts."""
        results: list[DeliveryDispatchResult] = []
        async for event in self._events.follow(
            task_id,
            after_sequence=after_sequence,
            poll_interval=poll_interval,
        ):
            requests = await self._projector.project(event, policy)
            for request in requests:
                result = await self._dispatcher.dispatch(
                    request,
                    attempt=1,
                    owner_id=owner_id,
                )
                receipt = result.attempt.receipt
                if self._inbox is not None and receipt is not None:
                    await self._inbox.project(request, receipt)
                results.append(result)
        return tuple(results)


__all__ = ["TaskDeliveryWorker"]
