# -*- coding: utf-8 -*-
"""Tests for approval-gated contributed sensor dispatch."""

from pathlib import Path

import pytest

from qwenpaw.kernel.models import Proposal, RiskLevel, TaskStatus
from qwenpaw.plugins.architecture import PluginManifest
from qwenpaw.plugins.generations import ActivationError, GenerationRegistry
from qwenpaw.tasks.ledger import SQLiteExecutionLedger
from qwenpaw.tasks.sensors import (
    SensorCapabilityUnavailableError,
    SensorContributionHost,
    SensorTriggerPayloadLimitError,
)
from qwenpaw.tasks.service import TaskService


class ReviewSensor:
    """Small conforming sensor used by the runtime host tests."""

    def __init__(self, source: str | None = None) -> None:
        self._proposal = Proposal(
            source=source or self.sensor_id,
            objective="Review the latest task outcome",
            rationale_summary="A review may be useful",
            risk=RiskLevel.LOW,
        )

    @property
    def sensor_id(self) -> str:
        return "task-insights.review-sensor"

    async def propose(self) -> tuple[Proposal, ...]:
        return (self._proposal,)


def _manifest(slot: str = "sensor") -> PluginManifest:
    return PluginManifest.from_dict(
        {
            "schema_version": "qwenpaw.plugin.v2",
            "id": "task-insights",
            "version": "1.0.0",
            "contributions": [
                {
                    "id": "review-sensor",
                    "slot": slot,
                    "entrypoint": "task_insights.sensor:create_sensor",
                },
            ],
        },
    )


@pytest.mark.asyncio
async def test_sensor_proposals_enter_shared_approval_gate(
    tmp_path: Path,
) -> None:
    registry = GenerationRegistry()
    await registry.activate(_manifest(), lambda _: ReviewSensor())
    service = TaskService(
        store=SQLiteExecutionLedger(tmp_path / "tasks.db"),
        registry_generation=registry.generation,
    )
    host = SensorContributionHost(service, registry)

    first = await host.poll(
        "task-insights.review-sensor",
        agent_id="default",
    )
    retry = await host.poll(
        "task-insights.review-sensor",
        agent_id="default",
    )

    assert first[0].task_id == retry[0].task_id
    assert retry[0].status is TaskStatus.WAITING_APPROVAL
    assert retry[0].metadata["sensor_id"] == ("task-insights.review-sensor")
    events = await service.list_events(first[0].task_id)
    assert [event.event_type for event in events] == [
        "task.created",
        "task.planned",
        "run.started",
        "approval.requested",
    ]


@pytest.mark.asyncio
async def test_sensor_slot_mismatch_fails_before_task_creation(
    tmp_path: Path,
) -> None:
    registry = GenerationRegistry()
    with pytest.raises(
        ActivationError,
        match="does not implement the 'runner' contract",
    ):
        await registry.activate(
            _manifest("runner"),
            lambda _: ReviewSensor(),
        )

    assert registry.generation == 1
    service = TaskService(
        store=SQLiteExecutionLedger(tmp_path / "tasks.db"),
        registry_generation=registry.generation,
    )

    with pytest.raises(
        SensorCapabilityUnavailableError,
        match="is unavailable",
    ):
        await SensorContributionHost(service, registry).poll(
            "task-insights.review-sensor",
            agent_id="default",
        )

    assert not await service.list_tasks(cursor=None, limit=10)


@pytest.mark.asyncio
async def test_sensor_trigger_size_fails_before_plugin_execution(
    tmp_path: Path,
) -> None:
    sensor = ReviewSensor()
    registry = GenerationRegistry()
    await registry.activate(_manifest(), lambda _: sensor)
    service = TaskService(
        store=SQLiteExecutionLedger(tmp_path / "tasks.db"),
        registry_generation=registry.generation,
    )

    with pytest.raises(
        SensorTriggerPayloadLimitError,
        match="exceeds 32 KiB",
    ):
        await SensorContributionHost(service, registry).poll(
            sensor.sensor_id,
            agent_id="default",
            trigger_payload={"content": "x" * (32 * 1024)},
        )

    assert not await service.list_tasks(cursor=None, limit=10)


@pytest.mark.asyncio
async def test_sensor_rejects_spoofed_proposal_source(
    tmp_path: Path,
) -> None:
    sensor = ReviewSensor(source="other.sensor")
    registry = GenerationRegistry()
    await registry.activate(_manifest(), lambda _: sensor)
    service = TaskService(
        store=SQLiteExecutionLedger(tmp_path / "tasks.db"),
        registry_generation=registry.generation,
    )

    with pytest.raises(
        SensorCapabilityUnavailableError,
        match="source does not match",
    ):
        await SensorContributionHost(service, registry).poll(
            sensor.sensor_id,
            agent_id="default",
        )

    assert not await service.list_tasks(cursor=None, limit=10)
