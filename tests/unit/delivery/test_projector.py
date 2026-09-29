# -*- coding: utf-8 -*-
"""Tests for Task event projection and durable delivery following."""

from pathlib import Path

import pytest

from qwenpaw.delivery import (
    DeliveryDispatchDisposition,
    DeliveryDispatcher,
    SQLiteDeliveryProjectionStore,
    TaskDeliveryProjector,
    TaskDeliveryWorker,
)
from qwenpaw.kernel import (
    ActorRef,
    ActorType,
    ArtifactRef,
    CapabilityBundle,
    CapabilityContribution,
    CapabilityProviderKind,
    DeliveryDestination,
    DeliveryKind,
    DeliveryMode,
    DeliveryPolicy,
    DeliveryReceipt,
    DeliveryStatus,
    ExecutionEvent,
    PlanStep,
    RunnerSignal,
)
from qwenpaw.plugins.generations import GenerationRegistry
from qwenpaw.tasks.event_application import TaskEventApplicationService
from qwenpaw.tasks.ledger import SQLiteExecutionLedger
from qwenpaw.tasks.service import TaskService

_PROVIDER_ID = "projector-test"
_ADAPTER_ID = f"{_PROVIDER_ID}.delivery"


class _Adapter:
    adapter_id = _ADAPTER_ID

    def __init__(self) -> None:
        self.requests = []

    def supports(self, request) -> bool:
        return request.destination.adapter_id == self.adapter_id

    async def deliver(self, request, *, attempt):
        self.requests.append(request)
        return DeliveryReceipt(
            delivery_id=request.delivery_id,
            adapter_id=self.adapter_id,
            status=DeliveryStatus.DELIVERED,
            attempt=attempt,
        )


class _FailingAdapter(_Adapter):
    async def deliver(self, request, *, attempt):
        del request, attempt
        raise TimeoutError("external acknowledgement unavailable")


async def _completed_task(
    tmp_path: Path,
    *,
    generation: int,
) -> tuple[TaskService, object]:
    ledger = SQLiteExecutionLedger(tmp_path / "tasks.db")
    await ledger.initialize()
    service = TaskService(
        store=ledger,
        registry_generation=generation,
    )
    task = await service.create_task(
        objective="Deliver the result",
        agent_id="agent-a",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Deliver", objective=task.objective),),
    )
    _, run = await service.start_task(
        task.task_id,
        runner_id="runner.local",
        registry_generation=generation,
    )
    await service.record_runner_signal(
        task.task_id,
        run.run_id,
        RunnerSignal(
            event_type="conversation.assistant.delta",
            payload={"role": "assistant", "text": "first "},
        ),
    )
    await service.record_runner_signal(
        task.task_id,
        run.run_id,
        RunnerSignal(
            event_type="conversation.assistant.delta",
            payload={"role": "assistant", "text": "second"},
        ),
    )
    await service.record_runner_signal(
        task.task_id,
        run.run_id,
        RunnerSignal(
            event_type="conversation.assistant.completed",
            payload={"role": "assistant", "text": "first second"},
        ),
    )
    await service.complete_task(task.task_id)
    return service, task


def _policy(mode: DeliveryMode) -> DeliveryPolicy:
    return DeliveryPolicy(
        destination=DeliveryDestination(
            adapter_id=_ADAPTER_ID,
            address="channel-address",
            conversation_id="chat-a",
        ),
        mode=mode,
    )


def _bundle(version: str) -> CapabilityBundle:
    return CapabilityBundle(
        provider_id=_PROVIDER_ID,
        provider_kind=CapabilityProviderKind.PLUGIN,
        version=version,
        contributions=(
            CapabilityContribution(
                contribution_id="delivery",
                slot="delivery.adapter",
                entrypoint="tests.projector:delivery",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_final_projection_reconstructs_committed_response(
    tmp_path: Path,
) -> None:
    service, task = await _completed_task(tmp_path, generation=7)
    events = await service.list_events(task.task_id)
    terminal = events[-1]

    requests = await TaskDeliveryProjector(service).project(
        terminal,
        _policy(DeliveryMode.FINAL),
    )

    assert len(requests) == 1
    request = requests[0]
    assert request.kind is DeliveryKind.RESULT
    assert request.agent_id == "agent-a"
    assert request.registry_generation == 7
    assert request.payload["text"] == "first second"
    assert request.task_id == task.task_id
    assert request.run_id == terminal.run_id
    assert request.invocation_id == terminal.invocation_id
    assert request.correlation_id == terminal.correlation_id


@pytest.mark.asyncio
async def test_stream_projection_uses_only_committed_completed_response(
    tmp_path: Path,
) -> None:
    service, task = await _completed_task(tmp_path, generation=7)
    events = await service.list_events(task.task_id)
    deltas = [
        event
        for event in events
        if event.event_type == "conversation.assistant.delta"
    ]
    completed = next(
        event
        for event in events
        if event.event_type == "conversation.assistant.completed"
    )
    projector = TaskDeliveryProjector(service)
    policy = _policy(DeliveryMode.STREAM)

    for delta in deltas:
        assert await projector.project(delta, policy) == ()
    requests = await projector.project(completed, policy)

    assert len(requests) == 1
    assert requests[0].kind is DeliveryKind.REPLY
    assert requests[0].mode is DeliveryMode.STREAM
    assert requests[0].payload == {
        "text": "first second",
        "event_type": "conversation.assistant.completed",
        "source": {
            "role": "assistant",
            "text": "first second",
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["tool.started", "tool.completed"])
async def test_stream_projection_maps_public_tool_activity(
    tmp_path: Path,
    event_type: str,
) -> None:
    ledger = SQLiteExecutionLedger(tmp_path / "tasks.db")
    await ledger.initialize()
    service = TaskService(store=ledger, registry_generation=3)
    task = await service.create_task(
        objective="Project tool activity",
        agent_id="agent-a",
    )
    event = ExecutionEvent(
        task_id=task.task_id,
        run_id=None,
        sequence=2,
        event_type=event_type,
        registry_generation=3,
        actor=ActorRef(type=ActorType.SYSTEM, id="tests"),
        payload={
            "call_id": "call-1",
            "name": "read_file",
            "arguments": '{"path":"README.md"}',
            "output": "done",
        },
    )

    requests = await TaskDeliveryProjector(service).project(
        event,
        _policy(DeliveryMode.STREAM),
    )

    assert len(requests) == 1
    assert requests[0].kind is DeliveryKind.ACTIVITY
    assert requests[0].payload["event_type"] == event_type
    assert requests[0].payload["source"] == event.payload


@pytest.mark.asyncio
async def test_embedded_reply_media_does_not_emit_duplicate_artifact_notice(
    tmp_path: Path,
) -> None:
    ledger = SQLiteExecutionLedger(tmp_path / "tasks.db")
    await ledger.initialize()
    service = TaskService(store=ledger, registry_generation=3)
    task = await service.create_task(
        objective="Project embedded media",
        agent_id="agent-a",
    )
    artifact = ArtifactRef(
        kind="conversation.image",
        uri=f"qwenpaw-artifact://sha256/{'6' * 64}",
        media_type="image/png",
        content_hash=f"sha256:{'6' * 64}",
        size_bytes=5,
        metadata={
            "delivery_disposition": "embedded",
            "delivery_content_type": "image",
        },
    )
    produced = ExecutionEvent(
        task_id=task.task_id,
        sequence=2,
        event_type="artifact.produced",
        registry_generation=3,
        actor=ActorRef(type=ActorType.RUNNER, id="runner.local"),
        artifact_refs=(artifact,),
    )
    completed = ExecutionEvent(
        task_id=task.task_id,
        sequence=3,
        event_type="conversation.assistant.completed",
        registry_generation=3,
        actor=ActorRef(type=ActorType.RUNNER, id="runner.local"),
        payload={
            "role": "assistant",
            "text": "",
            "content": [
                {
                    "type": "image",
                    "artifact_id": str(artifact.artifact_id),
                },
            ],
        },
        artifact_refs=(artifact,),
    )
    projector = TaskDeliveryProjector(service)
    policy = _policy(DeliveryMode.STREAM)

    assert await projector.project(produced, policy) == ()
    result_projection = produced.model_copy(
        update={
            "artifact_refs": (
                artifact.model_copy(
                    update={
                        "metadata": {
                            "delivery_disposition": "result_projection",
                        },
                    },
                ),
            ),
        },
    )
    assert await projector.project(result_projection, policy) == ()
    requests = await projector.project(completed, policy)

    assert len(requests) == 1
    assert requests[0].kind is DeliveryKind.REPLY
    assert requests[0].artifact_refs == (artifact,)


@pytest.mark.asyncio
async def test_final_projection_suppresses_exact_quiet_result(
    tmp_path: Path,
) -> None:
    """A policy may keep a completed Task while omitting its Delivery."""
    service, task = await _completed_task(tmp_path, generation=7)
    terminal = (await service.list_events(task.task_id))[-1]
    base = _policy(DeliveryMode.FINAL)
    policy = base.model_copy(
        update={"suppress_exact_text": ("first second",)},
    )

    requests = await TaskDeliveryProjector(service).project(
        terminal,
        policy,
    )

    assert requests == ()
    stored = await service.get_task(task.task_id)
    assert stored is not None
    assert stored.status.value == "completed"


@pytest.mark.asyncio
async def test_plugin_result_without_conversation_text_has_fallback(
    tmp_path: Path,
) -> None:
    ledger = SQLiteExecutionLedger(tmp_path / "tasks.db")
    await ledger.initialize()
    service = TaskService(store=ledger, registry_generation=1)
    task = await service.create_task(
        objective="Deliver an artifact-only plugin result",
        agent_id="agent-a",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Deliver", objective=task.objective),),
    )
    _, run = await service.start_task(
        task.task_id,
        runner_id="plugin.artifact-only",
    )
    artifact = ArtifactRef(
        kind="report",
        uri="qwenpaw-artifact://sha256/plugin-report",
        media_type="text/plain",
        content_hash="4" * 64,
        size_bytes=6,
    )
    await service.record_runner_signal(
        task.task_id,
        run.run_id,
        RunnerSignal(
            event_type="artifact.created",
            artifact_refs=(artifact,),
        ),
    )
    await service.complete_task(task.task_id)
    terminal = (await service.list_events(task.task_id))[-1]

    requests = await TaskDeliveryProjector(service).project(
        terminal,
        _policy(DeliveryMode.FINAL),
    )

    assert len(requests) == 1
    assert requests[0].kind is DeliveryKind.RESULT
    assert requests[0].payload["text"] == "Task completed"

    quiet_policy = _policy(DeliveryMode.FINAL).model_copy(
        update={"suppress_empty_text": True},
    )
    assert (
        await TaskDeliveryProjector(service).project(
            terminal,
            quiet_policy,
        )
        == ()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "kind", "payload", "artifact_refs"),
    (
        (
            "approval.requested",
            DeliveryKind.APPROVAL,
            {"action": "shell.execute", "risk": "high"},
            (),
        ),
        (
            "run.failed",
            DeliveryKind.EXCEPTION,
            {"error_summary": "RuntimeProcessLost"},
            (),
        ),
        (
            "artifact.created",
            DeliveryKind.ARTIFACT_READY,
            {},
            (
                ArtifactRef(
                    kind="report",
                    uri="qwenpaw-artifact://sha256/ready",
                    media_type="text/plain",
                    content_hash="5" * 64,
                    size_bytes=5,
                ),
            ),
        ),
    ),
)
async def test_projector_maps_non_reply_delivery_facts(
    tmp_path: Path,
    event_type: str,
    kind: DeliveryKind,
    payload: dict,
    artifact_refs: tuple[ArtifactRef, ...],
) -> None:
    ledger = SQLiteExecutionLedger(tmp_path / "tasks.db")
    await ledger.initialize()
    service = TaskService(store=ledger, registry_generation=3)
    task = await service.create_task(
        objective="Project committed facts",
        agent_id="agent-a",
    )
    event = ExecutionEvent(
        task_id=task.task_id,
        run_id=None,
        sequence=2,
        event_type=event_type,
        registry_generation=3,
        actor=ActorRef(type=ActorType.SYSTEM, id="tests"),
        payload=payload,
        artifact_refs=artifact_refs,
    )
    policy = _policy(DeliveryMode.FINAL).model_copy(
        update={"kinds": (kind,)},
    )

    requests = await TaskDeliveryProjector(service).project(event, policy)

    assert len(requests) == 1
    assert requests[0].kind is kind
    assert requests[0].source_event_id == event.event_id
    assert requests[0].artifact_refs == artifact_refs


@pytest.mark.asyncio
async def test_progress_event_never_creates_external_delivery(
    tmp_path: Path,
) -> None:
    ledger = SQLiteExecutionLedger(tmp_path / "tasks.db")
    await ledger.initialize()
    service = TaskService(store=ledger, registry_generation=1)
    task = await service.create_task(
        objective="Keep progress internal",
        agent_id="agent-a",
    )
    event = ExecutionEvent(
        task_id=task.task_id,
        sequence=2,
        event_type="runner.progress",
        registry_generation=1,
        actor=ActorRef(type=ActorType.RUNNER, id="runner.local"),
        payload={"percent": 50},
    )

    requests = await TaskDeliveryProjector(service).project(
        event,
        _policy(DeliveryMode.FINAL),
    )

    assert requests == ()


@pytest.mark.asyncio
async def test_stream_projection_never_exposes_transient_text_delta(
    tmp_path: Path,
) -> None:
    service, task = await _completed_task(tmp_path, generation=3)
    events = await service.list_events(task.task_id)
    delta = next(
        event
        for event in events
        if event.event_type == "conversation.assistant.delta"
    )

    requests = await TaskDeliveryProjector(service).project(
        delta,
        _policy(DeliveryMode.STREAM),
    )

    assert requests == ()


@pytest.mark.asyncio
async def test_worker_replays_terminal_task_through_one_adapter(
    tmp_path: Path,
) -> None:
    registry = GenerationRegistry()
    adapter = _Adapter()
    bundle = CapabilityBundle(
        provider_id=_PROVIDER_ID,
        provider_kind=CapabilityProviderKind.PLUGIN,
        version="1.0.0",
        contributions=(
            CapabilityContribution(
                contribution_id="delivery",
                slot="delivery.adapter",
                entrypoint="tests.projector:delivery",
            ),
        ),
    )
    await registry.activate_bundle(bundle, lambda _: adapter)
    service, task = await _completed_task(
        tmp_path,
        generation=registry.generation,
    )
    worker = TaskDeliveryWorker(
        events=TaskEventApplicationService(service),
        projector=TaskDeliveryProjector(service),
        dispatcher=DeliveryDispatcher(
            projection=SQLiteDeliveryProjectionStore(
                tmp_path / "delivery.db",
            ),
            capability_resolver=registry,
        ),
    )

    results = await worker.follow(
        task.task_id,
        _policy(DeliveryMode.FINAL),
        owner_id="delivery-worker",
        poll_interval=0,
    )

    assert len(results) == 1
    assert results[0].disposition is DeliveryDispatchDisposition.DELIVERED
    assert len(adapter.requests) == 1
    assert adapter.requests[0].payload["text"] == "first second"


@pytest.mark.asyncio
async def test_delivery_failure_never_mutates_task_source_facts(
    tmp_path: Path,
) -> None:
    registry = GenerationRegistry()
    adapter = _FailingAdapter()
    await registry.activate_bundle(_bundle("1.0.0"), lambda _: adapter)
    service, task = await _completed_task(
        tmp_path,
        generation=registry.generation,
    )
    before = await service.projection_snapshot(task.task_id)
    worker = TaskDeliveryWorker(
        events=TaskEventApplicationService(service),
        projector=TaskDeliveryProjector(service),
        dispatcher=DeliveryDispatcher(
            projection=SQLiteDeliveryProjectionStore(
                tmp_path / "delivery.db",
            ),
            capability_resolver=registry,
        ),
    )

    results = await worker.follow(
        task.task_id,
        _policy(DeliveryMode.FINAL),
        owner_id="delivery-worker",
        poll_interval=0,
    )
    after = await service.projection_snapshot(task.task_id)

    assert len(results) == 1
    assert results[0].disposition is (DeliveryDispatchDisposition.UNCERTAIN)
    assert before == after
