# -*- coding: utf-8 -*-
"""Tests for the Delivery-backed Lite Inbox projection."""

from pathlib import Path
from uuid import uuid4

import pytest

from qwenpaw.inbox import SQLiteInboxProjectionStore
from qwenpaw.kernel import (
    DeliveryDestination,
    DeliveryKind,
    DeliveryMode,
    DeliveryReceipt,
    DeliveryRequest,
    DeliveryStatus,
    InboxProjectionConflictError,
    InboxProjectionPort,
)


def _request() -> DeliveryRequest:
    return DeliveryRequest(
        source_event_id=uuid4(),
        idempotency_key="result-1",
        agent_id="default",
        registry_generation=3,
        kind=DeliveryKind.RESULT,
        mode=DeliveryMode.FINAL,
        destination=DeliveryDestination(
            adapter_id="test.channel-delivery",
            address="opaque",
            conversation_id="chat-1",
        ),
        conversation_id="chat-1",
        task_id=uuid4(),
        run_id=uuid4(),
        payload={
            "text": "Completed result",
            "ignored": {"large": True},
            "operational_payload": {
                "uid": 42,
                "folder": "INBOX",
            },
        },
    )


def _receipt(request: DeliveryRequest, attempt: int = 1) -> DeliveryReceipt:
    assert request.delivery_id is not None
    return DeliveryReceipt(
        delivery_id=request.delivery_id,
        adapter_id=request.destination.adapter_id,
        status=DeliveryStatus.DELIVERED,
        attempt=attempt,
    )


@pytest.mark.asyncio
async def test_project_read_and_handle_do_not_change_source(
    tmp_path: Path,
) -> None:
    store = SQLiteInboxProjectionStore(tmp_path / "inbox.db")
    request = _request()
    receipt = _receipt(request)

    item = await store.project(request, receipt)
    replay = await store.project(request, receipt)
    read = await store.mark_read(
        item.item_id,
        agent_id="default",
        expected_revision=item.revision,
    )
    handled = await store.mark_handled(
        item.item_id,
        agent_id="default",
        expected_revision=read.revision,
    )

    assert isinstance(store, InboxProjectionPort)
    assert replay == item
    assert handled.read and handled.handled
    assert handled.source_event_id == request.source_event_id
    assert handled.task_id == request.task_id
    assert handled.summary == "Completed result"
    assert handled.source_payload == {"uid": 42, "folder": "INBOX"}
    assert "ignored" not in handled.model_dump_json()
    assert await store.list_items(agent_id="default", unread_only=True) == ()


@pytest.mark.asyncio
async def test_projection_is_agent_isolated_and_revisioned(
    tmp_path: Path,
) -> None:
    store = SQLiteInboxProjectionStore(tmp_path / "inbox.db")
    request = _request()
    item = await store.project(request, _receipt(request))

    assert await store.list_items(agent_id="another") == ()
    with pytest.raises(InboxProjectionConflictError, match="revision"):
        await store.mark_read(
            item.item_id,
            agent_id="default",
            expected_revision=item.revision + 1,
        )


@pytest.mark.asyncio
async def test_later_delivery_attempt_advances_same_item(
    tmp_path: Path,
) -> None:
    store = SQLiteInboxProjectionStore(tmp_path / "inbox.db")
    request = _request()
    first = DeliveryReceipt(
        delivery_id=request.delivery_id,
        adapter_id=request.destination.adapter_id,
        status=DeliveryStatus.FAILED,
        attempt=1,
        error_code="temporary",
    )
    failed = await store.project(request, first)
    delivered = await store.project(request, _receipt(request, attempt=2))

    assert delivered.item_id == failed.item_id
    assert delivered.revision == failed.revision + 1
    assert delivered.delivery_status is DeliveryStatus.DELIVERED


@pytest.mark.asyncio
async def test_replay_rejects_changed_operational_payload(
    tmp_path: Path,
) -> None:
    store = SQLiteInboxProjectionStore(tmp_path / "inbox.db")
    request = _request()
    await store.project(request, _receipt(request))
    changed = request.model_copy(
        update={
            "payload": {
                **request.payload,
                "operational_payload": {"uid": 99},
            },
        },
    )

    with pytest.raises(InboxProjectionConflictError, match="source facts"):
        await store.project(changed, _receipt(changed))
