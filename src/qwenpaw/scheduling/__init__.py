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
]
