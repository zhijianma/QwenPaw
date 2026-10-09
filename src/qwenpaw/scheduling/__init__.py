# -*- coding: utf-8 -*-
"""Scheduler adapters for QwenPaw editions."""

from .sqlite import SQLiteSchedulerStore
from .providers import HostSchedulerProvider, SchedulerStoreHost
from .dispatch import (
    DEFAULT_SCHEDULER_CAPABILITY_ID,
    ScheduleDispatchAccountingError,
    ScheduleDispatchDisposition,
    ScheduleDispatchResult,
    ScheduledTaskDispatcher,
    SchedulerCapabilityUnavailableError,
)
from .triggering import (
    first_schedule_fire_at,
    next_schedule_fire_after,
    next_schedule_fire_at,
    schedule_definition_hash,
)
from .trigger_worker import (
    DurableScheduleTriggerWorker,
    ScheduleOccurrenceHandler,
    ScheduleOccurrenceHandling,
    ScheduleTriggerDisposition,
    ScheduleTriggerOutcome,
    ScheduleTriggerTickReport,
)

__all__ = [
    "DEFAULT_SCHEDULER_CAPABILITY_ID",
    "SQLiteSchedulerStore",
    "HostSchedulerProvider",
    "SchedulerStoreHost",
    "ScheduleDispatchAccountingError",
    "ScheduleDispatchDisposition",
    "ScheduleDispatchResult",
    "ScheduledTaskDispatcher",
    "SchedulerCapabilityUnavailableError",
    "DurableScheduleTriggerWorker",
    "ScheduleOccurrenceHandler",
    "ScheduleOccurrenceHandling",
    "ScheduleTriggerDisposition",
    "ScheduleTriggerOutcome",
    "ScheduleTriggerTickReport",
    "first_schedule_fire_at",
    "next_schedule_fire_after",
    "next_schedule_fire_at",
    "schedule_definition_hash",
]
