# -*- coding: utf-8 -*-
"""Tests for safe-point steer delivery semantics."""

from uuid import uuid4

import pytest

from qwenpaw.invocation_control import (
    SteerInvocationUnavailableError,
    SteeringMailbox,
)
from qwenpaw.kernel import (
    ControlCommand,
    ControlCommandKind,
    SteerSafePoint,
)


def _steer(invocation_id, key: str = "steer-1") -> ControlCommand:
    return ControlCommand(
        kind=ControlCommandKind.STEER,
        agent_id="default",
        conversation_id="chat-1",
        idempotency_key=key,
        expected_revision=2,
        target_invocation_id=invocation_id,
        instruction="change direction",
    )


@pytest.mark.asyncio
async def test_acknowledge_records_the_actual_safe_point() -> None:
    mailbox = SteeringMailbox()
    invocation_id = uuid4()
    await mailbox.bind(invocation_id)
    result = await mailbox.offer(_steer(invocation_id))

    delivery = await mailbox.claim(invocation_id)
    assert delivery is not None
    assert not result.done()

    await mailbox.acknowledge(
        delivery,
        SteerSafePoint.BEFORE_REASONING,
    )

    assert await result is SteerSafePoint.BEFORE_REASONING


@pytest.mark.asyncio
async def test_release_retries_without_false_applied_receipt() -> None:
    mailbox = SteeringMailbox()
    invocation_id = uuid4()
    await mailbox.bind(invocation_id)
    result = await mailbox.offer(_steer(invocation_id))
    first = await mailbox.claim(invocation_id)
    assert first is not None

    await mailbox.release(first)
    second = await mailbox.claim(invocation_id)

    assert second == first
    assert not result.done()


@pytest.mark.asyncio
async def test_unbind_fails_pending_and_claimed_deliveries() -> None:
    mailbox = SteeringMailbox()
    invocation_id = uuid4()
    await mailbox.bind(invocation_id)
    pending = await mailbox.offer(_steer(invocation_id, "pending"))
    claimed = await mailbox.offer(_steer(invocation_id, "claimed"))
    delivery = await mailbox.claim(invocation_id)
    assert delivery is not None

    await mailbox.unbind(invocation_id)

    with pytest.raises(SteerInvocationUnavailableError):
        await pending
    with pytest.raises(SteerInvocationUnavailableError):
        await claimed


@pytest.mark.asyncio
async def test_offer_rejects_inactive_invocation() -> None:
    mailbox = SteeringMailbox()

    with pytest.raises(SteerInvocationUnavailableError):
        await mailbox.offer(_steer(uuid4()))
