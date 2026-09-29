# -*- coding: utf-8 -*-
"""Focused tests for generation-pinned Delivery dispatch."""

import asyncio
from pathlib import Path

import pytest

from qwenpaw.delivery import (
    DeliveryDispatchDisposition,
    DeliveryDispatcher,
    SQLiteDeliveryProjectionStore,
)
from qwenpaw.kernel import (
    CapabilityBundle,
    CapabilityContribution,
    CapabilityProviderKind,
    DeliveryDestination,
    DeliveryKind,
    DeliveryMode,
    DeliveryReceipt,
    DeliveryRequest,
    DeliveryStatus,
)
from qwenpaw.plugins.generations import GenerationRegistry

_PROVIDER_ID = "delivery-test"
_ADAPTER_ID = f"{_PROVIDER_ID}.channel"


class _Adapter:
    adapter_id = _ADAPTER_ID

    def __init__(self, label: str, release: asyncio.Event | None = None):
        self.label = label
        self.release = release
        self.calls = 0

    def supports(self, request: DeliveryRequest) -> bool:
        return request.destination.adapter_id == self.adapter_id

    async def deliver(
        self,
        request: DeliveryRequest,
        *,
        attempt: int,
    ) -> DeliveryReceipt:
        self.calls += 1
        if self.release is not None:
            await self.release.wait()
        return DeliveryReceipt(
            delivery_id=request.delivery_id,
            adapter_id=self.adapter_id,
            status=DeliveryStatus.DELIVERED,
            attempt=attempt,
            metadata={"adapter": self.label},
        )


class _InvalidReceiptAdapter(_Adapter):
    async def deliver(
        self,
        request: DeliveryRequest,
        *,
        attempt: int,
    ) -> DeliveryReceipt:
        self.calls += 1
        return DeliveryReceipt(
            delivery_id=request.delivery_id,
            adapter_id=self.adapter_id,
            status=DeliveryStatus.DELIVERED,
            attempt=attempt + 1,
        )


class _UncertainAdapter(_Adapter):
    async def deliver(
        self,
        request: DeliveryRequest,
        *,
        attempt: int,
    ) -> DeliveryReceipt:
        del request, attempt
        self.calls += 1
        raise TimeoutError("external acknowledgement was not observed")


def _bundle(version: str) -> CapabilityBundle:
    return CapabilityBundle(
        provider_id=_PROVIDER_ID,
        provider_kind=CapabilityProviderKind.PLUGIN,
        version=version,
        contributions=(
            CapabilityContribution(
                contribution_id="channel",
                slot="delivery.adapter",
                entrypoint="tests.delivery:adapter",
            ),
        ),
    )


def _request(
    generation: int,
    *,
    mode: DeliveryMode = DeliveryMode.FINAL,
    key: str = "task:event:channel",
) -> DeliveryRequest:
    return DeliveryRequest(
        source_event_id="00000000-0000-0000-0000-000000000001",
        idempotency_key=key,
        agent_id="default",
        registry_generation=generation,
        kind=DeliveryKind.RESULT,
        mode=mode,
        destination=DeliveryDestination(
            adapter_id=_ADAPTER_ID,
            address="console:default",
        ),
        payload={"text": "done"},
    )


@pytest.mark.asyncio
async def test_concurrent_dispatch_calls_adapter_once(tmp_path: Path) -> None:
    release = asyncio.Event()
    adapter = _Adapter("current", release)
    registry = GenerationRegistry()
    await registry.activate_bundle(_bundle("1.0.0"), lambda _: adapter)
    dispatcher = DeliveryDispatcher(
        projection=SQLiteDeliveryProjectionStore(tmp_path / "delivery.db"),
        capability_resolver=registry,
    )
    request = _request(registry.generation)

    first = asyncio.create_task(
        dispatcher.dispatch(
            request,
            attempt=1,
            owner_id="worker.first",
        ),
    )
    for _ in range(100):
        if adapter.calls:
            break
        await asyncio.sleep(0.001)
    second = await dispatcher.dispatch(
        request,
        attempt=1,
        owner_id="worker.second",
    )
    release.set()
    first_result = await first

    assert first_result.disposition is DeliveryDispatchDisposition.DELIVERED
    assert second.disposition is (DeliveryDispatchDisposition.OWNED_ELSEWHERE)
    assert adapter.calls == 1


@pytest.mark.asyncio
async def test_dispatch_uses_request_generation_after_hot_replace(
    tmp_path: Path,
) -> None:
    registry = GenerationRegistry()
    old_adapter = _Adapter("old")
    await registry.activate_bundle(_bundle("1.0.0"), lambda _: old_adapter)
    old_generation = registry.generation
    retainer = await registry.pin(old_generation)
    new_adapter = _Adapter("new")
    await registry.activate_bundle(_bundle("2.0.0"), lambda _: new_adapter)
    dispatcher = DeliveryDispatcher(
        projection=SQLiteDeliveryProjectionStore(tmp_path / "delivery.db"),
        capability_resolver=registry,
    )

    result = await dispatcher.dispatch(
        _request(old_generation),
        attempt=1,
        owner_id="worker",
    )
    await retainer.close()

    assert result.attempt.receipt is not None
    assert result.attempt.receipt.metadata["adapter"] == "old"
    assert old_adapter.calls == 1
    assert new_adapter.calls == 0


@pytest.mark.asyncio
async def test_invalid_adapter_receipt_fails_durably(tmp_path: Path) -> None:
    registry = GenerationRegistry()
    adapter = _InvalidReceiptAdapter("invalid")
    await registry.activate_bundle(_bundle("1.0.0"), lambda _: adapter)
    store = SQLiteDeliveryProjectionStore(tmp_path / "delivery.db")
    dispatcher = DeliveryDispatcher(
        projection=store,
        capability_resolver=registry,
    )
    request = _request(registry.generation)

    result = await dispatcher.dispatch(
        request,
        attempt=1,
        owner_id="worker",
    )

    assert result.disposition is DeliveryDispatchDisposition.UNCERTAIN
    assert result.attempt.receipt is not None
    assert result.attempt.receipt.error_code == "invalid_adapter_receipt"
    assert await store.list_attempts(request.delivery_id) == (result.attempt,)


@pytest.mark.asyncio
async def test_silent_delivery_records_suppression_without_adapter(
    tmp_path: Path,
) -> None:
    registry = GenerationRegistry()
    dispatcher = DeliveryDispatcher(
        projection=SQLiteDeliveryProjectionStore(tmp_path / "delivery.db"),
        capability_resolver=registry,
    )

    result = await dispatcher.dispatch(
        _request(1, mode=DeliveryMode.SILENT, key="silent:key"),
        attempt=1,
        owner_id="worker",
    )

    assert result.disposition is DeliveryDispatchDisposition.SUPPRESSED
    assert result.attempt.receipt is not None
    assert result.attempt.receipt.status is DeliveryStatus.SUPPRESSED


@pytest.mark.asyncio
async def test_adapter_exception_is_uncertain_but_missing_adapter_failed(
    tmp_path: Path,
) -> None:
    registry = GenerationRegistry()
    adapter = _UncertainAdapter("uncertain")
    await registry.activate_bundle(_bundle("1.0.0"), lambda _: adapter)
    dispatcher = DeliveryDispatcher(
        projection=SQLiteDeliveryProjectionStore(
            tmp_path / "uncertain.db",
        ),
        capability_resolver=registry,
    )

    uncertain = await dispatcher.dispatch(
        _request(registry.generation, key="uncertain:key"),
        attempt=1,
        owner_id="worker",
    )

    assert uncertain.disposition is DeliveryDispatchDisposition.UNCERTAIN
    assert uncertain.attempt.receipt is not None
    assert uncertain.attempt.receipt.error_code == "TimeoutError"

    empty_registry = GenerationRegistry()
    missing_dispatcher = DeliveryDispatcher(
        projection=SQLiteDeliveryProjectionStore(tmp_path / "missing.db"),
        capability_resolver=empty_registry,
    )
    missing = await missing_dispatcher.dispatch(
        _request(1, key="missing:key"),
        attempt=1,
        owner_id="worker",
    )

    assert missing.disposition is DeliveryDispatchDisposition.FAILED
    assert missing.attempt.receipt is not None
    assert missing.attempt.receipt.error_code == "LookupError"
