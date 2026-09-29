# -*- coding: utf-8 -*-
"""System delivery adapter selecting the local durable Inbox."""

from __future__ import annotations

from ..kernel import (
    DeliveryMode,
    DeliveryReceipt,
    DeliveryRequest,
    DeliveryStatus,
)

SYSTEM_INBOX_DELIVERY_ID = "qwenpaw.system.tasks.inbox-delivery"
SYSTEM_INBOX_ADDRESS = "local"


class SystemInboxDeliveryAdapter:
    """Acknowledge Task facts selected for the local Inbox projection.

    Persistence remains owned by ``InboxProjectionPort``. The adapter only
    selects the destination and produces the same durable receipt contract as
    external delivery adapters, so replay can finish an interrupted Inbox
    projection without mutating Task facts.
    """

    adapter_id = SYSTEM_INBOX_DELIVERY_ID

    def supports(self, request: DeliveryRequest) -> bool:
        """Accept final text explicitly addressed to the local Inbox."""
        return (
            request.destination.adapter_id == self.adapter_id
            and request.destination.address == SYSTEM_INBOX_ADDRESS
            and request.mode is DeliveryMode.FINAL
            and isinstance(request.payload.get("text"), str)
        )

    async def deliver(
        self,
        request: DeliveryRequest,
        *,
        attempt: int,
    ) -> DeliveryReceipt:
        """Return a source-bound receipt for the projection worker."""
        if not self.supports(request):
            return DeliveryReceipt(
                delivery_id=request.delivery_id,
                adapter_id=self.adapter_id,
                status=DeliveryStatus.FAILED,
                attempt=attempt,
                error_code="inbox_destination_unsupported",
            )
        return DeliveryReceipt(
            delivery_id=request.delivery_id,
            adapter_id=self.adapter_id,
            status=DeliveryStatus.DELIVERED,
            attempt=attempt,
        )


__all__ = [
    "SYSTEM_INBOX_ADDRESS",
    "SYSTEM_INBOX_DELIVERY_ID",
    "SystemInboxDeliveryAdapter",
]
