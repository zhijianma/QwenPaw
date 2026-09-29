# -*- coding: utf-8 -*-
"""Contract tests for source-independent delivery projections."""

from uuid import uuid4

import pytest

from qwenpaw.kernel import (
    DeliveryAdapter,
    DeliveryDestination,
    DeliveryKind,
    DeliveryMode,
    DeliveryPolicy,
    DeliveryReceipt,
    DeliveryRequest,
    DeliveryStatus,
)


class _Adapter:
    adapter_id = "example.delivery"

    def supports(self, request: DeliveryRequest) -> bool:
        return request.destination.adapter_id == self.adapter_id

    async def deliver(
        self,
        request: DeliveryRequest,
        *,
        attempt: int,
    ) -> DeliveryReceipt:
        return DeliveryReceipt(
            delivery_id=request.delivery_id,
            adapter_id=self.adapter_id,
            status=DeliveryStatus.DELIVERED,
            attempt=attempt,
        )


def _request(**updates) -> DeliveryRequest:
    values = {
        "source_event_id": uuid4(),
        "idempotency_key": "task:event:channel",
        "agent_id": "default",
        "kind": DeliveryKind.RESULT,
        "mode": DeliveryMode.FINAL,
        "destination": DeliveryDestination(
            adapter_id="example.delivery",
            address="channel-address",
            conversation_id="chat-1",
        ),
        "conversation_id": "chat-1",
        "task_id": uuid4(),
    }
    values.update(updates)
    return DeliveryRequest(**values)


@pytest.mark.asyncio
async def test_delivery_adapter_returns_source_bound_receipt() -> None:
    request = _request()
    adapter = _Adapter()

    assert isinstance(adapter, DeliveryAdapter)
    assert adapter.supports(request)
    receipt = await adapter.deliver(request, attempt=1)

    assert receipt.delivery_id == request.delivery_id
    assert receipt.status is DeliveryStatus.DELIVERED
    assert "session_id" not in request.model_dump(mode="json")


def test_delivery_rejects_run_without_task() -> None:
    with pytest.raises(ValueError, match="run_id requires task_id"):
        _request(task_id=None, run_id=uuid4())


def test_delivery_causal_identity_is_optional_and_round_trips() -> None:
    invocation_id = uuid4()
    correlation_id = uuid4()
    request = _request(
        invocation_id=invocation_id,
        correlation_id=correlation_id,
    )

    restored = DeliveryRequest.model_validate_json(
        request.model_dump_json(),
    )
    legacy = request.model_dump(mode="json")
    legacy.pop("invocation_id")
    legacy.pop("correlation_id")

    assert restored.invocation_id == invocation_id
    assert restored.correlation_id == correlation_id
    assert DeliveryRequest.model_validate(legacy).invocation_id is None


def test_delivery_rejects_cross_conversation_destination() -> None:
    with pytest.raises(ValueError, match="conversation does not match"):
        _request(
            destination=DeliveryDestination(
                adapter_id="example.delivery",
                address="channel-address",
                conversation_id="chat-2",
            ),
        )


def test_failed_receipt_requires_bounded_error_code() -> None:
    with pytest.raises(ValueError, match="requires error_code"):
        DeliveryReceipt(
            delivery_id=uuid4(),
            adapter_id="example.delivery",
            status=DeliveryStatus.FAILED,
        )

    with pytest.raises(ValueError, match="cannot contain an error"):
        DeliveryReceipt(
            delivery_id=uuid4(),
            adapter_id="example.delivery",
            status=DeliveryStatus.DELIVERED,
            error_code="unexpected",
        )


def test_delivery_policy_uses_trimmed_exact_suppression() -> None:
    """Quiet-result matching remains explicit and deterministic."""
    policy = DeliveryPolicy(
        destination=DeliveryDestination(
            adapter_id="example.delivery",
            address="local",
        ),
        suppress_exact_text=("HEARTBEAT_OK",),
    )

    assert policy.suppresses_text("  HEARTBEAT_OK\n")
    assert not policy.suppresses_text("HEARTBEAT_OK: changed")


def test_delivery_policy_suppresses_empty_only_when_enabled() -> None:
    """Empty-result suppression is explicit and disabled by default."""
    destination = DeliveryDestination(
        adapter_id="example.delivery",
        address="local",
    )

    assert not DeliveryPolicy(destination=destination).suppresses_text("")
    quiet = DeliveryPolicy(
        destination=destination,
        suppress_empty_text=True,
    )
    assert quiet.suppresses_text(" \n")
