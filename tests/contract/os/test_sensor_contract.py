# -*- coding: utf-8 -*-
"""Shared approval contract for system and plugin Proposal Sensors."""

import importlib
import json
from pathlib import Path

import pytest

from qwenpaw.kernel import ApprovalStatus, Proposal, RiskLevel, TaskStatus
from qwenpaw.plugins.architecture import PluginManifest
from qwenpaw.plugins.generations import GenerationRegistry
from qwenpaw.tasks.ledger import SQLiteExecutionLedger
from qwenpaw.tasks.sensors import SensorContributionHost
from qwenpaw.tasks.service import TaskService
from qwenpaw.tasks.system_contributions import (
    SYSTEM_CAPABILITY_BUNDLE,
    SYSTEM_PROACTIVE_MEMORY_SENSOR_ID,
    system_contribution_factory,
)


def _plugin_root() -> Path:
    return Path(__file__).parents[3] / "examples" / "plugins" / "task-insights"


@pytest.mark.asyncio
async def test_system_and_plugin_sensors_share_approval_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def resolve_workspace(_agent_id: str):
        raise AssertionError("Sensor adapter must not resolve a Workspace")

    system_bundle = SYSTEM_CAPABILITY_BUNDLE.model_copy(
        update={
            "contributions": tuple(
                contribution
                for contribution in SYSTEM_CAPABILITY_BUNDLE.contributions
                if contribution.contribution_id == "proactive-memory-sensor"
            ),
        },
    )
    system_registry = GenerationRegistry()
    await system_registry.activate_bundle(
        system_bundle,
        system_contribution_factory(resolve_workspace),
    )

    plugin_root = _plugin_root()
    monkeypatch.syspath_prepend(str(plugin_root))
    sensor_module = importlib.import_module("task_insights.sensor")
    manifest_data = json.loads(
        (plugin_root / "plugin.json").read_text(encoding="utf-8"),
    )
    manifest_data["contributions"] = [
        contribution
        for contribution in manifest_data["contributions"]
        if contribution["slot"] == "sensor"
    ]
    plugin_registry = GenerationRegistry()
    await plugin_registry.activate(
        PluginManifest.from_dict(manifest_data),
        lambda _declaration: sensor_module.create_sensor(),
    )

    cases = (
        (
            system_registry,
            SYSTEM_PROACTIVE_MEMORY_SENSOR_ID,
            {
                "proposals": [
                    Proposal(
                        source=SYSTEM_PROACTIVE_MEMORY_SENSOR_ID,
                        objective="Review proactive memory",
                        rationale_summary="New durable memory is available",
                        risk=RiskLevel.MEDIUM,
                    ).model_dump(mode="json"),
                ],
            },
        ),
        (
            plugin_registry,
            "task-insights.review-sensor",
            None,
        ),
    )
    for index, (registry, sensor_id, trigger_payload) in enumerate(cases):
        service = TaskService(
            store=SQLiteExecutionLedger(
                tmp_path / f"sensor-{index}.sqlite3",
            ),
            registry_generation=1,
        )
        tasks = await SensorContributionHost(service, registry).poll(
            sensor_id,
            agent_id="agent-contract",
            trigger_payload=trigger_payload,
        )

        assert len(tasks) == 1
        task = tasks[0]
        assert task.status is TaskStatus.WAITING_APPROVAL
        assert task.metadata["sensor_id"] == sensor_id
        assert task.metadata["sensor_registry_generation"] == (
            registry.generation
        )
        assert task.metadata["proposal"]["source"] == sensor_id
        if index == 1:
            assert task.metadata["proposal"]["metadata"] == {
                "agent_id": "agent-contract",
                "registry_generation": registry.generation,
            }
        runs = await service.list_runs(task.task_id)
        assert runs[0].registry_generation == registry.generation
        approvals = await service.list_approvals(
            task.task_id,
            status=ApprovalStatus.PENDING,
        )
        assert len(approvals) == 1
        assert approvals[0][0].requester.id == sensor_id
        assert approvals[0][0].action == "proposal.execute"
        events = await service.list_events(task.task_id)
        assert [event.event_type for event in events] == [
            "task.created",
            "task.planned",
            "run.started",
            "approval.requested",
        ]
