# -*- coding: utf-8 -*-
"""Shared Task Delivery contract for system and plugin Adapters."""

import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from qwenpaw.delivery import (
    DeliveryDispatchDisposition,
    DeliveryDispatcher,
    SQLiteDeliveryProjectionStore,
    SYSTEM_INBOX_ADDRESS,
    TaskDeliveryProjector,
    TaskDeliveryWorker,
)
from qwenpaw.inbox import SQLiteInboxProjectionStore
from qwenpaw.kernel import (
    DeliveryDestination,
    DeliveryKind,
    DeliveryMode,
    DeliveryPolicy,
    PlanStep,
    RunnerSignal,
)
from qwenpaw.kernel.models import CapabilityBundle
from qwenpaw.kernel.ports import DeliveryAdapter
from qwenpaw.plugins.architecture import PluginManifest
from qwenpaw.plugins.generations import GenerationRegistry
from qwenpaw.tasks.event_application import TaskEventApplicationService
from qwenpaw.tasks.ledger import SQLiteExecutionLedger
from qwenpaw.tasks.service import TaskService
from qwenpaw.tasks.system_contributions import (
    SYSTEM_CAPABILITY_BUNDLE,
    SYSTEM_INBOX_DELIVERY_ID,
    system_contribution_factory,
)


def _system_bundle() -> CapabilityBundle:
    return SYSTEM_CAPABILITY_BUNDLE.model_copy(
        update={
            "contributions": tuple(
                contribution
                for contribution in SYSTEM_CAPABILITY_BUNDLE.contributions
                if contribution.contribution_id == "inbox-delivery"
            ),
        },
    )


def _plugin_root() -> Path:
    return (
        Path(__file__).parents[3]
        / "examples"
        / "plugins"
        / "delivery-provider"
    )


async def _completed_task(
    tmp_path: Path,
    *,
    generation: int,
    label: str,
) -> tuple[TaskService, object]:
    service = TaskService(
        store=SQLiteExecutionLedger(tmp_path / f"{label}-tasks.sqlite3"),
        registry_generation=generation,
    )
    task = await service.create_task(
        objective=f"Deliver the {label} result",
        agent_id="agent-contract",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Deliver", objective=task.objective),),
    )
    _, run = await service.start_task(
        task.task_id,
        runner_id="contract.runner",
        registry_generation=generation,
    )
    await service.record_runner_signal(
        task.task_id,
        run.run_id,
        RunnerSignal(
            event_type="conversation.assistant.delta",
            payload={"role": "assistant", "text": f"{label} result"},
        ),
    )
    await service.complete_task(task.task_id)
    return service, task


async def _deliver_case(
    tmp_path: Path,
    registry: GenerationRegistry,
    *,
    label: str,
    adapter_id: str,
    address: str,
    inbox: SQLiteInboxProjectionStore | None,
) -> tuple[Any, Any, Any]:
    service, task = await _completed_task(
        tmp_path,
        generation=registry.generation,
        label=label,
    )
    delivery_store = SQLiteDeliveryProjectionStore(
        tmp_path / f"{label}-delivery.sqlite3",
    )
    worker = TaskDeliveryWorker(
        events=TaskEventApplicationService(service),
        projector=TaskDeliveryProjector(service),
        dispatcher=DeliveryDispatcher(
            projection=delivery_store,
            capability_resolver=registry,
        ),
        inbox=inbox,
    )
    policy = DeliveryPolicy(
        destination=DeliveryDestination(
            adapter_id=adapter_id,
            address=address,
        ),
        mode=DeliveryMode.FINAL,
        kinds=(DeliveryKind.RESULT,),
    )
    first = await worker.follow(
        task.task_id,
        policy,
        owner_id=f"worker.{label}",
        poll_interval=0.001,
    )
    replay = await worker.follow(
        task.task_id,
        policy,
        owner_id=f"worker.{label}.replay",
        poll_interval=0.001,
    )

    assert len(first) == len(replay) == 1
    assert first[0].disposition is DeliveryDispatchDisposition.DELIVERED
    assert replay[0].disposition is DeliveryDispatchDisposition.REPLAYED
    assert first[0].attempt.delivery_id == replay[0].attempt.delivery_id
    assert first[0].attempt.receipt is not None
    assert first[0].attempt.receipt.adapter_id == adapter_id
    attempts = await delivery_store.list_attempts(
        first[0].attempt.delivery_id,
    )
    assert attempts == (first[0].attempt,)
    request = await delivery_store.get_request(
        first[0].attempt.delivery_id,
    )
    assert request is not None
    assert request.registry_generation == registry.generation
    assert request.invocation_id is not None
    assert request.correlation_id is not None
    assert request.payload["text"] == f"{label} result"
    return first[0], request, task


@pytest.mark.asyncio
async def test_system_and_plugin_adapters_share_task_delivery_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def resolve_workspace(_agent_id: str):
        raise AssertionError("Inbox Adapter must not resolve a Workspace")

    system_registry = GenerationRegistry()
    await system_registry.activate_bundle(
        _system_bundle(),
        system_contribution_factory(resolve_workspace),
    )

    delivery_log = tmp_path / "plugin-delivery.jsonl"
    plugin_root = _plugin_root()
    monkeypatch.syspath_prepend(str(plugin_root))
    monkeypatch.setenv("QWENPAW_EXAMPLE_DELIVERY_LOG", str(delivery_log))
    provider_module = importlib.import_module("delivery_provider.provider")
    manifest = PluginManifest.from_dict(
        json.loads(
            (plugin_root / "plugin.json").read_text(encoding="utf-8"),
        ),
    )
    plugin_registry = GenerationRegistry()
    await plugin_registry.activate(
        manifest,
        lambda _declaration: provider_module.create_adapter(),
    )

    inbox = SQLiteInboxProjectionStore(tmp_path / "inbox.sqlite3")
    cases = (
        (
            system_registry,
            "system",
            SYSTEM_INBOX_DELIVERY_ID,
            SYSTEM_INBOX_ADDRESS,
            inbox,
        ),
        (
            plugin_registry,
            "plugin",
            "delivery-provider.local-jsonl",
            "local-jsonl",
            None,
        ),
    )
    for registry, label, adapter_id, address, inbox_store in cases:
        lease = await registry.pin()
        descriptor = lease.resolve(adapter_id)
        assert descriptor is not None
        assert descriptor.slot == "delivery.adapter"
        assert isinstance(lease.implementation(adapter_id), DeliveryAdapter)
        await lease.close()
        await _deliver_case(
            tmp_path,
            registry,
            label=label,
            adapter_id=adapter_id,
            address=address,
            inbox=inbox_store,
        )

    inbox_items = await inbox.list_items(agent_id="agent-contract")
    assert len(inbox_items) == 1
    assert inbox_items[0].summary == "system result"
    assert inbox_items[0].invocation_id is not None
    assert inbox_items[0].correlation_id is not None
    log_lines = delivery_log.read_text(encoding="utf-8").splitlines()
    assert len(log_lines) == 1
    plugin_record = json.loads(log_lines[0])
    assert plugin_record["attempt"] == 1
    assert plugin_record["request"]["payload"]["text"] == "plugin result"
