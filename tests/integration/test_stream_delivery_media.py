# -*- coding: utf-8 -*-
"""Real Lite stream delivery over Ledger, Worker, and ConsoleChannel."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from qwenpaw.app.channels.console.channel import ConsoleChannel
from qwenpaw.delivery import (
    DeliveryDispatcher,
    SQLiteDeliveryProjectionStore,
    TaskDeliveryProjector,
    TaskDeliveryWorker,
    encode_channel_address,
)
from qwenpaw.kernel import (
    DeliveryDestination,
    DeliveryKind,
    DeliveryMode,
    DeliveryPolicy,
    PlanStep,
    RunnerSignal,
)
from qwenpaw.plugins.generations import GenerationRegistry
from qwenpaw.tasks.artifacts import lite_artifact_store
from qwenpaw.tasks.event_application import TaskEventApplicationService
from qwenpaw.tasks.ledger import SQLiteExecutionLedger
from qwenpaw.tasks.service import TaskService
from qwenpaw.tasks.system_contributions import (
    SYSTEM_CAPABILITY_BUNDLE,
    SYSTEM_CHANNEL_DELIVERY_ID,
    system_contribution_factory,
)


class _RealConsoleManager:
    def __init__(self, channel: ConsoleChannel) -> None:
        self._channel = channel

    async def get_channel(self, name: str):
        return self._channel if name == "console" else None

    async def send_event(self, **kwargs) -> None:
        meta = dict(kwargs.get("meta") or {})
        meta["session_id"] = kwargs["session_id"]
        meta["user_id"] = kwargs["user_id"]
        await self._channel.send_event(
            user_id=kwargs["user_id"],
            session_id=kwargs["session_id"],
            event=kwargs["event"],
            meta=meta,
        )


@pytest.mark.asyncio
async def test_stream_media_replays_once_through_real_console_channel(
    tmp_path: Path,
    monkeypatch,
) -> None:
    push = AsyncMock()
    monkeypatch.setattr(
        "qwenpaw.app.channels.console.channel.push_store_append",
        push,
    )
    channel = ConsoleChannel(
        process=MagicMock(),
        enabled=True,
        bot_prefix="",
        media_dir=str(tmp_path),
    )
    print_parts = MagicMock(wraps=channel._print_parts)
    monkeypatch.setattr(channel, "_print_parts", print_parts)
    manager = _RealConsoleManager(channel)
    ledger = SQLiteExecutionLedger(tmp_path / ".qwenpaw/lite/tasks.db")
    await ledger.initialize()
    service = TaskService(store=ledger, registry_generation=1)
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        channel_manager=manager,
        _lite_task_service=service,
    )

    async def resolve_workspace(_agent_id: str):
        return workspace

    registry = GenerationRegistry()
    await registry.activate_bundle(
        SYSTEM_CAPABILITY_BUNDLE,
        system_contribution_factory(resolve_workspace),
    )
    task = await service.create_task(
        objective="Deliver one image",
        agent_id="default",
        metadata={"workspace_dir": str(tmp_path)},
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Deliver", objective=task.objective),),
    )
    _, run = await service.start_task(
        task.task_id,
        runner_id="runner.media",
        registry_generation=registry.generation,
    )
    artifact = await lite_artifact_store(tmp_path).put(
        kind="conversation.image",
        media_type="image/png",
        content=b"image",
        metadata={
            "name": "image.png",
            "delivery_disposition": "embedded",
            "delivery_content_type": "image",
        },
    )
    await service.record_runner_signal(
        task.task_id,
        run.run_id,
        RunnerSignal(
            event_type="artifact.produced",
            artifact_refs=(artifact,),
        ),
    )
    await service.record_runner_signal(
        task.task_id,
        run.run_id,
        RunnerSignal(
            event_type="conversation.assistant.completed",
            payload={
                "role": "assistant",
                "text": "Media result",
                "content": [
                    {"type": "text", "text": "Media result"},
                    {
                        "type": "image",
                        "artifact_id": str(artifact.artifact_id),
                        "filename": "image.png",
                    },
                ],
            },
            artifact_refs=(artifact,),
        ),
    )
    await service.complete_task(task.task_id)
    policy = DeliveryPolicy(
        destination=DeliveryDestination(
            adapter_id=SYSTEM_CHANNEL_DELIVERY_ID,
            address=encode_channel_address(
                channel="console",
                user_id="cron-user",
                transport_context="console:cron-user",
            ),
            metadata={
                "channel_meta": {"suppress_console_push": True},
            },
        ),
        mode=DeliveryMode.STREAM,
        kinds=(
            DeliveryKind.REPLY,
            DeliveryKind.ARTIFACT_READY,
        ),
    )
    worker = TaskDeliveryWorker(
        events=TaskEventApplicationService(service),
        projector=TaskDeliveryProjector(service),
        dispatcher=DeliveryDispatcher(
            projection=SQLiteDeliveryProjectionStore(
                tmp_path / ".qwenpaw/lite/delivery.db",
            ),
            capability_resolver=registry,
        ),
    )

    deliveries = await worker.follow(
        task.task_id,
        policy,
        owner_id="integration.console",
        poll_interval=0.01,
    )

    assert len(deliveries) == 1
    receipt = deliveries[0].attempt.receipt
    assert receipt is not None
    assert receipt.status.value == "delivered"
    print_parts.assert_called_once()
    parts = print_parts.call_args.args[0]
    assert parts[0].text == "Media result"
    assert parts[1].image_url == "data:image/png;base64,aW1hZ2U="
    push.assert_not_awaited()
