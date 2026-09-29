# -*- coding: utf-8 -*-
"""Tests for the Task capability catalog application boundary."""

import pytest

from qwenpaw.kernel.models import CapabilityDescriptor
from qwenpaw.tasks.capability_application import (
    TaskCapabilityApplicationService,
)


class _Lease:
    generation = 7

    def __init__(self, descriptor: CapabilityDescriptor) -> None:
        self._descriptor = descriptor
        self.closed = False
        self.requested_slot: str | None = None

    def descriptors(self, slot: str | None = None):
        self.requested_slot = slot
        return (self._descriptor,)

    async def close(self) -> None:
        self.closed = True


class _Resolver:
    def __init__(self, lease: _Lease) -> None:
        self.lease = lease

    async def pin(self, generation: int | None = None) -> _Lease:
        assert generation is None
        return self.lease


@pytest.mark.asyncio
async def test_lists_one_generation_and_releases_its_lease() -> None:
    descriptor = CapabilityDescriptor(
        capability_id="tests.strategy",
        slot="strategy",
        provider_id="tests",
        version="1.0.0",
    )
    lease = _Lease(descriptor)
    application = TaskCapabilityApplicationService(_Resolver(lease))

    catalog = await application.list(slot="strategy")

    assert catalog.registry_generation == 7
    assert catalog.items == (descriptor,)
    assert lease.requested_slot == "strategy"
    assert lease.closed is True
