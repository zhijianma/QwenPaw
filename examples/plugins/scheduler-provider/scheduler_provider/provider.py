"""Create a durable Scheduler through the stable plugin SDK."""

from __future__ import annotations

import os
from pathlib import Path

from qwenpaw.plugins.sdk import SQLiteSchedulerStore


def create_scheduler() -> SQLiteSchedulerStore:
    """Create a local Scheduler using a configurable database path."""
    configured = os.environ.get("QWENPAW_EXAMPLE_SCHEDULER_DB", "").strip()
    database_path = (
        Path(configured).expanduser()
        if configured
        else Path.cwd() / ".qwenpaw" / "example-scheduler.db"
    )
    return SQLiteSchedulerStore(database_path)
