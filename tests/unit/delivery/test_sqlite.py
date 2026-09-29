# -*- coding: utf-8 -*-
"""Contract tests for the Lite durable Delivery projection store."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from qwenpaw.delivery import SQLiteDeliveryProjectionStore
from qwenpaw.kernel import (
    DeliveryAttemptConflictError,
    DeliveryAttemptStatus,
    DeliveryDestination,
    DeliveryKind,
    DeliveryMode,
    DeliveryProjectionPort,
    DeliveryReceipt,
    DeliveryRequest,
    DeliveryRequestConflictError,
    DeliveryStatus,
)


def _request(
    *,
    agent_id: str = "default",
    key: str = "task:event:channel",
    payload: dict | None = None,
) -> DeliveryRequest:
    return DeliveryRequest(
        source_event_id="00000000-0000-0000-0000-000000000001",
        idempotency_key=key,
        agent_id=agent_id,
        kind=DeliveryKind.RESULT,
        mode=DeliveryMode.FINAL,
        destination=DeliveryDestination(
            adapter_id="qwenpaw.system.channel-delivery",
            address="console:default",
            conversation_id="chat-1",
        ),
        conversation_id="chat-1",
        payload=payload or {"text": "done"},
    )


@pytest.mark.asyncio
async def test_concurrent_claim_has_one_owner_and_survives_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "delivery.db"
    store = SQLiteDeliveryProjectionStore(path)
    request = _request()

    claims = await asyncio.gather(
        store.claim(
            request,
            attempt=1,
            owner_id="worker.first",
            lease_seconds=30,
        ),
        store.claim(
            request.model_copy(
                update={"created_at": datetime.now(timezone.utc)},
            ),
            attempt=1,
            owner_id="worker.second",
            lease_seconds=30,
        ),
    )

    assert isinstance(store, DeliveryProjectionPort)
    assert claims[0] == claims[1]
    assert claims[0].owner_id in {"worker.first", "worker.second"}
    reopened = SQLiteDeliveryProjectionStore(path)
    assert await reopened.get_request(request.delivery_id) is not None
    assert await reopened.list_attempts(request.delivery_id) == (claims[0],)


@pytest.mark.asyncio
async def test_same_identity_rejects_conflicting_projection(
    tmp_path: Path,
) -> None:
    store = SQLiteDeliveryProjectionStore(tmp_path / "delivery.db")
    request = _request(payload={"text": "first"})
    await store.claim(
        request,
        attempt=1,
        owner_id="worker",
        lease_seconds=30,
    )

    with pytest.raises(DeliveryRequestConflictError, match="conflicting"):
        await store.claim(
            _request(payload={"text": "different"}),
            attempt=1,
            owner_id="worker",
            lease_seconds=30,
        )


@pytest.mark.asyncio
async def test_failed_attempt_allows_explicit_retry_but_success_stops_it(
    tmp_path: Path,
) -> None:
    store = SQLiteDeliveryProjectionStore(tmp_path / "delivery.db")
    request = _request()
    first = await store.claim(
        request,
        attempt=1,
        owner_id="worker",
        lease_seconds=30,
    )
    failed = await store.settle(
        DeliveryReceipt(
            delivery_id=first.delivery_id,
            adapter_id=request.destination.adapter_id,
            status=DeliveryStatus.FAILED,
            attempt=1,
            error_code="channel_unavailable",
        ),
        owner_id="worker",
        expected_revision=first.revision,
    )
    assert failed.status is DeliveryAttemptStatus.FAILED

    second = await store.claim(
        request,
        attempt=2,
        owner_id="retry-worker",
        lease_seconds=30,
    )
    delivered = await store.settle(
        DeliveryReceipt(
            delivery_id=second.delivery_id,
            adapter_id=request.destination.adapter_id,
            status=DeliveryStatus.DELIVERED,
            attempt=2,
        ),
        owner_id="retry-worker",
        expected_revision=second.revision,
    )
    assert delivered.status is DeliveryAttemptStatus.DELIVERED

    with pytest.raises(DeliveryAttemptConflictError, match="cannot create"):
        await store.claim(
            request,
            attempt=3,
            owner_id="third-worker",
            lease_seconds=30,
        )


@pytest.mark.asyncio
async def test_renew_and_settle_require_owner_revision_and_adapter(
    tmp_path: Path,
) -> None:
    store = SQLiteDeliveryProjectionStore(tmp_path / "delivery.db")
    request = _request()
    claimed = await store.claim(
        request,
        attempt=1,
        owner_id="worker",
        lease_seconds=30,
    )

    with pytest.raises(DeliveryAttemptConflictError, match="another owner"):
        await store.renew(
            claimed.delivery_id,
            attempt=1,
            owner_id="other",
            expected_revision=claimed.revision,
            lease_seconds=30,
        )

    renewed = await store.renew(
        claimed.delivery_id,
        attempt=1,
        owner_id="worker",
        expected_revision=claimed.revision,
        lease_seconds=30,
    )
    assert renewed.revision == claimed.revision + 1

    with pytest.raises(DeliveryAttemptConflictError, match="adapter"):
        await store.settle(
            DeliveryReceipt(
                delivery_id=renewed.delivery_id,
                adapter_id="other.adapter",
                status=DeliveryStatus.DELIVERED,
                attempt=1,
            ),
            owner_id="worker",
            expected_revision=renewed.revision,
        )


@pytest.mark.asyncio
async def test_recover_expired_is_agent_scoped_and_blocks_retry(
    tmp_path: Path,
) -> None:
    store = SQLiteDeliveryProjectionStore(tmp_path / "delivery.db")
    first_request = _request(agent_id="first", key="first:key")
    second_request = _request(agent_id="second", key="second:key")
    first = await store.claim(
        first_request,
        attempt=1,
        owner_id="worker",
        lease_seconds=0.01,
    )
    second = await store.claim(
        second_request,
        attempt=1,
        owner_id="worker",
        lease_seconds=0.01,
    )
    future = datetime.now(timezone.utc) + timedelta(seconds=1)

    recovered = await store.recover_expired(
        agent_id="first",
        now=future,
    )

    assert len(recovered) == 1
    assert recovered[0].delivery_id == first.delivery_id
    assert recovered[0].receipt is not None
    assert recovered[0].status is DeliveryAttemptStatus.UNCERTAIN
    assert recovered[0].receipt.error_code == ("lease_expired_outcome_unknown")
    assert (await store.list_attempts(second.delivery_id))[0].status is (
        DeliveryAttemptStatus.CLAIMED
    )
    with pytest.raises(DeliveryAttemptConflictError, match="prior failure"):
        await store.claim(
            first_request,
            attempt=2,
            owner_id="retry-worker",
            lease_seconds=30,
        )
