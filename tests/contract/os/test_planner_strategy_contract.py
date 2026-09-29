# -*- coding: utf-8 -*-
"""Shared orchestration contract for system and plugin task capabilities."""

import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from qwenpaw.harnesses.events import HarnessEvent, HarnessEventKind
from qwenpaw.kernel.models import (
    CapabilityBundle,
    RunStatus,
    RuntimeLaunchConfig,
    TaskStatus,
)
from qwenpaw.plugins.architecture import PluginManifest
from qwenpaw.plugins.generations import GenerationRegistry
from qwenpaw.tasks.ledger import SQLiteExecutionLedger
from qwenpaw.tasks.runtime import (
    TaskRuntimeOrchestrator,
    TaskRuntimeSupervisor,
)
from qwenpaw.tasks.service import TaskService
from qwenpaw.tasks.system_contributions import (
    SYSTEM_BASIC_PLANNER_ID,
    SYSTEM_CAPABILITY_BUNDLE,
    SYSTEM_CODEX_RUNNER_ID,
    SYSTEM_DEFAULT_STRATEGY_ID,
    system_contribution_factory,
)


class _SystemHarnessRuntime:
    async def task_events(self, **kwargs: Any):
        del kwargs
        yield HarnessEvent(
            kind=HarnessEventKind.TEXT_DELTA,
            text="System task result",
        )
        yield HarnessEvent(kind=HarnessEventKind.COMPLETED)


def _system_bundle() -> CapabilityBundle:
    selected = {
        "basic-planner",
        "codex-harness",
        "default-strategy",
    }
    return SYSTEM_CAPABILITY_BUNDLE.model_copy(
        update={
            "contributions": tuple(
                item
                for item in SYSTEM_CAPABILITY_BUNDLE.contributions
                if item.contribution_id in selected
            ),
        },
    )


def _plugin_root() -> Path:
    return Path(__file__).parents[3] / "examples" / "plugins" / "task-insights"


async def _wait_for_terminal(
    service: TaskService,
    task_id: Any,
) -> Any:
    for _ in range(100):
        task = await service.get_task(task_id)
        if task is not None and task.status in {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
        }:
            return task
        await asyncio.sleep(0.01)
    raise AssertionError("task did not reach a terminal state")


async def _run_case(
    tmp_path: Path,
    registry: GenerationRegistry,
    *,
    label: str,
    planner_id: str,
    runner_id: str,
    strategy_id: str,
) -> tuple[Any, Any, Any, list[Any]]:
    project_dir = tmp_path / label / "project"
    project_dir.mkdir(parents=True)
    service = TaskService(
        store=SQLiteExecutionLedger(tmp_path / f"{label}.sqlite3"),
        registry_generation=registry.generation,
    )
    task = await service.create_task(
        objective=f"Inspect {label} runtime",
        agent_id="agent-contract",
    )
    run = await TaskRuntimeOrchestrator(
        service,
        registry,
        TaskRuntimeSupervisor(),
    ).start(
        task,
        planner_id=planner_id,
        runner_id=runner_id,
        strategy_id=strategy_id,
        runtime_config=RuntimeLaunchConfig(
            agent_id=task.agent_id,
            conversation_id=f"chat-{label}",
            project_dir=str(project_dir),
            ledger_workspace_dir=str(tmp_path / label),
            strategy_id=strategy_id,
        ),
    )
    stored = await _wait_for_terminal(service, task.task_id)
    plan = await service.latest_plan(task.task_id)
    events = list(await service.list_events(task.task_id))
    return run, stored, plan, events


@pytest.mark.asyncio
async def test_system_and_plugin_planning_share_orchestration_contract(
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
        _system_bundle(),
        system_contribution_factory(resolve_workspace),
    )

    plugin_root = _plugin_root()
    monkeypatch.syspath_prepend(str(plugin_root))
    modules = {
        "insight-planner": importlib.import_module(
            "task_insights.planner",
        ).create_planner,
        "insight-strategy": importlib.import_module(
            "task_insights.strategy",
        ).create_strategy,
        "summary-runner": importlib.import_module(
            "task_insights.runner",
        ).create_runner,
    }
    manifest_data = json.loads(
        (plugin_root / "plugin.json").read_text(encoding="utf-8"),
    )
    manifest_data["contributions"] = [
        item
        for item in manifest_data["contributions"]
        if item["id"] in modules
    ]
    plugin_registry = GenerationRegistry()
    await plugin_registry.activate(
        PluginManifest.from_dict(manifest_data),
        lambda declaration: modules[declaration.contribution_id](),
    )

    cases = (
        (
            system_registry,
            "system",
            SYSTEM_BASIC_PLANNER_ID,
            SYSTEM_CODEX_RUNNER_ID,
            SYSTEM_DEFAULT_STRATEGY_ID,
        ),
        (
            plugin_registry,
            "plugin",
            "task-insights.insight-planner",
            "task-insights.summary-runner",
            "task-insights.insight-strategy",
        ),
    )
    results = []
    for registry, label, planner_id, runner_id, strategy_id in cases:
        results.append(
            await _run_case(
                tmp_path,
                registry,
                label=label,
                planner_id=planner_id,
                runner_id=runner_id,
                strategy_id=strategy_id,
            ),
        )

    for case, result in zip(cases, results, strict=True):
        registry, _, _, runner_id, strategy_id = case
        run, stored, plan, events = result
        assert run.status is RunStatus.RUNNING
        assert run.runner_id == runner_id
        assert run.strategy_id == strategy_id
        assert run.registry_generation == registry.generation
        assert stored.status is TaskStatus.COMPLETED
        assert plan is not None
        assert plan.steps
        assert events[-1].event_type == "run.completed"
        artifact = next(
            event
            for event in events
            if event.event_type == "artifact.produced"
        )
        assert artifact.evidence_refs[0].producer == runner_id

    plugin_events = results[1][3]
    summary = next(
        event
        for event in plugin_events
        if event.event_type == "plugin.task-insights.summary"
    )
    assert summary.source == "task-insights.summary-runner"
    assert summary.payload["strategy_id"] == ("task-insights.insight-strategy")
    assert summary.payload["strategy_parameters"] == {
        "analysis_depth": "concise",
        "objective_length": len("Inspect plugin runtime"),
        "registry_generation": plugin_registry.generation,
    }
    plugin_plan = results[1][2]
    assert [step.title for step in plugin_plan.steps] == [
        "Inspect task objective",
        "Publish task insight",
    ]
    assert plugin_plan.steps[1].depends_on == (plugin_plan.steps[0].step_id,)
