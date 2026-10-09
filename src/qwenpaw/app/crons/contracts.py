# -*- coding: utf-8 -*-
"""Backend-neutral declarations for service-contributed cron jobs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from ...kernel import ScheduleDefinition
from ...scheduling import (
    ScheduleOccurrenceHandling,
    ScheduleTriggerTickReport,
)

from .models import CronJobSpec, CronRuntimeDecision

ScheduleOccurrenceExecutor = Callable[
    [ScheduleDefinition, datetime],
    Awaitable[ScheduleOccurrenceHandling | None],
]
# Compatibility name retained while callers migrate to the shared contract.
CronOccurrenceExecutor = ScheduleOccurrenceExecutor


@dataclass(frozen=True)
class ServiceCronJob:
    """A cron job declared by a workspace service.

    The declaration intentionally contains no APScheduler types. Services own
    job semantics and configuration; :class:`CronManager` owns scheduling.
    """

    key: str
    cron: str
    callback: Callable[[], Awaitable[None]]
    misfire_grace_seconds: int = 600
    jitter_seconds: int = 0


class ServiceScheduleRuntime(Protocol):
    """Durable catalog used for service-contributed callback schedules."""

    async def synchronize(
        self,
        *,
        source: str,
        declaration: ServiceCronJob,
        timezone: str,
    ) -> str:
        """Reconcile one service declaration and return its schedule ID."""

    async def remove(self, *, source: str, key: str) -> bool:
        """Remove one service declaration and cursor."""

    async def prune(
        self,
        *,
        source: str,
        active_keys: set[str],
    ) -> tuple[str, ...]:
        """Remove declarations absent from the current source snapshot."""

    async def execute(
        self,
        *,
        definition: ScheduleDefinition,
        declaration: ServiceCronJob,
        scheduled_for: datetime,
    ) -> ScheduleOccurrenceHandling:
        """Fence and run one service callback occurrence."""


class CronTaskRuntime(Protocol):
    """Optional durable runtime used during the Cron migration."""

    def decision(self, job: CronJobSpec) -> CronRuntimeDecision:
        """Explain whether the job can migrate without semantic loss."""

    def supports(self, job: CronJobSpec) -> bool:
        """Compatibility alias for older host integrations."""

    async def synchronize(self, job: CronJobSpec) -> datetime | None:
        """Reconcile one declaration into the durable schedule catalog."""

    async def remove(self, job: CronJobSpec) -> bool:
        """Remove one declaration from the durable schedule catalog."""

    async def execute(
        self,
        job: CronJobSpec,
        *,
        trigger: Literal["scheduled", "manual"],
        scheduled_for: datetime,
    ) -> dict[str, Any]:
        """Wait through Task and Delivery terminal accounting."""

    async def run_due(
        self,
        *,
        now: datetime,
        execute: ScheduleOccurrenceExecutor,
    ) -> ScheduleTriggerTickReport:
        """Consume one durable trigger batch through manager accounting."""


@dataclass(frozen=True, slots=True)
class HeartbeatExecutionRequest:
    """Validated compatibility inputs for one Heartbeat occurrence."""

    query_text: str
    every: str
    target: str
    timeout_seconds: int
    trigger: Literal["scheduled", "manual"]
    scheduled_for: datetime
    channel: str
    user_id: str
    transport_context: str


class HeartbeatTaskRuntime(Protocol):
    """Durable runtime used by scheduled and manual Heartbeat triggers."""

    async def execute(
        self,
        request: HeartbeatExecutionRequest,
    ) -> dict[str, Any]:
        """Run one Heartbeat through Schedule, Task, and Delivery."""

    async def synchronize(
        self,
        request: HeartbeatExecutionRequest,
    ) -> datetime | None:
        """Reconcile the current Heartbeat declaration and cursor."""

    async def remove(self) -> bool:
        """Remove the current Heartbeat declaration and cursor."""

    def schedule_id(self) -> str:
        """Return the stable per-Agent Heartbeat schedule identity."""
