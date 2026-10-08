# -*- coding: utf-8 -*-
"""Tests for Lite hierarchical budget authorization."""

from __future__ import annotations

import pytest

from qwenpaw.kernel import (
    BudgetAllocation,
    BudgetLeaseStatus,
    UsageDelta,
    UsageSnapshot,
)
from qwenpaw.tasks.budget_leases import (
    BudgetAllocationExceededError,
    BudgetLeaseUnavailableError,
    open_root_budget_lease,
)


class _DurableMeter:
    def __init__(
        self,
        initial: UsageSnapshot | None = None,
    ) -> None:
        self.current = initial or UsageSnapshot()
        self.deltas: list[UsageDelta] = []
        self.concurrency = 0

    async def record(
        self,
        delta: UsageDelta,
        *,
        source: str | None = None,
    ) -> UsageSnapshot:
        del source
        self.deltas.append(delta)
        self.current = self.current.add(delta)
        return self.current

    async def snapshot(self) -> UsageSnapshot:
        return self.current

    async def acquire_concurrency(self) -> None:
        self.concurrency += 1

    def release_concurrency(self) -> None:
        self.concurrency -= 1


@pytest.mark.asyncio
async def test_sibling_allocations_cannot_overcommit_parent() -> None:
    root = await open_root_budget_lease(
        _DurableMeter(),
        BudgetAllocation(max_tokens=100),
        owner_id="root",
    )

    first = root.derive(
        BudgetAllocation(max_tokens=60),
        owner_id="child-a",
    )
    with pytest.raises(BudgetAllocationExceededError):
        root.derive(
            BudgetAllocation(max_tokens=50),
            owner_id="child-b",
        )

    first.release()
    replacement = root.derive(
        BudgetAllocation(max_tokens=100),
        owner_id="child-b",
    )
    assert replacement.lease_snapshot().allocation.max_tokens == 100


@pytest.mark.asyncio
async def test_record_updates_child_parent_and_durable_meter() -> None:
    meter = _DurableMeter()
    root = await open_root_budget_lease(
        meter,
        BudgetAllocation(max_tokens=100),
        owner_id="root",
    )
    child = root.derive(
        BudgetAllocation(max_tokens=60),
        owner_id="child",
    )

    result = await child.record(
        UsageDelta(input_tokens=20, output_tokens=10),
        source="model",
    )

    assert result.total_tokens == 30
    assert child.lease_snapshot().usage.total_tokens == 30
    assert root.lease_snapshot().usage.total_tokens == 30
    assert meter.current.total_tokens == 30


@pytest.mark.asyncio
async def test_revoking_root_rejects_descendant_usage() -> None:
    meter = _DurableMeter()
    root = await open_root_budget_lease(
        meter,
        BudgetAllocation(max_tokens=100),
        owner_id="root",
    )
    child = root.derive(BudgetAllocation(), owner_id="child")

    root.revoke()

    assert child.lease_snapshot().status is BudgetLeaseStatus.REVOKED
    with pytest.raises(BudgetLeaseUnavailableError):
        await child.record(UsageDelta(input_tokens=1))
    assert not meter.deltas


@pytest.mark.asyncio
async def test_local_overage_is_durable_before_lease_exhausts() -> None:
    meter = _DurableMeter()
    root = await open_root_budget_lease(
        meter,
        BudgetAllocation(max_tokens=100),
        owner_id="root",
    )
    child = root.derive(
        BudgetAllocation(max_tokens=5),
        owner_id="child",
    )

    with pytest.raises(BudgetAllocationExceededError):
        await child.record(UsageDelta(input_tokens=6))

    assert meter.current.total_tokens == 6
    assert child.lease_snapshot().status is BudgetLeaseStatus.EXHAUSTED


@pytest.mark.asyncio
async def test_reopened_root_uses_durable_usage_for_admission() -> None:
    meter = _DurableMeter(UsageSnapshot(input_tokens=80))
    root = await open_root_budget_lease(
        meter,
        BudgetAllocation(max_tokens=100),
        owner_id="root",
    )

    with pytest.raises(BudgetAllocationExceededError):
        root.derive(
            BudgetAllocation(max_tokens=30),
            owner_id="child",
        )

    child = root.derive(
        BudgetAllocation(max_tokens=20),
        owner_id="child",
    )
    assert child.lease_snapshot().allocation.max_tokens == 20
