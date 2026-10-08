# -*- coding: utf-8 -*-
"""Host-owned Scheduler Provider adapters."""

from __future__ import annotations

from dataclasses import dataclass

from ..kernel import SchedulerHost, SchedulerPort


@dataclass(frozen=True, slots=True)
class HostSchedulerProvider:
    """Return only the durable Scheduler Store admitted by the Host."""

    provider_id: str

    async def open(self, host: SchedulerHost) -> SchedulerPort:
        """Bind this Provider to Host-owned persistence."""
        store = host.scheduler_store()
        if not isinstance(store, SchedulerPort):
            raise TypeError("scheduler host returned an invalid store")
        return store


@dataclass(frozen=True, slots=True)
class SchedulerStoreHost:
    """Expose one pre-authorized Store without leaking its filesystem path."""

    _store: SchedulerPort

    def scheduler_store(self) -> SchedulerPort:
        """Return the pre-authorized durable Store."""
        return self._store


__all__ = ["HostSchedulerProvider", "SchedulerStoreHost"]
