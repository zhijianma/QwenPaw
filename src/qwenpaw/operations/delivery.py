# -*- coding: utf-8 -*-
"""Project committed operational facts through Delivery into Inbox."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from ..delivery import (
    SYSTEM_INBOX_ADDRESS,
    SYSTEM_INBOX_DELIVERY_ID,
    DeliveryDispatchResult,
    DeliveryDispatchDisposition,
    DeliveryDispatcher,
)
from ..kernel import (
    DeliveryDestination,
    DeliveryKind,
    DeliveryMode,
    DeliveryRequest,
    InboxItem,
    InboxProjectionPort,
    OperationalEvent,
    OperationalEventPort,
    OperationalStatus,
)


@dataclass(frozen=True, slots=True)
class OperationalDeliveryResult:
    """Committed source fact plus its terminal delivery projections."""

    event: OperationalEvent
    delivery: DeliveryDispatchResult
    inbox_item: InboxItem


class OperationalDeliveryService:
    """Commit one non-Task fact before deriving any notification."""

    def __init__(
        self,
        *,
        events: OperationalEventPort,
        dispatcher: DeliveryDispatcher,
        inbox: InboxProjectionPort,
    ) -> None:
        self._events = events
        self._dispatcher = dispatcher
        self._inbox = inbox

    async def publish(
        self,
        event: OperationalEvent,
        *,
        owner_id: str | None = None,
    ) -> OperationalDeliveryResult:
        """Persist the source, settle Delivery, then project Inbox."""
        committed = await self._events.commit(event)
        request = self._request(committed)
        delivery = await self._dispatcher.dispatch(
            request,
            attempt=1,
            owner_id=owner_id or f"operational:{uuid4()}",
        )
        receipt = delivery.attempt.receipt
        if receipt is None:
            delivery_id = request.delivery_id
            if delivery_id is None:  # pragma: no cover - Kernel validator
                raise ValueError("Operational Delivery has no identity")
            settled = await self._dispatcher.wait_for_terminal(
                delivery_id,
                attempt=1,
            )
            delivery = DeliveryDispatchResult(
                attempt=settled,
                disposition=DeliveryDispatchDisposition.REPLAYED,
            )
            receipt = settled.receipt
        if receipt is None:  # pragma: no cover - wait contract
            raise RuntimeError("Operational Delivery has no terminal receipt")
        item = await self._inbox.project(request, receipt)
        return OperationalDeliveryResult(
            event=committed,
            delivery=delivery,
            inbox_item=item,
        )

    @staticmethod
    def _request(event: OperationalEvent) -> DeliveryRequest:
        event_id = event.event_id
        if event_id is None:  # pragma: no cover - Kernel validator
            raise ValueError("Operational event has no identity")
        kind = (
            DeliveryKind.EXCEPTION
            if event.status is OperationalStatus.ERROR
            else DeliveryKind.RESULT
        )
        return DeliveryRequest(
            source_event_id=event_id,
            idempotency_key=f"operational:{event_id}:inbox",
            agent_id=event.agent_id,
            registry_generation=event.registry_generation,
            kind=kind,
            mode=DeliveryMode.FINAL,
            destination=DeliveryDestination(
                adapter_id=SYSTEM_INBOX_DELIVERY_ID,
                address=SYSTEM_INBOX_ADDRESS,
            ),
            payload={
                "text": event.body,
                "title": event.title,
                "source_type": event.source_type,
                "source_id": event.source_id,
                "event_type": event.event_type,
                "source_status": event.source_status or event.status.value,
                "severity": event.severity.value,
                "operational_payload": event.payload,
            },
            created_at=event.occurred_at,
        )


__all__ = ["OperationalDeliveryResult", "OperationalDeliveryService"]
