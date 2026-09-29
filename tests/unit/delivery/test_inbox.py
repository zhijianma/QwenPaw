# -*- coding: utf-8 -*-
"""Tests for the system local Inbox delivery adapter."""

from uuid import uuid4

import pytest

from qwenpaw.delivery import (
    SYSTEM_INBOX_ADDRESS,
    SYSTEM_INBOX_DELIVERY_ID,
    SystemInboxDeliveryAdapter,
)
from qwenpaw.kernel import (
    DeliveryDestination,
    DeliveryKind,
    DeliveryMode,
    DeliveryRequest,
    DeliveryStatus,
)


def _request(*, address: str = SYSTEM_INBOX_ADDRESS) -> DeliveryRequest:
    return DeliveryRequest(
        source_event_id=uuid4(),
        idempotency_key="task:event:inbox",
        agent_id="default",
        kind=DeliveryKind.RESULT,
        mode=DeliveryMode.FINAL,
        destination=DeliveryDestination(
            adapter_id=SYSTEM_INBOX_DELIVERY_ID,
            address=address,
        ),
        payload={"text": "Heartbeat found one actionable change."},
    )


@pytest.mark.asyncio
async def test_inbox_adapter_acknowledges_supported_projection() -> None:
    """A valid local destination returns a source-bound receipt."""
    request = _request()

    receipt = await SystemInboxDeliveryAdapter().deliver(
        request,
        attempt=1,
    )

    assert receipt.delivery_id == request.delivery_id
    assert receipt.adapter_id == SYSTEM_INBOX_DELIVERY_ID
    assert receipt.status is DeliveryStatus.DELIVERED


@pytest.mark.asyncio
async def test_inbox_adapter_fails_closed_for_another_address() -> None:
    """An unknown local address must not be acknowledged as delivered."""
    request = _request(address="another-inbox")

    receipt = await SystemInboxDeliveryAdapter().deliver(
        request,
        attempt=1,
    )

    assert receipt.status is DeliveryStatus.FAILED
    assert receipt.error_code == "inbox_destination_unsupported"
