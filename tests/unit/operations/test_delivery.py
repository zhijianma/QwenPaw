# -*- coding: utf-8 -*-
"""End-to-end operational fact projection through durable Delivery."""

import asyncio
from pathlib import Path

import pytest

from qwenpaw.delivery import (
    DeliveryDispatcher,
    SQLiteDeliveryProjectionStore,
    SystemInboxDeliveryAdapter,
)
from qwenpaw.inbox import SQLiteInboxProjectionStore
from qwenpaw.kernel import (
    CapabilityBundle,
    CapabilityContribution,
    CapabilityProviderKind,
    OperationalEvent,
)
from qwenpaw.operations import (
    OperationalDeliveryService,
    SQLiteOperationalEventStore,
)
from qwenpaw.plugins.generations import GenerationRegistry


class _SlowInboxAdapter(SystemInboxDeliveryAdapter):
    async def deliver(self, request, *, attempt):
        await asyncio.sleep(0.05)
        return await super().deliver(request, attempt=attempt)


async def _service(tmp_path: Path, *, slow: bool = False):
    registry = GenerationRegistry()
    bundle = CapabilityBundle(
        provider_id="qwenpaw.system.tasks",
        provider_kind=CapabilityProviderKind.SYSTEM,
        version="test",
        contributions=(
            CapabilityContribution(
                contribution_id="inbox-delivery",
                slot="delivery.adapter",
                entrypoint="qwenpaw.delivery:SystemInboxDeliveryAdapter",
            ),
        ),
    )
    adapter = _SlowInboxAdapter() if slow else SystemInboxDeliveryAdapter()
    await registry.activate_bundle(
        bundle,
        lambda _declaration: adapter,
    )
    inbox = SQLiteInboxProjectionStore(tmp_path / "inbox.db")
    return (
        OperationalDeliveryService(
            events=SQLiteOperationalEventStore(
                tmp_path / "operations.db",
            ),
            dispatcher=DeliveryDispatcher(
                projection=SQLiteDeliveryProjectionStore(
                    tmp_path / "delivery.db",
                ),
                capability_resolver=registry,
            ),
            inbox=inbox,
        ),
        registry,
        inbox,
    )


def _event(generation: int, *, title: str = "Auto Sync completed"):
    return OperationalEvent(
        agent_id="default",
        producer_id="qwenpaw.system.skills",
        event_type="skill.auto_sync",
        idempotency_key="sync:result-digest",
        source_type="skill_autoupdate",
        source_id="",
        status="success",
        severity="info",
        title=title,
        body="s1 → default",
        payload={"synced": ["s1"]},
        registry_generation=generation,
    )


@pytest.mark.asyncio
async def test_operational_fact_projects_with_source_metadata(
    tmp_path: Path,
) -> None:
    """A committed Skill fact preserves its released Inbox identity."""
    service, registry, inbox = await _service(tmp_path)

    result = await service.publish(_event(registry.generation))

    item = result.inbox_item
    assert item.source_event_id == result.event.event_id
    assert item.source_type == "skill_autoupdate"
    assert item.event_type == "skill.auto_sync"
    assert item.title == "Auto Sync completed"
    assert item.summary == "s1 → default"
    assert item.task_id is None
    assert await inbox.list_items(agent_id="default") == (item,)


@pytest.mark.asyncio
async def test_operational_delivery_replay_is_idempotent(
    tmp_path: Path,
) -> None:
    """Replaying one source fact does not duplicate Inbox state."""
    service, registry, inbox = await _service(tmp_path)
    event = _event(registry.generation)

    first = await service.publish(event)
    replay = await service.publish(event)

    assert replay.event == first.event
    assert replay.inbox_item == first.inbox_item
    assert len(await inbox.list_items(agent_id="default")) == 1


@pytest.mark.asyncio
async def test_concurrent_replay_waits_for_terminal_delivery(
    tmp_path: Path,
) -> None:
    """A concurrent publisher observes the first owner's receipt."""
    service, registry, inbox = await _service(tmp_path, slow=True)
    event = _event(registry.generation)

    first, replay = await asyncio.gather(
        service.publish(event),
        service.publish(event),
    )

    assert replay.event == first.event
    assert replay.delivery.attempt.receipt == first.delivery.attempt.receipt
    assert replay.inbox_item == first.inbox_item
    assert len(await inbox.list_items(agent_id="default")) == 1


@pytest.mark.asyncio
async def test_source_conflict_stops_before_second_delivery(
    tmp_path: Path,
) -> None:
    """Conflicting producer content cannot overwrite a delivered fact."""
    service, registry, inbox = await _service(tmp_path)
    event = _event(registry.generation)
    await service.publish(event)

    with pytest.raises(RuntimeError, match="conflicts"):
        await service.publish(
            event.model_copy(update={"title": "Different"}),
        )

    assert len(await inbox.list_items(agent_id="default")) == 1
