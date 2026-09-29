# -*- coding: utf-8 -*-
"""Shared execution contract for system and plugin Harness Runners."""

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from qwenpaw.harnesses.events import HarnessEvent, HarnessEventKind
from qwenpaw.kernel.models import (
    CapabilityBundle,
    PlanStep,
    RunStatus,
    TaskOrder,
    TaskStatus,
)
from qwenpaw.kernel.ports import ContextualTaskRunner, TaskRunner
from qwenpaw.plugins.architecture import PluginManifest
from qwenpaw.plugins.generations import GenerationRegistry
from qwenpaw.tasks.ledger import SQLiteExecutionLedger
from qwenpaw.tasks.runner import TaskExecutionCoordinator
from qwenpaw.tasks.service import TaskService
from qwenpaw.tasks.system_contributions import (
    SYSTEM_CAPABILITY_BUNDLE,
    SYSTEM_CODEX_RUNNER_ID,
    system_contribution_factory,
)


class _SystemHarnessRuntime:
    async def task_events(self, **kwargs: Any):
        del kwargs
        yield HarnessEvent(
            kind=HarnessEventKind.REASONING_DELTA,
            text="Inspecting",
            item_id="reason-contract",
        )
        yield HarnessEvent(
            kind=HarnessEventKind.TEXT_DELTA,
            text="System Harness result",
        )
        yield HarnessEvent(kind=HarnessEventKind.COMPLETED)


def _system_harness_bundle() -> CapabilityBundle:
    return SYSTEM_CAPABILITY_BUNDLE.model_copy(
        update={
            "contributions": tuple(
                item
                for item in SYSTEM_CAPABILITY_BUNDLE.contributions
                if item.contribution_id == "codex-harness"
            ),
        },
    )


def _plugin_root() -> Path:
    return (
        Path(__file__).parents[3]
        / "examples"
        / "plugins"
        / "runtime-provider-kit"
    )


async def _execute_selected(
    tmp_path: Path,
    registry: GenerationRegistry,
    runner_id: str,
    label: str,
) -> tuple[Any, Any, list[Any]]:
    service = TaskService(
        store=SQLiteExecutionLedger(tmp_path / f"{label}.sqlite3"),
        registry_generation=registry.generation,
    )
    task = await service.create_task(
        objective=f"Run {label} Harness",
        agent_id="agent-contract",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Run", objective=task.objective),),
    )
    order = TaskOrder(
        task_id=task.task_id,
        objective=task.objective,
        metadata={
            "agent_id": task.agent_id,
            "project_dir": str(tmp_path / label),
            "task_ledger_workspace_dir": str(tmp_path / label),
        },
    )
    completed = await TaskExecutionCoordinator(
        service,
        registry,
    ).execute_selected(order, runner_id)
    stored = await service.get_task(task.task_id)
    events = await service.list_events(task.task_id)
    return completed, stored, events


@pytest.mark.asyncio
async def test_system_and_plugin_harnesses_share_execution_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = SimpleNamespace(
        harness_runtime=_SystemHarnessRuntime(),
        config=SimpleNamespace(backend="qwenpaw", backend_settings={}),
    )

    async def resolve_workspace(_agent_id: str) -> Any:
        return workspace

    system_registry = GenerationRegistry()
    await system_registry.activate_bundle(
        _system_harness_bundle(),
        system_contribution_factory(resolve_workspace),
    )

    plugin_root = _plugin_root()
    monkeypatch.syspath_prepend(str(plugin_root))
    harness_module = importlib.import_module("runtime_provider_kit.harness")
    manifest_data = json.loads(
        (plugin_root / "plugin.json").read_text(encoding="utf-8"),
    )
    manifest_data["contributions"] = [
        item
        for item in manifest_data["contributions"]
        if item["slot"] == "harness.runner"
    ]
    plugin_registry = GenerationRegistry()
    await plugin_registry.activate(
        PluginManifest.from_dict(manifest_data),
        lambda _declaration: harness_module.create_runner(),
    )

    cases = (
        (system_registry, SYSTEM_CODEX_RUNNER_ID, "system"),
        (
            plugin_registry,
            "runtime-provider-kit.echo-harness",
            "plugin",
        ),
    )
    for registry, runner_id, label in cases:
        lease = await registry.pin()
        runner = lease.implementation(runner_id)
        descriptor = lease.resolve(runner_id)
        assert descriptor is not None
        assert descriptor.slot == "harness.runner"
        assert isinstance(runner, TaskRunner)
        assert isinstance(runner, ContextualTaskRunner)
        await lease.close()

        completed, stored, events = await _execute_selected(
            tmp_path,
            registry,
            runner_id,
            label,
        )
        event_types = [event.event_type for event in events]
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
        assert event_types[0] == "task.created"
        assert event_types[-1] == "run.completed"
        assert artifact.artifact_refs
        assert artifact.evidence_refs[0].producer == runner_id
