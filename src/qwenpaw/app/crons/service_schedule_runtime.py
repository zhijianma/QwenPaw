# -*- coding: utf-8 -*-
"""Durable catalog adapter for service-contributed time callbacks."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from uuid import uuid4

from ...kernel import ScheduleDefinition, ScheduleTrigger, ScheduleWorkKind
from ...scheduling import (
    ScheduleOccurrenceHandling,
    ServiceScheduleDispatchDisposition,
)
from .contracts import ServiceCronJob
from .scheduled_task_runtime import LiteScheduledTaskRuntime

SERVICE_SCHEDULE_RUNNER_ID = "qwenpaw.system.crons.service-callback"


def service_schedule_id(agent_id: str, source: str, key: str) -> str:
    """Return one stable, namespaced service schedule identity."""
    digest = hashlib.sha256(
        f"{agent_id}:{source}:{key}".encode("utf-8"),
    ).hexdigest()[:20]
    return f"qwenpaw.system.service-cron.job-{digest}"


class LiteServiceScheduleRuntime:
    """Synchronize service callbacks without materializing fake Tasks."""

    def __init__(self, workspace: Any) -> None:
        self._workspace = workspace
        self._scheduled = LiteScheduledTaskRuntime(workspace)

    async def synchronize(
        self,
        *,
        source: str,
        declaration: ServiceCronJob,
        timezone: str,
    ) -> str:
        """Upsert one service declaration and return its schedule ID."""
        schedule_id = service_schedule_id(
            self._workspace.agent_id,
            source,
            declaration.key,
        )
        await self._scheduled.upsert_definition(
            ScheduleDefinition(
                schedule_id=schedule_id,
                agent_id=self._workspace.agent_id,
                name=f"{source}:{declaration.key}",
                objective="Execute a registered workspace service callback.",
                trigger=ScheduleTrigger(
                    kind="cron",
                    cron=declaration.cron,
                    timezone=timezone,
                    jitter_seconds=declaration.jitter_seconds,
                    jitter_seed=(
                        schedule_id if declaration.jitter_seconds > 0 else None
                    ),
                ),
                work_kind=ScheduleWorkKind.SERVICE,
                runner_id=SERVICE_SCHEDULE_RUNNER_ID,
                misfire_grace_seconds=(declaration.misfire_grace_seconds),
                metadata={
                    "source": "service_cron",
                    "service_source": source,
                    "service_key": declaration.key,
                },
            ),
        )
        return schedule_id

    async def remove(self, *, source: str, key: str) -> bool:
        """Remove one service declaration and cursor."""
        return await self._scheduled.remove_definition(
            agent_id=self._workspace.agent_id,
            schedule_id=service_schedule_id(
                self._workspace.agent_id,
                source,
                key,
            ),
        )

    async def prune(
        self,
        *,
        source: str,
        active_keys: set[str],
    ) -> tuple[str, ...]:
        """Remove declarations no longer produced by one service source."""
        removed: list[str] = []
        definitions = await self._scheduled.list_definitions(
            agent_id=self._workspace.agent_id,
        )
        for definition in definitions:
            metadata = definition.metadata
            if (
                definition.work_kind is not ScheduleWorkKind.SERVICE
                or metadata.get("service_source") != source
            ):
                continue
            key = metadata.get("service_key")
            if not isinstance(key, str) or key in active_keys:
                continue
            if await self._scheduled.remove_definition(
                agent_id=self._workspace.agent_id,
                schedule_id=definition.schedule_id,
            ):
                removed.append(definition.schedule_id)
        return tuple(sorted(removed))

    async def execute(
        self,
        *,
        definition: ScheduleDefinition,
        declaration: ServiceCronJob,
        scheduled_for: datetime,
    ) -> ScheduleOccurrenceHandling:
        """Fence one callback through a durable Fire and renewable lease."""
        result = await self._scheduled.execute_service_callback(
            definition,
            scheduled_for=scheduled_for,
            idempotency_key=(
                f"scheduled:{definition.schedule_id}:"
                f"{scheduled_for.isoformat()}"
            ),
            owner_id=(f"service:{self._workspace.agent_id}:{uuid4()}"),
            callback=declaration.callback,
        )
        if (
            result.disposition
            is ServiceScheduleDispatchDisposition.OWNED_ELSEWHERE
        ):
            return ScheduleOccurrenceHandling.RETRY
        return ScheduleOccurrenceHandling.HANDLED


__all__ = [
    "LiteServiceScheduleRuntime",
    "SERVICE_SCHEDULE_RUNNER_ID",
    "service_schedule_id",
]
