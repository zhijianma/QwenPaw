# -*- coding: utf-8 -*-
"""Tests for the transport-neutral Sensor application boundary."""

import pytest

from qwenpaw.kernel.models import Task, TaskSource
from qwenpaw.tasks.sensor_application import TaskSensorApplicationService


class _RecordingPoller:
    """Small Sensor host port used by application tests."""

    def __init__(self) -> None:
        self.call: tuple[str, str] | None = None

    async def poll(
        self,
        capability_id: str,
        *,
        agent_id: str,
        trigger_payload=None,
    ) -> tuple[Task, ...]:
        del trigger_payload
        self.call = (capability_id, agent_id)
        return (
            Task(
                objective="Review a task",
                source=TaskSource.SENSOR,
                agent_id=agent_id,
            ),
        )


@pytest.mark.asyncio
async def test_sensor_application_binds_agent_identity() -> None:
    poller = _RecordingPoller()
    application = TaskSensorApplicationService(
        poller,
        agent_id="agent.local",
    )

    tasks = await application.poll("sensor.review")

    assert poller.call == ("sensor.review", "agent.local")
    assert tasks[0].agent_id == "agent.local"
