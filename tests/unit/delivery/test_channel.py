# -*- coding: utf-8 -*-
"""Tests for the system adapter over existing ChannelManager contracts."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from qwenpaw.delivery import (
    SYSTEM_CHANNEL_DELIVERY_ID,
    SystemChannelDeliveryAdapter,
    encode_channel_address,
)
from qwenpaw.kernel import (
    ArtifactRef,
    DeliveryDestination,
    DeliveryKind,
    DeliveryMode,
    DeliveryRequest,
    DeliveryStatus,
    PlanStep,
    RunnerSignal,
)
from qwenpaw.schemas import Message, MessageType, Role, RunStatus
from qwenpaw.tasks.artifacts import lite_artifact_store
from qwenpaw.tasks.ledger import SQLiteExecutionLedger
from qwenpaw.tasks.service import TaskService


def _request(
    address: str,
    *,
    kind: DeliveryKind = DeliveryKind.RESULT,
    mode: DeliveryMode = DeliveryMode.FINAL,
    payload: dict | None = None,
    task_id=None,
    artifact_refs: tuple[ArtifactRef, ...] = (),
) -> DeliveryRequest:
    return DeliveryRequest(
        source_event_id="00000000-0000-0000-0000-000000000001",
        idempotency_key="task:event:channel",
        agent_id="default",
        kind=kind,
        mode=mode,
        destination=DeliveryDestination(
            adapter_id=SYSTEM_CHANNEL_DELIVERY_ID,
            address=address,
        ),
        task_id=task_id,
        artifact_refs=artifact_refs,
        payload=payload or {"text": "Task completed"},
    )


@pytest.mark.asyncio
async def test_channel_adapter_sends_one_completed_message() -> None:
    manager = SimpleNamespace(
        get_channel=AsyncMock(return_value=object()),
        send_event=AsyncMock(),
    )

    async def resolve_workspace(agent_id: str):
        assert agent_id == "default"
        return SimpleNamespace(channel_manager=manager)

    address = encode_channel_address(
        channel="console",
        user_id="cron-user",
        transport_context="legacy-transport-context",
    )
    request = _request(address)
    invocation_id = uuid4()
    correlation_id = uuid4()
    request = request.model_copy(
        update={
            "invocation_id": invocation_id,
            "correlation_id": correlation_id,
            "destination": request.destination.model_copy(
                update={
                    "metadata": {
                        "channel_meta": {
                            "suppress_console_push": True,
                            "thread_id": "thread-1",
                        },
                    },
                },
            ),
        },
    )
    adapter = SystemChannelDeliveryAdapter(resolve_workspace)

    receipt = await adapter.deliver(request, attempt=1)

    assert receipt.status is DeliveryStatus.DELIVERED
    manager.send_event.assert_awaited_once()
    call = manager.send_event.await_args.kwargs
    assert call["channel"] == "console"
    assert call["user_id"] == "cron-user"
    assert call["session_id"] == "legacy-transport-context"
    assert call["meta"] == {
        "suppress_console_push": True,
        "thread_id": "thread-1",
        "source": "delivery_projection",
    }
    assert isinstance(call["event"], Message)
    assert call["event"].object == "message"
    assert call["event"].status is RunStatus.Completed
    assert call["event"].content[0].text == "Task completed"
    assert call["event"].metadata["invocation_id"] == str(invocation_id)
    assert call["event"].metadata["correlation_id"] == str(correlation_id)
    assert "session_id" not in request.model_dump(mode="json")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "message_type", "role", "field", "value"),
    (
        (
            "tool.started",
            MessageType.FUNCTION_CALL,
            Role.ASSISTANT,
            "arguments",
            '{"path":"README.md"}',
        ),
        (
            "tool.completed",
            MessageType.FUNCTION_CALL_OUTPUT,
            Role.TOOL,
            "output",
            "done",
        ),
    ),
)
async def test_channel_adapter_maps_stream_tool_activity(
    event_type: str,
    message_type: MessageType,
    role: Role,
    field: str,
    value: str,
) -> None:
    manager = SimpleNamespace(
        get_channel=AsyncMock(return_value=object()),
        send_event=AsyncMock(),
    )

    async def resolve_workspace(_agent_id: str):
        return SimpleNamespace(channel_manager=manager)

    address = encode_channel_address(
        channel="console",
        user_id="cron-user",
        transport_context="transport",
    )
    source = {
        "call_id": "call-1",
        "name": "read_file",
        field: value,
    }
    request = _request(
        address,
        kind=DeliveryKind.ACTIVITY,
        mode=DeliveryMode.STREAM,
        payload={
            "text": "read_file",
            "event_type": event_type,
            "source": source,
        },
    )

    receipt = await SystemChannelDeliveryAdapter(
        resolve_workspace,
    ).deliver(request, attempt=1)

    assert receipt.status is DeliveryStatus.DELIVERED
    message = manager.send_event.await_args.kwargs["event"]
    assert message.type is message_type
    assert message.object == "message"
    assert message.role is role
    assert message.status is RunStatus.Completed
    assert message.content[0].data["call_id"] == "call-1"
    assert message.content[0].data["name"] == "read_file"
    assert message.content[0].data[field] == value


@pytest.mark.asyncio
async def test_channel_adapter_maps_stream_reply_as_completed_message() -> (
    None
):
    manager = SimpleNamespace(
        get_channel=AsyncMock(return_value=object()),
        send_event=AsyncMock(),
    )

    async def resolve_workspace(_agent_id: str):
        return SimpleNamespace(channel_manager=manager)

    request = _request(
        encode_channel_address(
            channel="console",
            user_id="cron-user",
            transport_context="transport",
        ),
        kind=DeliveryKind.REPLY,
        mode=DeliveryMode.STREAM,
        payload={
            "text": "Committed answer",
            "event_type": "conversation.assistant.completed",
            "source": {"text": "Committed answer"},
        },
    )

    receipt = await SystemChannelDeliveryAdapter(
        resolve_workspace,
    ).deliver(request, attempt=1)

    assert receipt.status is DeliveryStatus.DELIVERED
    message = manager.send_event.await_args.kwargs["event"]
    assert message.type is MessageType.MESSAGE
    assert message.role is Role.ASSISTANT
    assert message.status is RunStatus.Completed
    assert message.content[0].text == "Committed answer"


@pytest.mark.asyncio
async def test_channel_adapter_materializes_ordered_artifact_media(
    tmp_path,
) -> None:
    ledger = SQLiteExecutionLedger(tmp_path / ".qwenpaw/lite/tasks.db")
    await ledger.initialize()
    service = TaskService(store=ledger, registry_generation=5)
    task = await service.create_task(
        objective="Deliver media",
        agent_id="default",
        metadata={"workspace_dir": str(tmp_path)},
    )
    store = lite_artifact_store(tmp_path)
    media = (
        await store.put(
            kind="conversation.image",
            media_type="image/png",
            content=b"image",
            metadata={"name": "image.png"},
        ),
        await store.put(
            kind="conversation.audio",
            media_type="audio/mpeg",
            content=b"audio",
            metadata={"name": "audio.mp3"},
        ),
        await store.put(
            kind="conversation.video",
            media_type="video/mp4",
            content=b"video",
            metadata={"name": "video.mp4"},
        ),
        await store.put(
            kind="conversation.file",
            media_type="text/plain",
            content=b"file",
            metadata={"name": "result.txt"},
        ),
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Deliver", objective=task.objective),),
    )
    _, media_run = await service.start_task(
        task.task_id,
        runner_id="runner.media",
    )
    await service.record_runner_signal(
        task.task_id,
        media_run.run_id,
        RunnerSignal(
            event_type="artifact.produced",
            artifact_refs=media,
        ),
    )
    manager = SimpleNamespace(
        get_channel=AsyncMock(return_value=object()),
        send_event=AsyncMock(),
    )
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        channel_manager=manager,
        _lite_task_service=service,
    )

    async def resolve_workspace(_agent_id: str):
        return workspace

    content = [{"type": "text", "text": "Media result"}]
    for part_type, artifact in zip(
        ("image", "audio", "video", "file"),
        media,
        strict=True,
    ):
        content.append(
            {
                "type": part_type,
                "artifact_id": str(artifact.artifact_id),
                "filename": artifact.metadata["name"],
            },
        )
    request = _request(
        encode_channel_address(
            channel="console",
            user_id="cron-user",
            transport_context="transport",
        ),
        kind=DeliveryKind.REPLY,
        mode=DeliveryMode.STREAM,
        task_id=task.task_id,
        artifact_refs=media,
        payload={
            "text": "Media result",
            "event_type": "conversation.assistant.completed",
            "source": {
                "role": "assistant",
                "text": "Media result",
                "content": content,
            },
        },
    )

    receipt = await SystemChannelDeliveryAdapter(
        resolve_workspace,
    ).deliver(request, attempt=1)

    assert receipt.status is DeliveryStatus.DELIVERED
    message = manager.send_event.await_args.kwargs["event"]
    assert [part.type.value for part in message.content] == [
        "text",
        "image",
        "audio",
        "video",
        "file",
    ]
    assert message.content[1].image_url == ("data:image/png;base64,aW1hZ2U=")
    assert message.content[2].data == ("data:audio/mpeg;base64,YXVkaW8=")
    assert message.content[3].video_url == ("data:video/mp4;base64,dmlkZW8=")
    assert message.content[4].file_url == ("data:text/plain;base64,ZmlsZQ==")
    assert message.content[4].filename == "result.txt"
    next_task = await service.create_task(
        objective="Use current generation",
        agent_id="default",
        metadata={"workspace_dir": str(tmp_path)},
    )
    await service.plan_task(
        next_task.task_id,
        steps=(PlanStep(title="Next", objective=next_task.objective),),
    )
    _, run = await service.start_task(
        next_task.task_id,
        runner_id="runner.next",
    )
    assert run.registry_generation == 5


@pytest.mark.asyncio
async def test_channel_adapter_fails_before_send_for_missing_artifact(
    tmp_path,
) -> None:
    ledger = SQLiteExecutionLedger(tmp_path / ".qwenpaw/lite/tasks.db")
    await ledger.initialize()
    service = TaskService(store=ledger, registry_generation=1)
    task = await service.create_task(
        objective="Deliver missing media",
        agent_id="default",
        metadata={"workspace_dir": str(tmp_path)},
    )
    missing = ArtifactRef(
        kind="conversation.image",
        uri=f"qwenpaw-artifact://sha256/{'a' * 64}",
        media_type="image/png",
        content_hash=f"sha256:{'a' * 64}",
        size_bytes=5,
    )
    manager = SimpleNamespace(
        get_channel=AsyncMock(return_value=object()),
        send_event=AsyncMock(),
    )
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        channel_manager=manager,
        _lite_task_service=service,
    )

    async def resolve_workspace(_agent_id: str):
        return workspace

    request = _request(
        encode_channel_address(
            channel="console",
            user_id="cron-user",
            transport_context="transport",
        ),
        kind=DeliveryKind.REPLY,
        mode=DeliveryMode.STREAM,
        task_id=task.task_id,
        artifact_refs=(missing,),
        payload={
            "text": "",
            "event_type": "conversation.assistant.completed",
            "source": {
                "content": [
                    {
                        "type": "image",
                        "artifact_id": str(missing.artifact_id),
                    },
                ],
            },
        },
    )

    receipt = await SystemChannelDeliveryAdapter(
        resolve_workspace,
    ).deliver(request, attempt=1)

    assert receipt.status is DeliveryStatus.FAILED
    assert receipt.error_code == "artifact_unavailable"
    manager.send_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_channel_adapter_returns_known_preflight_failure() -> None:
    manager = SimpleNamespace(
        get_channel=AsyncMock(return_value=None),
        send_event=AsyncMock(),
    )

    async def resolve_workspace(_agent_id: str):
        return SimpleNamespace(channel_manager=manager)

    request = _request(
        encode_channel_address(
            channel="missing",
            user_id="cron-user",
            transport_context="transport",
        ),
    )
    adapter = SystemChannelDeliveryAdapter(resolve_workspace)

    receipt = await adapter.deliver(request, attempt=1)

    assert receipt.status is DeliveryStatus.FAILED
    assert receipt.error_code == "channel_not_found"
    manager.send_event.assert_not_awaited()


def test_channel_address_rejects_empty_coordinates() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        encode_channel_address(
            channel="console",
            user_id="",
            transport_context="transport",
        )
