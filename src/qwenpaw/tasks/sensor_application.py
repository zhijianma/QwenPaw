# -*- coding: utf-8 -*-
"""Transport-neutral application boundary for Sensor polling."""

from __future__ import annotations

from typing import Protocol

from ..kernel.models import JsonObject, Task


class SensorPoller(Protocol):
    """Minimum Sensor host contract required by an application entry."""

    async def poll(
        self,
        capability_id: str,
        *,
        agent_id: str,
        trigger_payload: JsonObject | None = None,
    ) -> tuple[Task, ...]:
        """Poll one pinned capability and persist its proposals."""


class TaskSensorApplicationService:
    """Bind an authenticated Agent identity to Sensor polling."""

    def __init__(self, poller: SensorPoller, *, agent_id: str) -> None:
        if not agent_id.strip():
            raise ValueError("agent_id cannot be empty")
        self._poller = poller
        self._agent_id = agent_id

    async def poll(
        self,
        capability_id: str,
        *,
        trigger_payload: JsonObject | None = None,
    ) -> tuple[Task, ...]:
        """Return Tasks created through the shared proposal gate."""
        return await self._poller.poll(
            capability_id,
            agent_id=self._agent_id,
            trigger_payload=trigger_payload,
        )


__all__ = ["SensorPoller", "TaskSensorApplicationService"]
