# -*- coding: utf-8 -*-
"""Durable Lite usage accounting for Execution Contract budgets."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from uuid import UUID

from ..kernel.events import ExecutionEvent
from ..kernel.models import (
    ExecutionBudget,
    RunnerSignal,
    UsageDelta,
    UsageSnapshot,
)
from .service import TaskService

_USAGE_EVENT_TYPE = "usage.recorded"
_EVENT_PAGE_SIZE = 200


def usage_snapshot_from_events(
    events: Iterable[ExecutionEvent],
    *,
    initial: UsageSnapshot | None = None,
) -> UsageSnapshot:
    """Fold canonical usage events into one cumulative projection."""
    snapshot = initial or UsageSnapshot()
    for event in events:
        if event.event_type != _USAGE_EVENT_TYPE:
            continue
        raw_delta = event.payload.get("delta")
        if isinstance(raw_delta, dict):
            snapshot = snapshot.add(UsageDelta.model_validate(raw_delta))
    return snapshot


async def load_task_usage_snapshot(
    service: TaskService,
    task_id: UUID,
    *,
    through_sequence: int | None = None,
) -> UsageSnapshot:
    """Replay all usage through one optional projection watermark."""
    snapshot = UsageSnapshot()
    after_sequence = 0
    while True:
        events = await service.list_events(
            task_id,
            after_sequence=after_sequence,
            limit=_EVENT_PAGE_SIZE,
        )
        bounded_events = tuple(
            event
            for event in events
            if through_sequence is None or event.sequence <= through_sequence
        )
        snapshot = usage_snapshot_from_events(
            bounded_events,
            initial=snapshot,
        )
        if len(events) < _EVENT_PAGE_SIZE:
            return snapshot
        if through_sequence is not None:
            if events[-1].sequence >= through_sequence:
                return snapshot
        after_sequence = events[-1].sequence


class UsageBudgetExceededError(RuntimeError):
    """Raised after durable usage crosses one Execution Contract ceiling."""

    def __init__(self, resource: str, limit: int, observed: int) -> None:
        self.resource = resource
        self.limit = limit
        self.observed = observed
        super().__init__(
            f"{resource} budget exceeded: {observed} > {limit}",
        )


class UsageAccountingUnavailableError(RuntimeError):
    """Raised after unknown cost is recorded under a hard cost ceiling."""

    def __init__(self) -> None:
        super().__init__(
            "cost budget cannot be enforced because provider cost is unknown",
        )


class TaskUsageMeter:
    """Reconstruct and append usage through the Task execution ledger."""

    def __init__(
        self,
        service: TaskService,
        task_id: UUID,
        run_id: UUID,
        budget: ExecutionBudget,
    ) -> None:
        self._service = service
        self._task_id = task_id
        self._run_id = run_id
        self._budget = budget
        self._lock = asyncio.Lock()
        self._concurrency = asyncio.Semaphore(budget.max_concurrency)
        self._concurrency_acquired = 0

    async def record(
        self,
        delta: UsageDelta,
        *,
        source: str | None = None,
    ) -> UsageSnapshot:
        """Persist actual usage before reporting a crossed hard limit."""
        async with self._lock:
            current = await self._snapshot_unlocked()
            updated = current.add(delta)
            await self._service.record_runner_signal(
                self._task_id,
                self._run_id,
                RunnerSignal(
                    event_type=_USAGE_EVENT_TYPE,
                    source=source,
                    payload={
                        "delta": delta.model_dump(mode="json"),
                        "total": updated.model_dump(mode="json"),
                    },
                ),
            )
            self._validate(updated)
            return updated

    async def snapshot(self) -> UsageSnapshot:
        """Return usage accumulated across current and prior attempts."""
        async with self._lock:
            return await self._snapshot_unlocked()

    async def assert_within_budget(self) -> UsageSnapshot:
        """Recheck shared descendant usage before root completion."""
        async with self._lock:
            snapshot = await self._snapshot_unlocked()
            self._validate(snapshot)
            return snapshot

    async def acquire_concurrency(self) -> None:
        """Wait for one live-operation slot from the contract budget."""
        await self._concurrency.acquire()
        self._concurrency_acquired += 1

    def release_concurrency(self) -> None:
        """Release one live-operation slot without exceeding capacity."""
        if self._concurrency_acquired <= 0:
            raise RuntimeError("usage concurrency release without acquire")
        self._concurrency_acquired -= 1
        self._concurrency.release()

    async def _snapshot_unlocked(self) -> UsageSnapshot:
        return await load_task_usage_snapshot(
            self._service,
            self._task_id,
        )

    def _validate(self, snapshot: UsageSnapshot) -> None:
        if self._budget.max_cost_micros is not None and snapshot.cost_unknown:
            raise UsageAccountingUnavailableError()
        limits = (
            ("tokens", self._budget.max_tokens, snapshot.total_tokens),
            (
                "cost_micros",
                self._budget.max_cost_micros,
                snapshot.cost_micros,
            ),
            (
                "tool_calls",
                self._budget.max_tool_calls,
                snapshot.tool_calls,
            ),
        )
        for resource, limit, observed in limits:
            if limit is not None and observed > limit:
                raise UsageBudgetExceededError(
                    resource,
                    limit,
                    observed,
                )


__all__ = [
    "TaskUsageMeter",
    "UsageAccountingUnavailableError",
    "UsageBudgetExceededError",
    "load_task_usage_snapshot",
    "usage_snapshot_from_events",
]
