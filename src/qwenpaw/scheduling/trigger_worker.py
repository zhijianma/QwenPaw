# -*- coding: utf-8 -*-
"""Framework-neutral worker for durable Schedule trigger cursors."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from ..kernel import (
    ScheduleCursorConflictError,
    ScheduleDefinition,
    ScheduleTriggerCursor,
    ScheduleTriggerCursorStore,
    SchedulerPort,
)
from .triggering import (
    first_schedule_fire_at,
    next_schedule_fire_after,
    next_schedule_fire_at,
    schedule_definition_hash,
)


class ScheduleOccurrenceHandling(str, Enum):
    """Whether one observed occurrence may advance durable progress."""

    HANDLED = "handled"
    RETRY = "retry"


ScheduleOccurrenceHandler = Callable[
    [ScheduleDefinition, datetime],
    Awaitable[ScheduleOccurrenceHandling | None],
]


class ScheduleTriggerDisposition(str, Enum):
    """Content-safe result for one due cursor observation."""

    DISPATCHED = "dispatched"
    MISFIRED = "misfired"
    RETRY_PENDING = "retry_pending"
    RECONCILED = "reconciled"
    REMOVED = "removed"
    RACE_LOST = "race_lost"


@dataclass(frozen=True, slots=True)
class ScheduleTriggerOutcome:
    """One durable cursor decision without handler error content."""

    schedule_id: str
    scheduled_for: datetime
    disposition: ScheduleTriggerDisposition
    next_fire_at: datetime | None = None
    retry_not_before: datetime | None = None


@dataclass(frozen=True, slots=True)
class ScheduleTriggerTickReport:
    """Ordered results for one bounded worker tick."""

    outcomes: tuple[ScheduleTriggerOutcome, ...]


class DurableScheduleTriggerWorker:
    """Consume due trigger cursors while preserving retryable progress."""

    def __init__(
        self,
        *,
        catalog: SchedulerPort,
        cursors: ScheduleTriggerCursorStore,
        batch_size: int = 100,
        minimum_retry_delay_seconds: float = 1.0,
    ) -> None:
        if batch_size < 1 or batch_size > 1000:
            raise ValueError(
                "schedule trigger batch size must be between 1 and 1000",
            )
        self._catalog = catalog
        self._cursors = cursors
        self._batch_size = batch_size
        if minimum_retry_delay_seconds <= 0:
            raise ValueError("minimum retry delay must be positive")
        self._minimum_retry_delay_seconds = minimum_retry_delay_seconds

    async def tick(
        self,
        *,
        agent_id: str,
        now: datetime,
        handle: ScheduleOccurrenceHandler,
    ) -> ScheduleTriggerTickReport:
        """Process one bounded due batch for exactly one Agent."""
        current = self._utc(now)
        definitions = {
            item.schedule_id: item
            for item in await self._catalog.list_definitions(
                agent_id=agent_id,
            )
        }
        due = await self._cursors.list_due_cursors(
            agent_id=agent_id,
            now=current,
            limit=self._batch_size,
        )
        outcomes = await asyncio.gather(
            *(
                self._consume(
                    cursor=cursor,
                    definition=definitions.get(cursor.schedule_id),
                    now=current,
                    handle=handle,
                )
                for cursor in due
            ),
        )
        return ScheduleTriggerTickReport(outcomes=tuple(outcomes))

    async def _consume(
        self,
        *,
        cursor: ScheduleTriggerCursor,
        definition: ScheduleDefinition | None,
        now: datetime,
        handle: ScheduleOccurrenceHandler,
    ) -> ScheduleTriggerOutcome:
        scheduled_for = cursor.next_fire_at
        assert scheduled_for is not None
        if definition is None:
            removed = await self._cursors.remove_cursor(
                agent_id=cursor.agent_id,
                schedule_id=cursor.schedule_id,
                expected_definition_hash=cursor.definition_hash,
                expected_revision=cursor.revision,
            )
            return self._outcome(
                cursor,
                scheduled_for,
                (
                    ScheduleTriggerDisposition.REMOVED
                    if removed
                    else ScheduleTriggerDisposition.RACE_LOST
                ),
                next_fire_at=None,
            )

        definition_hash = schedule_definition_hash(definition)
        if cursor.definition_hash != definition_hash or not definition.enabled:
            reconciled = await self._cursors.reconcile_cursor(
                ScheduleTriggerCursor(
                    agent_id=definition.agent_id,
                    schedule_id=definition.schedule_id,
                    definition_hash=definition_hash,
                    next_fire_at=first_schedule_fire_at(
                        definition,
                        now=now,
                    ),
                ),
            )
            return self._outcome(
                cursor,
                scheduled_for,
                ScheduleTriggerDisposition.RECONCILED,
                next_fire_at=reconciled.next_fire_at,
            )

        lateness = (now - scheduled_for).total_seconds()
        if lateness > definition.misfire_grace_seconds:
            next_fire_at = next_schedule_fire_after(
                definition.trigger,
                previous=scheduled_for,
                not_before=now,
            )
            return await self._advance(
                cursor=cursor,
                scheduled_for=scheduled_for,
                next_fire_at=next_fire_at,
                disposition=ScheduleTriggerDisposition.MISFIRED,
            )

        try:
            handling = await handle(definition, scheduled_for)
        except Exception:  # noqa: BLE001 - retry is the durability boundary
            return await self._defer_retry(
                cursor=cursor,
                definition=definition,
                scheduled_for=scheduled_for,
                now=now,
            )
        if handling is ScheduleOccurrenceHandling.RETRY:
            return await self._defer_retry(
                cursor=cursor,
                definition=definition,
                scheduled_for=scheduled_for,
                now=now,
            )

        next_fire_at = next_schedule_fire_at(
            definition.trigger,
            previous=scheduled_for,
        )
        if next_fire_at is not None and next_fire_at <= now:
            next_fire_at = next_schedule_fire_after(
                definition.trigger,
                previous=scheduled_for,
                not_before=now,
            )
        return await self._advance(
            cursor=cursor,
            scheduled_for=scheduled_for,
            next_fire_at=next_fire_at,
            disposition=ScheduleTriggerDisposition.DISPATCHED,
        )

    async def _advance(
        self,
        *,
        cursor: ScheduleTriggerCursor,
        scheduled_for: datetime,
        next_fire_at: datetime | None,
        disposition: ScheduleTriggerDisposition,
    ) -> ScheduleTriggerOutcome:
        try:
            advanced = await self._cursors.advance_cursor(
                agent_id=cursor.agent_id,
                schedule_id=cursor.schedule_id,
                definition_hash=cursor.definition_hash,
                expected_revision=cursor.revision,
                scheduled_for=scheduled_for,
                next_fire_at=next_fire_at,
            )
        except ScheduleCursorConflictError:
            return self._outcome(
                cursor,
                scheduled_for,
                ScheduleTriggerDisposition.RACE_LOST,
            )
        return self._outcome(
            cursor,
            scheduled_for,
            disposition,
            next_fire_at=advanced.next_fire_at,
        )

    async def _defer_retry(
        self,
        *,
        cursor: ScheduleTriggerCursor,
        definition: ScheduleDefinition,
        scheduled_for: datetime,
        now: datetime,
    ) -> ScheduleTriggerOutcome:
        delay = max(
            definition.retry_policy.backoff_seconds,
            self._minimum_retry_delay_seconds,
        )
        retry_not_before = now + timedelta(seconds=delay)
        try:
            deferred = await self._cursors.defer_cursor(
                agent_id=cursor.agent_id,
                schedule_id=cursor.schedule_id,
                definition_hash=cursor.definition_hash,
                expected_revision=cursor.revision,
                scheduled_for=scheduled_for,
                retry_not_before=retry_not_before,
            )
        except ScheduleCursorConflictError:
            return self._outcome(
                cursor,
                scheduled_for,
                ScheduleTriggerDisposition.RACE_LOST,
            )
        return self._outcome(
            cursor,
            scheduled_for,
            ScheduleTriggerDisposition.RETRY_PENDING,
            next_fire_at=scheduled_for,
            retry_not_before=deferred.retry_not_before,
        )

    @staticmethod
    def _outcome(
        cursor: ScheduleTriggerCursor,
        scheduled_for: datetime,
        disposition: ScheduleTriggerDisposition,
        *,
        next_fire_at: datetime | None = None,
        retry_not_before: datetime | None = None,
    ) -> ScheduleTriggerOutcome:
        return ScheduleTriggerOutcome(
            schedule_id=cursor.schedule_id,
            scheduled_for=scheduled_for,
            disposition=disposition,
            next_fire_at=next_fire_at,
            retry_not_before=retry_not_before,
        )

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("schedule trigger time must be timezone-aware")
        return value.astimezone(timezone.utc)


__all__ = [
    "DurableScheduleTriggerWorker",
    "ScheduleOccurrenceHandler",
    "ScheduleOccurrenceHandling",
    "ScheduleTriggerDisposition",
    "ScheduleTriggerOutcome",
    "ScheduleTriggerTickReport",
]
