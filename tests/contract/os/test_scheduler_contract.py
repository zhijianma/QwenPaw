# -*- coding: utf-8 -*-
"""Shared dispatch contract for system and plugin Scheduler providers."""

import asyncio
import importlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from qwenpaw.harnesses.events import HarnessEvent, HarnessEventKind
from qwenpaw.kernel import (
    ScheduleDefinition,
    ScheduleTrigger,
    ScheduleWorkKind,
    TaskStatus,
)
from qwenpaw.kernel.models import CapabilityBundle
from qwenpaw.plugins.architecture import PluginManifest
from qwenpaw.plugins.generations import GenerationRegistry
from qwenpaw.scheduling import (
    ScheduleDispatchDisposition,
    ServiceScheduleDispatchDisposition,
    ScheduledServiceCallbackDispatcher,
    ScheduledTaskDispatcher,
    SQLiteSchedulerStore,
    SchedulerStoreHost,
)
from qwenpaw.tasks.application import TaskApplicationService
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
    SYSTEM_LOCAL_SCHEDULER_ID,
    system_contribution_factory,
)


class _SystemHarnessRuntime:
    async def task_events(self, **kwargs: Any):
        del kwargs
        yield HarnessEvent(
            kind=HarnessEventKind.TEXT_DELTA,
            text="Scheduled task result",
        )
        yield HarnessEvent(kind=HarnessEventKind.COMPLETED)


def _system_bundle(*, include_scheduler: bool) -> CapabilityBundle:
    selected = {
        "basic-planner",
        "codex-harness",
        "default-strategy",
    }
    if include_scheduler:
        selected.add("local-durable-scheduler")
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
    return (
        Path(__file__).parents[3]
        / "examples"
        / "plugins"
        / "scheduler-provider"
    )


async def _wait_for_completed(
    service: TaskService,
    task_id: Any,
) -> Any:
    for _ in range(100):
        task = await service.get_task(task_id)
        if task is not None and task.status is TaskStatus.COMPLETED:
            return task
        await asyncio.sleep(0.01)
    raise AssertionError("scheduled task did not complete")


async def _dispatch_case(
    tmp_path: Path,
    registry: GenerationRegistry,
    *,
    label: str,
    scheduler_id: str,
    scheduler_database: Path,
) -> tuple[Any, list[Any]]:
    project_dir = tmp_path / label
    project_dir.mkdir(parents=True)
    service = TaskService(
        store=SQLiteExecutionLedger(tmp_path / f"{label}-tasks.sqlite3"),
        registry_generation=registry.generation,
    )
    application = TaskApplicationService(
        service,
        agent_id="agent-contract",
        default_project_dir=project_dir,
    )
    dispatcher = ScheduledTaskDispatcher(
        capability_resolver=registry,
        task_application=application,
        task_orchestrator=TaskRuntimeOrchestrator(
            service,
            registry,
            TaskRuntimeSupervisor(),
        ),
        ledger_workspace_dir=tmp_path / label,
        scheduler_capability_id=scheduler_id,
        scheduler_host=SchedulerStoreHost(
            SQLiteSchedulerStore(scheduler_database),
        ),
    )
    definition = ScheduleDefinition(
        agent_id="agent-contract",
        schedule_id=f"{label}.daily",
        conversation_id=f"chat-{label}",
        name=f"{label.title()} daily review",
        objective=f"Prepare the {label} daily review",
        trigger=ScheduleTrigger(kind="cron", cron="0 9 * * *"),
        planner_id=SYSTEM_BASIC_PLANNER_ID,
        runner_id=SYSTEM_CODEX_RUNNER_ID,
        strategy_id=SYSTEM_DEFAULT_STRATEGY_ID,
    )
    scheduled_for = datetime(2026, 9, 29, 9, tzinfo=timezone.utc)
    first = await dispatcher.dispatch(
        definition,
        scheduled_for=scheduled_for,
        idempotency_key=f"{label}:2026-09-29T09:00:00Z",
        owner_id=f"worker.{label}",
    )
    assert first.task is not None
    await _wait_for_completed(service, first.task.task_id)
    replay = await dispatcher.dispatch(
        definition,
        scheduled_for=scheduled_for,
        idempotency_key=f"{label}:2026-09-29T09:00:00Z",
        owner_id=f"worker.{label}.replay",
    )
    durable = SQLiteSchedulerStore(scheduler_database)
    definitions = await durable.list_definitions(agent_id="agent-contract")
    events = list(await service.list_events(first.task.task_id))

    assert first.disposition is ScheduleDispatchDisposition.STARTED
    assert replay.disposition is ScheduleDispatchDisposition.REPLAYED
    assert replay.task is not None
    assert replay.task.task_id == first.task.task_id
    assert first.run is not None
    assert first.run.registry_generation == registry.generation
    assert first.lease.fire.registry_generation == registry.generation
    assert first.lease.task_id == first.task.task_id
    assert definitions == (definition,)

    service_definition = definition.model_copy(
        update={
            "schedule_id": f"{label}.service",
            "name": f"{label.title()} service maintenance",
            "objective": "Execute service maintenance",
            "work_kind": ScheduleWorkKind.SERVICE,
        },
    )
    callback_calls = 0

    async def callback() -> None:
        nonlocal callback_calls
        callback_calls += 1

    service_dispatcher = ScheduledServiceCallbackDispatcher(
        capability_resolver=registry,
        scheduler_capability_id=scheduler_id,
        scheduler_host=SchedulerStoreHost(
            SQLiteSchedulerStore(scheduler_database),
        ),
    )
    service_first = await service_dispatcher.dispatch(
        service_definition,
        scheduled_for=scheduled_for,
        idempotency_key=f"{label}:service:2026-09-29T09:00:00Z",
        owner_id=f"worker.{label}.service",
        callback=callback,
    )
    service_replay = await service_dispatcher.dispatch(
        service_definition,
        scheduled_for=scheduled_for,
        idempotency_key=f"{label}:service:2026-09-29T09:00:00Z",
        owner_id=f"worker.{label}.service.replay",
        callback=callback,
    )

    assert service_first.disposition is (
        ServiceScheduleDispatchDisposition.EXECUTED
    )
    assert service_first.lease.task_id is None
    assert service_first.lease.completion_ref == (
        f"service:{service_definition.schedule_id}"
    )
    assert service_replay.disposition is (
        ServiceScheduleDispatchDisposition.REPLAYED
    )
    assert callback_calls == 1
    return first, events


@pytest.mark.asyncio
async def test_system_and_plugin_schedulers_share_dispatch_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = SimpleNamespace(
        harness_runtime=_SystemHarnessRuntime(),
        config=SimpleNamespace(backend="qwenpaw", backend_settings={}),
    )

    async def resolve_workspace(_agent_id: str) -> Any:
        return workspace

    system_database = tmp_path / "system-scheduler.sqlite3"
    system_registry = GenerationRegistry()
    await system_registry.activate_bundle(
        _system_bundle(include_scheduler=True),
        system_contribution_factory(resolve_workspace),
    )

    plugin_database = tmp_path / "plugin-scheduler.sqlite3"
    plugin_registry = GenerationRegistry()
    await plugin_registry.activate_bundle(
        _system_bundle(include_scheduler=False),
        system_contribution_factory(resolve_workspace),
    )
    plugin_root = _plugin_root()
    monkeypatch.syspath_prepend(str(plugin_root))
    provider_module = importlib.import_module("scheduler_provider.provider")
    manifest = PluginManifest.from_dict(
        json.loads(
            (plugin_root / "plugin.json").read_text(encoding="utf-8"),
        ),
    )
    await plugin_registry.activate(
        manifest,
        lambda _declaration: provider_module.create_scheduler(),
    )

    cases = (
        (
            system_registry,
            "system",
            SYSTEM_LOCAL_SCHEDULER_ID,
            system_database,
        ),
        (
            plugin_registry,
            "plugin",
            "scheduler-provider.local-durable",
            plugin_database,
        ),
    )
    for registry, label, scheduler_id, database in cases:
        lease = await registry.pin()
        descriptor = lease.resolve(scheduler_id)
        assert descriptor is not None
        assert descriptor.slot == "scheduler.provider"
        await lease.close()

        result, events = await _dispatch_case(
            tmp_path,
            registry,
            label=label,
            scheduler_id=scheduler_id,
            scheduler_database=database,
        )
        artifact = next(
            event
            for event in events
            if event.event_type == "artifact.produced"
        )
        assert result.run is not None
        assert artifact.evidence_refs[0].producer == result.run.runner_id
        assert events[-1].event_type == "run.completed"
