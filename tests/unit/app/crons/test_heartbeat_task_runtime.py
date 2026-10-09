# -*- coding: utf-8 -*-
"""End-to-end Heartbeat execution through Schedule and Task facts."""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from qwenpaw.app.crons.contracts import HeartbeatExecutionRequest
from qwenpaw.app.crons.heartbeat_task_runtime import (
    HEARTBEAT_QUIET_RESULT,
    LiteHeartbeatTaskRuntime,
)
from qwenpaw.app.crons.task_runtime import LiteCronTaskRuntime
from qwenpaw.inbox import SQLiteInboxProjectionStore
from qwenpaw.plugins.generations import GenerationRegistry
from qwenpaw.scheduling import SQLiteSchedulerStore
from qwenpaw.tasks.bootstrap import task_service_for_workspace


class _ConsoleChannel:  # pylint: disable=too-few-public-methods
    def __init__(self, result: str) -> None:
        self.result = result
        self.payloads = []

    async def stream_one(self, payload):
        """Yield one completed Console response."""
        self.payloads.append(payload)
        yield (
            'data: {"object":"response","status":"completed",'
            '"output":[{"role":"assistant","content":['
            f'{{"type":"text","text":"{self.result}"}}]}}]}}\n\n'
        )


class _ChannelManager:
    def __init__(self, result: str) -> None:
        self.console = _ConsoleChannel(result)
        self.deliveries = []

    async def get_channel(self, name: str):
        """Return the configured test channel."""
        return self.console if name == "console" else None

    async def send_event(self, **kwargs) -> None:
        """Record external deliveries."""
        self.deliveries.append(kwargs)


class _ChatManager:  # pylint: disable=too-few-public-methods
    async def get_or_create_chat(self, **kwargs):
        """Return one stable test Conversation."""
        return SimpleNamespace(id="heartbeat-chat", **kwargs)


def _workspace(tmp_path: Path, result: str):
    return SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        capability_registry=GenerationRegistry(),
        channel_manager=_ChannelManager(result),
        chat_manager=_ChatManager(),
    )


async def _run(workspace, *, target: str):
    return await LiteHeartbeatTaskRuntime(workspace).execute(
        HeartbeatExecutionRequest(
            query_text="Check the workspace.",
            every="30m",
            target=target,
            timeout_seconds=30,
            trigger="scheduled",
            scheduled_for=datetime(2030, 1, 1, tzinfo=timezone.utc),
            channel="console",
            user_id="main",
            transport_context="main",
        ),
    )


@pytest.mark.asyncio
async def test_inbox_heartbeat_is_durable_and_idempotent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """One scheduled slot creates one Task and one local Inbox item."""
    monkeypatch.setattr("qwenpaw.constant.WORKING_DIR", tmp_path)
    workspace = _workspace(tmp_path, "Actionable change")

    result = await _run(workspace, target="inbox")
    replay = await _run(workspace, target="inbox")

    assert replay["task_id"] == result["task_id"]
    assert replay["run_id"] == result["run_id"]
    assert result["conversation_id"] == "heartbeat-chat"
    assert result["delivery_status"] == "success"
    assert len(workspace.channel_manager.console.payloads) == 1
    assert workspace.channel_manager.deliveries == []
    inbox = SQLiteInboxProjectionStore(
        tmp_path / ".qwenpaw" / "lite" / "inbox.db",
    )
    items = await inbox.list_items(agent_id="default")
    assert len(items) == 1
    assert items[0].summary == "Actionable change"
    service = task_service_for_workspace(workspace)
    task = await service.get_task(UUID(result["task_id"]))
    assert task is not None
    assert task.source.value == "schedule"


@pytest.mark.asyncio
async def test_quiet_heartbeat_keeps_task_without_inbox_item(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A quiet marker remains auditable but creates no notification."""
    monkeypatch.setattr("qwenpaw.constant.WORKING_DIR", tmp_path)
    workspace = _workspace(tmp_path, HEARTBEAT_QUIET_RESULT)

    result = await _run(workspace, target="inbox")

    assert result["delivery_status"] == "suppressed"
    inbox = SQLiteInboxProjectionStore(
        tmp_path / ".qwenpaw" / "lite" / "inbox.db",
    )
    assert await inbox.list_items(agent_id="default") == ()
    service = task_service_for_workspace(workspace)
    events = await service.list_events(UUID(result["task_id"]))
    assert events[-1].event_type == "run.completed"


@pytest.mark.asyncio
async def test_main_heartbeat_does_not_request_delivery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The legacy main target executes without external projection."""
    monkeypatch.setattr("qwenpaw.constant.WORKING_DIR", tmp_path)
    workspace = _workspace(tmp_path, "Background maintenance complete")

    result = await _run(workspace, target="main")

    assert result["delivery_status"] == "not_requested"
    assert workspace.channel_manager.deliveries == []
    inbox = SQLiteInboxProjectionStore(
        tmp_path / ".qwenpaw" / "lite" / "inbox.db",
    )
    assert await inbox.list_items(agent_id="default") == ()


@pytest.mark.asyncio
async def test_heartbeat_definition_and_cursor_are_hot_reconciled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Synchronization creates no Task and removal clears durable wakeup."""
    monkeypatch.setattr("qwenpaw.constant.WORKING_DIR", tmp_path)
    workspace = _workspace(tmp_path, "unused")
    runtime = LiteHeartbeatTaskRuntime(workspace)
    request = HeartbeatExecutionRequest(
        query_text="Check the workspace.",
        every="30m",
        target="main",
        timeout_seconds=30,
        trigger="scheduled",
        scheduled_for=datetime(2030, 1, 1, tzinfo=timezone.utc),
        channel="console",
        user_id="main",
        transport_context="main",
    )

    next_fire_at = await runtime.synchronize(request)

    store = SQLiteSchedulerStore(tmp_path / "scheduler.db")
    definitions = await store.list_definitions(agent_id="default")
    assert len(definitions) == 1
    assert definitions[0].metadata["source"] == "heartbeat"
    assert definitions[0].schedule_id == runtime.schedule_id()
    assert next_fire_at is not None
    assert workspace.channel_manager.console.payloads == []
    assert await runtime.remove() is True
    assert await store.list_definitions(agent_id="default") == ()
    assert (
        await store.get_cursor(
            agent_id="default",
            schedule_id=runtime.schedule_id(),
        )
        is None
    )


@pytest.mark.asyncio
async def test_restarted_worker_resumes_durable_heartbeat_cursor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A fresh Runtime instance consumes the persisted Heartbeat cursor."""
    monkeypatch.setattr("qwenpaw.constant.WORKING_DIR", tmp_path)
    workspace = _workspace(tmp_path, "Restart recovery complete")
    request = HeartbeatExecutionRequest(
        query_text="Check the workspace.",
        every="30m",
        target="main",
        timeout_seconds=30,
        trigger="scheduled",
        scheduled_for=datetime(2030, 1, 1, tzinfo=timezone.utc),
        channel="console",
        user_id="main",
        transport_context="main",
    )
    initial = LiteHeartbeatTaskRuntime(workspace)
    next_fire_at = await initial.synchronize(request)
    assert next_fire_at is not None

    restarted_heartbeat = LiteHeartbeatTaskRuntime(workspace)
    restarted_worker = LiteCronTaskRuntime(workspace)
    results = []

    async def execute(definition, occurrence: datetime) -> None:
        assert definition.metadata["source"] == "heartbeat"
        results.append(
            await restarted_heartbeat.execute(
                HeartbeatExecutionRequest(
                    query_text=request.query_text,
                    every=request.every,
                    target=request.target,
                    timeout_seconds=request.timeout_seconds,
                    trigger="scheduled",
                    scheduled_for=occurrence,
                    channel=request.channel,
                    user_id=request.user_id,
                    transport_context=request.transport_context,
                ),
            ),
        )

    report = await restarted_worker.run_due(
        now=next_fire_at,
        execute=execute,
    )

    assert report.outcomes[0].disposition.value == "dispatched"
    assert len(results) == 1
    assert results[0]["delivery_status"] == "not_requested"
    assert len(workspace.channel_manager.console.payloads) == 1
