# -*- coding: utf-8 -*-
"""Lite revocable budget leases over the durable Task usage ledger."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from ..kernel import (
    BudgetAllocation,
    BudgetLeaseSnapshot,
    BudgetLeaseStatus,
    UsageDelta,
    UsageMeter,
    UsageSnapshot,
)
from ..kernel.models import utc_now
from .usage import (
    UsageAccountingUnavailableError,
    UsageBudgetExceededError,
)


class BudgetLeaseUnavailableError(RuntimeError):
    """Raised when a revoked, released, or exhausted lease is used."""


class BudgetAllocationExceededError(RuntimeError):
    """Raised after actual usage exceeds a derived lease allocation."""


@dataclass(slots=True)
class _LeaseState:
    lease_id: UUID
    root_lease_id: UUID
    parent_lease_id: UUID | None
    task_id: UUID | None
    run_id: UUID | None
    owner_id: str
    allocation: BudgetAllocation
    usage: UsageSnapshot
    status: BudgetLeaseStatus = BudgetLeaseStatus.ACTIVE
    revision: int = 1
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    children: set[UUID] = field(default_factory=set)
    semaphore: asyncio.Semaphore | None = None
    concurrency_acquired: int = 0


class _BudgetLeaseTree:
    def __init__(
        self,
        meter: UsageMeter,
        root: _LeaseState,
    ) -> None:
        self.meter = meter
        self.root_lease_id = root.lease_id
        self.states = {root.lease_id: root}
        self.lock = threading.RLock()
        self.record_lock = asyncio.Lock()

    def state(self, lease_id: UUID) -> _LeaseState:
        state = self.states.get(lease_id)
        if state is None:
            raise BudgetLeaseUnavailableError("budget lease is unavailable")
        return state

    def assert_active(self, state: _LeaseState) -> None:
        if state.status is not BudgetLeaseStatus.ACTIVE:
            raise BudgetLeaseUnavailableError(
                f"budget lease is {state.status.value}",
            )

    @staticmethod
    def resource_usage(
        snapshot: UsageSnapshot,
        resource: str,
    ) -> int:
        if resource == "tokens":
            return snapshot.total_tokens
        return int(getattr(snapshot, resource))

    @staticmethod
    def resource_limit(
        allocation: BudgetAllocation,
        resource: str,
    ) -> int | None:
        field_name = {
            "tokens": "max_tokens",
            "cost_micros": "max_cost_micros",
            "tool_calls": "max_tool_calls",
        }[resource]
        return getattr(allocation, field_name)

    def remaining_reservations(
        self,
        state: _LeaseState,
        resource: str,
    ) -> int:
        reserved = 0
        for child_id in state.children:
            child = self.states[child_id]
            if child.status is not BudgetLeaseStatus.ACTIVE:
                continue
            limit = self.resource_limit(child.allocation, resource)
            if limit is None:
                continue
            used = self.resource_usage(child.usage, resource)
            reserved += max(limit - used, 0)
        return reserved

    def allocation_failure(self, state: _LeaseState) -> str | None:
        if (
            state.allocation.max_cost_micros is not None
            and state.usage.cost_unknown
        ):
            return "cost_unknown"
        for resource in ("tokens", "cost_micros", "tool_calls"):
            limit = self.resource_limit(state.allocation, resource)
            if limit is None:
                continue
            committed = self.resource_usage(state.usage, resource)
            committed += self.remaining_reservations(state, resource)
            if committed > limit:
                return resource
        return None

    def cascade(
        self,
        state: _LeaseState,
        status: BudgetLeaseStatus,
    ) -> None:
        if state.status is not BudgetLeaseStatus.ACTIVE:
            return
        state.status = status
        state.revision += 1
        state.updated_at = utc_now()
        for child_id in tuple(state.children):
            self.cascade(self.states[child_id], status)


class LiteBudgetLease:
    """Process-local authorization whose usage remains Task-ledger durable."""

    def __init__(self, tree: _BudgetLeaseTree, lease_id: UUID) -> None:
        self._tree = tree
        self._lease_id = lease_id

    @property
    def lease_id(self) -> UUID:
        """Return this authorization identity."""
        return self._lease_id

    def derive(
        self,
        allocation: BudgetAllocation,
        *,
        owner_id: str,
    ) -> "LiteBudgetLease":
        """Reserve a bounded child or create a shared-root child."""
        if not owner_id.strip():
            raise ValueError("budget lease owner cannot be empty")
        with self._tree.lock:
            parent = self._tree.state(self._lease_id)
            self._tree.assert_active(parent)
            for resource in ("tokens", "cost_micros", "tool_calls"):
                requested = self._tree.resource_limit(
                    allocation,
                    resource,
                )
                parent_limit = self._tree.resource_limit(
                    parent.allocation,
                    resource,
                )
                if requested is None or parent_limit is None:
                    continue
                committed = self._tree.resource_usage(
                    parent.usage,
                    resource,
                )
                committed += self._tree.remaining_reservations(
                    parent,
                    resource,
                )
                if committed + requested > parent_limit:
                    raise BudgetAllocationExceededError(
                        f"insufficient {resource} allocation",
                    )
            if (
                allocation.max_concurrency is not None
                and parent.allocation.max_concurrency is not None
            ):
                reserved_concurrency = sum(
                    self._tree.states[child_id].allocation.max_concurrency
                    or 0
                    for child_id in parent.children
                    if self._tree.states[child_id].status
                    is BudgetLeaseStatus.ACTIVE
                )
                if (
                    reserved_concurrency + allocation.max_concurrency
                    > parent.allocation.max_concurrency
                ):
                    raise BudgetAllocationExceededError(
                        "insufficient concurrency allocation",
                    )
            lease_id = uuid4()
            child = _LeaseState(
                lease_id=lease_id,
                root_lease_id=parent.root_lease_id,
                parent_lease_id=parent.lease_id,
                task_id=parent.task_id,
                run_id=parent.run_id,
                owner_id=owner_id,
                allocation=allocation,
                usage=UsageSnapshot(),
                semaphore=(
                    asyncio.Semaphore(allocation.max_concurrency)
                    if allocation.max_concurrency is not None
                    else None
                ),
            )
            self._tree.states[lease_id] = child
            parent.children.add(lease_id)
            parent.revision += 1
            parent.updated_at = utc_now()
            return LiteBudgetLease(self._tree, lease_id)

    def lease_snapshot(self) -> BudgetLeaseSnapshot:
        """Return the current local admission and usage state."""
        with self._tree.lock:
            state = self._tree.state(self._lease_id)
            return BudgetLeaseSnapshot(
                lease_id=state.lease_id,
                root_lease_id=state.root_lease_id,
                parent_lease_id=state.parent_lease_id,
                task_id=state.task_id,
                run_id=state.run_id,
                owner_id=state.owner_id,
                allocation=state.allocation,
                usage=state.usage,
                status=state.status,
                revision=state.revision,
                created_at=state.created_at,
                updated_at=state.updated_at,
            )

    def revoke(self) -> None:
        """Reject future work for this lease and every descendant."""
        with self._tree.lock:
            self._tree.cascade(
                self._tree.state(self._lease_id),
                BudgetLeaseStatus.REVOKED,
            )

    def release(self) -> None:
        """Release unused reservation without erasing durable usage."""
        with self._tree.lock:
            state = self._tree.state(self._lease_id)
            self._tree.cascade(state, BudgetLeaseStatus.RELEASED)
            if state.parent_lease_id is not None:
                parent = self._tree.state(state.parent_lease_id)
                parent.children.discard(state.lease_id)
                parent.revision += 1
                parent.updated_at = utc_now()

    async def record(
        self,
        delta: UsageDelta,
        *,
        source: str | None = None,
    ) -> UsageSnapshot:
        """Persist actual usage and then enforce local and root ceilings."""
        async with self._tree.record_lock:
            with self._tree.lock:
                state = self._tree.state(self._lease_id)
                self._tree.assert_active(state)
            root_error: Exception | None = None
            root_snapshot: UsageSnapshot | None = None
            try:
                root_snapshot = await self._tree.meter.record(
                    delta,
                    source=source,
                )
            except (
                UsageAccountingUnavailableError,
                UsageBudgetExceededError,
            ) as exc:
                root_error = exc
            with self._tree.lock:
                state = self._tree.state(self._lease_id)
                current: _LeaseState | None = state
                lineage = []
                while current is not None:
                    lineage.append(current)
                    current = (
                        self._tree.states.get(current.parent_lease_id)
                        if current.parent_lease_id is not None
                        else None
                    )
                for item in lineage:
                    item.usage = item.usage.add(delta)
                    item.revision += 1
                    item.updated_at = utc_now()
                local_failure = self._tree.allocation_failure(state)
                if local_failure is not None:
                    self._tree.cascade(
                        state,
                        BudgetLeaseStatus.EXHAUSTED,
                    )
                if root_error is not None:
                    root = self._tree.state(self._tree.root_lease_id)
                    self._tree.cascade(
                        root,
                        BudgetLeaseStatus.EXHAUSTED,
                    )
            if root_error is not None:
                raise root_error
            if local_failure is not None:
                raise BudgetAllocationExceededError(
                    f"budget lease exhausted: {local_failure}",
                )
            assert root_snapshot is not None
            return root_snapshot

    async def snapshot(self) -> UsageSnapshot:
        """Return durable cumulative usage for backward compatibility."""
        return await self._tree.meter.snapshot()

    async def assert_within_budget(self) -> UsageSnapshot:
        """Recheck both lease state and the durable root meter."""
        with self._tree.lock:
            self._tree.assert_active(self._tree.state(self._lease_id))
        validator = getattr(self._tree.meter, "assert_within_budget", None)
        if callable(validator):
            return await validator()
        return await self._tree.meter.snapshot()

    async def acquire_concurrency(self) -> None:
        """Acquire local allocation before the root Task semaphore."""
        with self._tree.lock:
            state = self._tree.state(self._lease_id)
            self._tree.assert_active(state)
            semaphore = state.semaphore
        if semaphore is not None:
            await semaphore.acquire()
        try:
            await self._tree.meter.acquire_concurrency()
        except BaseException:
            if semaphore is not None:
                semaphore.release()
            raise
        with self._tree.lock:
            state = self._tree.state(self._lease_id)
            try:
                self._tree.assert_active(state)
            except BaseException:
                self._tree.meter.release_concurrency()
                if semaphore is not None:
                    semaphore.release()
                raise
            state.concurrency_acquired += 1

    def release_concurrency(self) -> None:
        """Release one local and root concurrency slot."""
        with self._tree.lock:
            state = self._tree.state(self._lease_id)
            if state.concurrency_acquired <= 0:
                raise RuntimeError(
                    "budget lease concurrency release without acquire",
                )
            state.concurrency_acquired -= 1
            semaphore = state.semaphore
        self._tree.meter.release_concurrency()
        if semaphore is not None:
            semaphore.release()


async def open_root_budget_lease(
    meter: UsageMeter,
    allocation: BudgetAllocation,
    *,
    owner_id: str,
    task_id: UUID | None = None,
    run_id: UUID | None = None,
) -> LiteBudgetLease:
    """Create a root authorization seeded from durable cumulative usage."""
    if not owner_id.strip():
        raise ValueError("budget lease owner cannot be empty")
    lease_id = uuid4()
    root = _LeaseState(
        lease_id=lease_id,
        root_lease_id=lease_id,
        parent_lease_id=None,
        task_id=task_id,
        run_id=run_id,
        owner_id=owner_id,
        allocation=allocation,
        usage=await meter.snapshot(),
    )
    return LiteBudgetLease(_BudgetLeaseTree(meter, root), lease_id)


__all__ = [
    "BudgetAllocationExceededError",
    "BudgetLeaseUnavailableError",
    "LiteBudgetLease",
    "open_root_budget_lease",
]
