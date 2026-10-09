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
    next_schedule_fire_at,
    schedule_definition_hash,
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
    "first_schedule_fire_at",
    "next_schedule_fire_at",
    "schedule_definition_hash",
]
