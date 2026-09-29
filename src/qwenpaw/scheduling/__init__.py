# -*- coding: utf-8 -*-
"""Scheduler adapters for QwenPaw editions."""

from .sqlite import SQLiteSchedulerStore
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
    "ScheduleDispatchAccountingError",
    "ScheduleDispatchDisposition",
    "ScheduleDispatchResult",
    "ScheduledTaskDispatcher",
    "SchedulerCapabilityUnavailableError",
]
