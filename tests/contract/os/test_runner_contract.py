# -*- coding: utf-8 -*-
"""Shared execution contract for system and plugin Task Runners."""

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from qwenpaw.kernel.models import (
    CapabilityBundle,
    PlanStep,
    RunnerPreflightRequest,
    RunStatus,
    TaskOrder,
    TaskStatus,
)
from qwenpaw.kernel.ports import (
    ContextualTaskRunner,
    PreflightTaskRunner,
    TaskRunner,
)
from qwenpaw.plugins.architecture import PluginManifest
from qwenpaw.plugins.generations import GenerationRegistry
from qwenpaw.tasks.ledger import SQLiteExecutionLedger
from qwenpaw.tasks.runner import TaskExecutionCoordinator
from qwenpaw.tasks.service import TaskService
from qwenpaw.tasks.system_contributions import (
    SYSTEM_CAPABILITY_BUNDLE,
    SYSTEM_CONSOLE_RUNNER_ID,
    system_contribution_factory,
)


class _ConsoleChannel:
    async def stream_one(self, _payload: dict[str, Any]):
        yield (
            'data: {"object":"response","status":"completed",'
            '"output":[{"role":"assistant","content":['
            '{"type":"text","text":"System runner result"}]}]}\n\n'
        )


class _ChannelManager:
    async def get_channel(self, channel_id: str) -> _ConsoleChannel | None:
        if channel_id == "console":
            return _ConsoleChannel()
        return None


def _system_bundle() -> CapabilityBundle:
    return SYSTEM_CAPABILITY_BUNDLE.model_copy(
        update={
            "contributions": tuple(
                contribution
                for contribution in SYSTEM_CAPABILITY_BUNDLE.contributions
                if contribution.contribution_id == "console-agent"
            ),
        },
    )


async def _system_registry(tmp_path: Path) -> GenerationRegistry:
    workspace = SimpleNamespace(
        agent_id="agent-contract",
        workspace_dir=tmp_path,
        channel_manager=_ChannelManager(),
    )

    async def resolve_workspace(_agent_id: str) -> Any:
        return workspace

    registry = GenerationRegistry()
    await registry.activate_bundle(
        _system_bundle(),
        system_contribution_factory(resolve_workspace),
    )
    return registry


async def _plugin_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> GenerationRegistry:
    plugin_root = (
        Path(__file__).parents[3] / "examples" / "plugins" / "task-insights"
    )
    monkeypatch.syspath_prepend(str(plugin_root))
    module = importlib.import_module("task_insights.runner")
    manifest_data = json.loads(
        (plugin_root / "plugin.json").read_text(encoding="utf-8"),
    )
    manifest_data["contributions"] = [
        item
        for item in manifest_data["contributions"]
        if item["slot"] == "runner"
    ]
    registry = GenerationRegistry()
    await registry.activate(
        PluginManifest.from_dict(manifest_data),
        lambda _declaration: module.create_runner(),
    )
    return registry


async def _execute(
    tmp_path: Path,
    registry: GenerationRegistry,
    runner_id: str,
    label: str,
) -> tuple[Any, Any, list[Any]]:
    project_dir = tmp_path / label / "project"
    project_dir.mkdir(parents=True)
    service = TaskService(
        store=SQLiteExecutionLedger(tmp_path / f"{label}.sqlite3"),
        registry_generation=registry.generation,
    )
    task = await service.create_task(
        objective=f"Execute through the {label} runner",
        agent_id="agent-contract",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Execute", objective=task.objective),),
    )
    completed = await TaskExecutionCoordinator(
        service,
        registry,
    ).execute_selected(
        TaskOrder(
            task_id=task.task_id,
            objective=task.objective,
            metadata={
                "agent_id": task.agent_id,
                "project_dir": str(project_dir),
                "task_ledger_workspace_dir": str(tmp_path / label),
            },
        ),
        runner_id,
    )
    stored = await service.get_task(task.task_id)
    events = list(await service.list_events(task.task_id))
    return completed, stored, events


@pytest.mark.asyncio
async def test_system_and_plugin_runners_share_execution_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system_registry = await _system_registry(tmp_path / "system-workspace")
    plugin_registry = await _plugin_registry(monkeypatch)
    cases = (
        (system_registry, SYSTEM_CONSOLE_RUNNER_ID, "system"),
        (
            plugin_registry,
            "task-insights.summary-runner",
            "plugin",
        ),
    )

    for registry, runner_id, label in cases:
        lease = await registry.pin()
        try:
            descriptor = lease.resolve(runner_id)
            runner = lease.implementation(runner_id)
            assert descriptor is not None
            assert descriptor.slot == "runner"
            assert isinstance(runner, TaskRunner)
            assert isinstance(runner, ContextualTaskRunner)
            assert isinstance(runner, PreflightTaskRunner)
            preflight = await runner.preflight(
                RunnerPreflightRequest(
                    runner_id=runner_id,
                    slot="runner",
                    registry_generation=registry.generation,
                ),
            )
            assert preflight.runner_id == runner_id
            assert preflight.slot == "runner"
            assert preflight.registry_generation == registry.generation
            assert preflight.contextual is True
        finally:
            await lease.close()

        completed, stored, events = await _execute(
            tmp_path,
            registry,
            runner_id,
            label,
        )
        artifact = next(
            event
            for event in events
            if event.event_type == "artifact.produced"
        )

        assert completed.status is RunStatus.SUCCEEDED
        assert completed.runner_id == runner_id
        assert completed.registry_generation == registry.generation
        assert stored is not None
        assert stored.status is TaskStatus.COMPLETED
        assert events[-1].event_type == "run.completed"
        assert artifact.artifact_refs
        assert artifact.evidence_refs[0].producer == runner_id
