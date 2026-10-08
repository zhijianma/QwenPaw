# -*- coding: utf-8 -*-
"""Revocable runtime authorization layered over durable usage facts."""

from __future__ import annotations

from enum import Enum
from typing import Protocol, runtime_checkable
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field

from .models import (
    ExecutionBudget,
    KernelModel,
    NonEmptyStr,
    UsageDelta,
    UsageMeter,
    UsageSnapshot,
    utc_now,
)


class BudgetLeaseStatus(str, Enum):
    """Admission state independent from durable resource usage."""

    ACTIVE = "active"
    EXHAUSTED = "exhausted"
    REVOKED = "revoked"
    RELEASED = "released"


class BudgetAllocation(KernelModel):
    """Optional child ceilings reserved from one parent authorization."""

    max_tokens: int | None = Field(default=None, ge=0)
    max_cost_micros: int | None = Field(default=None, ge=0)
    max_tool_calls: int | None = Field(default=None, ge=0)
    max_concurrency: int | None = Field(default=None, gt=0)

    @classmethod
    def from_execution_budget(
        cls,
        budget: ExecutionBudget,
    ) -> "BudgetAllocation":
        """Project consumable ceilings from an immutable Task contract."""
        return cls(
            max_tokens=budget.max_tokens,
            max_cost_micros=budget.max_cost_micros,
            max_tool_calls=budget.max_tool_calls,
            max_concurrency=budget.max_concurrency,
        )


class BudgetLeaseSnapshot(KernelModel):
    """Content-safe state for one root or derived authorization."""

    lease_id: UUID = Field(default_factory=uuid4)
    root_lease_id: UUID
    parent_lease_id: UUID | None = None
    task_id: UUID | None = None
    run_id: UUID | None = None
    owner_id: NonEmptyStr
    allocation: BudgetAllocation = Field(default_factory=BudgetAllocation)
    usage: UsageSnapshot = Field(default_factory=UsageSnapshot)
    status: BudgetLeaseStatus = BudgetLeaseStatus.ACTIVE
    revision: int = Field(default=1, ge=1)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)


@runtime_checkable
class BudgetLease(UsageMeter, Protocol):
    """Revocable, derivable admission capability over one UsageMeter."""

    @property
    def lease_id(self) -> UUID:
        """Return this authorization identity."""

    def derive(
        self,
        allocation: BudgetAllocation,
        *,
        owner_id: str,
    ) -> "BudgetLease":
        """Reserve a bounded child or create a shared-root child."""

    def lease_snapshot(self) -> BudgetLeaseSnapshot:
        """Return the current local admission and usage state."""

    def revoke(self) -> None:
        """Reject future work for this lease and every descendant."""

    def release(self) -> None:
        """Release unused reservation without erasing durable usage."""

    async def record(
        self,
        delta: UsageDelta,
        *,
        source: str | None = None,
    ) -> UsageSnapshot:
        """Persist actual usage and then enforce local and root ceilings."""


__all__ = [
    "BudgetAllocation",
    "BudgetLease",
    "BudgetLeaseSnapshot",
    "BudgetLeaseStatus",
]
