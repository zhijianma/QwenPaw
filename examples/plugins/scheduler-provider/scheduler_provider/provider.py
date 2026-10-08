# -*- coding: utf-8 -*-
"""Create a Host-backed Scheduler through the stable plugin SDK."""

from __future__ import annotations

from qwenpaw.plugins.sdk import SchedulerHost, SchedulerPort

PROVIDER_ID = "scheduler-provider.local-durable"


class LocalSchedulerProvider:
    """Bind the plugin identity to Host-owned durable Scheduler state."""

    provider_id = PROVIDER_ID

    async def health_check(self) -> bool:
        """Report that the stateless Provider can be published."""
        return True

    async def open(self, host: SchedulerHost) -> SchedulerPort:
        """Return only the Store admitted by the Host."""
        return host.scheduler_store()


def create_scheduler() -> LocalSchedulerProvider:
    """Create the stateless Scheduler Provider."""
    return LocalSchedulerProvider()
