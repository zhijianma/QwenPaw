# -*- coding: utf-8 -*-
"""Contract tests for built-ins published through the shared catalog."""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from qwenpaw.kernel.models import (
    ExecutionCheckpoint,
    PlanStep,
    Run,
    RunStatus,
    RuntimeStrategyDirective,
    TaskOrder,
    TaskStatus,
)
from qwenpaw.kernel import (
    CostAccountingMode,
    ScheduleDefinition,
    ScheduleFire,
    ScheduleTrigger,
    SchedulerPort,
    SchedulerProvider,
)
from qwenpaw.harnesses.events import HarnessEvent, HarnessEventKind
from qwenpaw.kernel.ports import (
    ArtifactRenderer,
    RuntimeStrategy,
    TaskPlanner,
    TaskRunner,
)
from qwenpaw.capabilities.system_tools import (
    SYSTEM_TOOL_CAPABILITY_BUNDLE,
    system_tool_contribution_factory,
)
from qwenpaw.plugins.generations import GenerationRegistry
from qwenpaw.scheduling import SQLiteSchedulerStore
from qwenpaw.tasks.system_contributions import (
    SYSTEM_BASIC_PLANNER_ID,
    SYSTEM_CAPABILITY_BUNDLE,
    SYSTEM_CODING_STRATEGY_ID,
    SYSTEM_CODEX_RUNNER_ID,
    SYSTEM_CONSOLE_RUNNER_ID,
    SYSTEM_DEFAULT_STRATEGY_ID,
    SYSTEM_GOAL_STRATEGY_ID,
    SYSTEM_INBOX_DELIVERY_ID,
    SYSTEM_MISSION_STRATEGY_ID,
    SYSTEM_LOCAL_SCHEDULER_ID,
    SYSTEM_QODER_RUNNER_ID,
    SYSTEM_SAFE_ARTIFACT_RENDERER_ID,
    system_contribution_factory,
)
from qwenpaw.tasks.cancellation import RuntimeCancellationToken
from qwenpaw.tasks.artifacts import lite_artifact_store
from qwenpaw.tasks.context import runtime_context_from_order
from qwenpaw.tasks.ledger import SQLiteExecutionLedger
from qwenpaw.tasks.runner import TaskExecutionCoordinator
from qwenpaw.tasks.service import TaskService


@pytest.mark.asyncio
async def test_task_and_chat_system_bundles_do_not_replace_each_other(
    tmp_path: Path,
) -> None:
    async def resolve_workspace(_agent_id: str):
        return SimpleNamespace(
            agent_id="default",
            workspace_dir=tmp_path,
        )

    registry = GenerationRegistry()
    await registry.ensure_bundle(
        SYSTEM_CAPABILITY_BUNDLE,
        system_contribution_factory(resolve_workspace),
    )
    await registry.ensure_bundle(
        SYSTEM_TOOL_CAPABILITY_BUNDLE,
        system_tool_contribution_factory,
    )
    await registry.ensure_bundle(
        SYSTEM_CAPABILITY_BUNDLE,
        system_contribution_factory(resolve_workspace),
    )
    lease = await registry.pin()

    assert lease.resolve(SYSTEM_DEFAULT_STRATEGY_ID) is not None
    assert lease.resolve("qwenpaw.system.workspace-tools") is not None
    await lease.close()


class _ConsoleChannel:
    def __init__(self) -> None:
        self.payloads = []

    async def stream_one(self, payload):
        self.payloads.append(payload)
        yield (
            'data: {"object":"response","status":"completed",'
            '"output":[{"role":"assistant","content":['
            '{"type":"text","text":"Task result"}]}]}\n\n'
        )


class _MediaConsoleChannel:
    async def stream_one(self, _payload):
        response = {
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Media result"},
                        {
                            "type": "image",
                            "image_url": ("data:image/png;base64,aW1hZ2U="),
                        },
                        {
                            "type": "audio",
                            "data": "YXVkaW8=",
                            "format": "mpeg",
                        },
                        {
                            "type": "video",
                            "video_url": ("data:video/mp4;base64,dmlkZW8="),
                        },
                        {
                            "type": "file",
                            "file_data": "ZmlsZQ==",
                            "filename": "result.txt",
                            "media_type": "text/plain",
                        },
                    ],
                },
            ],
        }
        yield f"data: {json.dumps(response)}\n\n"


class _ChannelManager:
    def __init__(self, channel) -> None:
        self._channel = channel

    async def get_channel(self, name: str):
        return self._channel if name == "console" else None


@pytest.mark.asyncio
async def test_system_planner_and_runner_use_shared_catalog(
    tmp_path: Path,
) -> None:
    channel = _ConsoleChannel()
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        channel_manager=_ChannelManager(channel),
    )

    async def resolve_workspace(agent_id: str):
        assert agent_id == workspace.agent_id
        return workspace

    registry = GenerationRegistry()
    snapshot = await registry.activate_bundle(
        SYSTEM_CAPABILITY_BUNDLE,
        system_contribution_factory(resolve_workspace),
    )
    lease = await registry.pin()

    planner = lease.implementation(SYSTEM_BASIC_PLANNER_ID)
    runner = lease.implementation(SYSTEM_CONSOLE_RUNNER_ID)
    strategy = lease.implementation(SYSTEM_DEFAULT_STRATEGY_ID)
    renderer = lease.implementation(SYSTEM_SAFE_ARTIFACT_RENDERER_ID)
    assert snapshot.generation == 2
    assert isinstance(planner, TaskPlanner)
    assert isinstance(runner, TaskRunner)
    assert runner.cost_accounting is CostAccountingMode.UNKNOWN
    assert isinstance(strategy, RuntimeStrategy)
    assert isinstance(renderer, ArtifactRenderer)
    assert lease.resolve(SYSTEM_BASIC_PLANNER_ID).slot == "planner"
    assert lease.resolve(SYSTEM_CONSOLE_RUNNER_ID).slot == "runner"
    assert lease.resolve(SYSTEM_DEFAULT_STRATEGY_ID).slot == "strategy"
    assert (
        lease.resolve(SYSTEM_SAFE_ARTIFACT_RENDERER_ID).slot
        == "artifact.renderer"
    )
    assert (
        lease.resolve(SYSTEM_LOCAL_SCHEDULER_ID).slot
        == "scheduler.provider"
    )
    assert lease.resolve(SYSTEM_INBOX_DELIVERY_ID).slot == ("delivery.adapter")

    task_id = uuid4()
    run = Run(
        task_id=task_id,
        attempt=1,
        registry_generation=lease.generation,
        runner_id=SYSTEM_CONSOLE_RUNNER_ID,
    )
    checkpoint = ExecutionCheckpoint(
        task_id=task_id,
        run_id=run.run_id,
        sequence=3,
        safe_to_resume=True,
        runner_cursor={"next_step": 2},
    )
    order = TaskOrder(
        task_id=task_id,
        objective="Produce a result",
        metadata={
            "agent_id": "default",
            "project_dir": str(tmp_path),
            "task_ledger_workspace_dir": str(tmp_path),
            "approval_level": "strict",
            "resume_checkpoint": checkpoint.model_dump(mode="json"),
        },
    )
    steps = await planner.plan(order)
    strategy_parameters = await strategy.prepare(
        runtime_context_from_order(order, run),
        order,
    )
    signals = [signal async for signal in runner.execute(order, run)]

    assert steps[0].objective == order.objective
    assert strategy_parameters == {"mode": "default"}
    assert [signal.event_type for signal in signals] == [
        "conversation.user",
        "runner.dispatched",
        "runner.generating",
        "runner.responding",
        "conversation.assistant.delta",
        "conversation.assistant.completed",
        "artifact.produced",
    ]
    assert signals[1].payload["approval_policy"] == "strict"
    assert signals[-1].evidence_refs[0].producer == (SYSTEM_CONSOLE_RUNNER_ID)
    request_context = channel.payloads[0]["meta"]["request_context"]
    assert request_context["approval_level"] == "strict"
    assert request_context["durable_task"] is True
    assert request_context["os_invocation_id"] == str(run.run_id)
    assert request_context["os_correlation_id"] == str(run.run_id)
    assert request_context["resume_checkpoint"]["runner_cursor"] == {
        "next_step": 2,
    }
    await lease.close()


@pytest.mark.asyncio
async def test_console_runner_externalizes_public_media_before_ledger(
    tmp_path: Path,
) -> None:
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        channel_manager=_ChannelManager(_MediaConsoleChannel()),
    )

    async def resolve_workspace(_agent_id: str):
        return workspace

    registry = GenerationRegistry()
    await registry.activate_bundle(
        SYSTEM_CAPABILITY_BUNDLE,
        system_contribution_factory(resolve_workspace),
    )
    lease = await registry.pin()
    runner = lease.implementation(SYSTEM_CONSOLE_RUNNER_ID)
    task_id = uuid4()
    run = Run(
        task_id=task_id,
        attempt=1,
        registry_generation=lease.generation,
        runner_id=SYSTEM_CONSOLE_RUNNER_ID,
    )
    order = TaskOrder(
        task_id=task_id,
        objective="Produce media",
        metadata={
            "agent_id": "default",
            "project_dir": str(tmp_path),
            "task_ledger_workspace_dir": str(tmp_path),
        },
    )

    signals = [signal async for signal in runner.execute(order, run)]

    media_signals = [
        signal
        for signal in signals
        if signal.event_type == "artifact.produced"
        and signal.artifact_refs[0].metadata.get(
            "delivery_disposition",
        )
        == "embedded"
    ]
    completed = next(
        signal
        for signal in signals
        if signal.event_type == "conversation.assistant.completed"
    )
    assert len(media_signals) == 4
    assert len(completed.artifact_refs) == 4
    assert [part["type"] for part in completed.payload["content"]] == [
        "text",
        "image",
        "audio",
        "video",
        "file",
    ]
    serialized = completed.model_dump_json()
    assert "aW1hZ2U=" not in serialized
    assert "YXVkaW8=" not in serialized
    assert "dmlkZW8=" not in serialized
    assert "ZmlsZQ==" not in serialized
    expected = [b"image", b"audio", b"video", b"file"]
    store = lite_artifact_store(tmp_path)
    assert [
        await store.read(signal.artifact_refs[0]) for signal in media_signals
    ] == expected
    await lease.close()


@pytest.mark.asyncio
async def test_system_scheduler_is_shared_and_agent_scoped(
    tmp_path: Path,
) -> None:
    async def resolve_workspace(_agent_id: str):
        return SimpleNamespace(workspace_dir=tmp_path)

    registry = GenerationRegistry()
    await registry.activate_bundle(
        SYSTEM_CAPABILITY_BUNDLE,
        system_contribution_factory(resolve_workspace),
    )
    lease = await registry.pin()
    provider = lease.implementation(SYSTEM_LOCAL_SCHEDULER_ID)
    from qwenpaw.scheduling import SchedulerStoreHost

    assert isinstance(provider, SchedulerProvider)
    scheduler = await provider.open(
        SchedulerStoreHost(
            SQLiteSchedulerStore(tmp_path / "scheduler.db"),
        ),
    )

    assert isinstance(scheduler, SchedulerPort)
    for agent_id in ("agent-a", "agent-b"):
        definition = ScheduleDefinition(
            schedule_id="reports.daily",
            agent_id=agent_id,
            name="Daily report",
            objective="Prepare report",
            trigger=ScheduleTrigger(kind="cron", cron="0 9 * * *"),
            runner_id=SYSTEM_CONSOLE_RUNNER_ID,
        )
        await scheduler.upsert(definition)
        await scheduler.claim(
            ScheduleFire(
                agent_id=agent_id,
                schedule_id=definition.schedule_id,
                scheduled_for=datetime.now(timezone.utc),
                idempotency_key="same-logical-fire",
            ),
            owner_id=f"worker.{agent_id}",
            lease_seconds=30,
        )

    assert len(await scheduler.list_definitions(agent_id="agent-a")) == 1
    assert len(await scheduler.list_definitions(agent_id="agent-b")) == 1
    await lease.close()


class _TaskHarnessRuntime:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def task_events(self, **kwargs):
        self.calls.append(kwargs)
        yield HarnessEvent(
            kind=HarnessEventKind.REASONING_DELTA,
            text="Inspecting",
            item_id="reason-1",
        )
        yield HarnessEvent(
            kind=HarnessEventKind.TOOL_STARTED,
            item_id="tool-1",
            tool_name="shell",
            data={"arguments": {"command": "pytest -q"}},
        )
        yield HarnessEvent(
            kind=HarnessEventKind.TOOL_COMPLETED,
            item_id="tool-1",
            tool_name="shell",
            text="1 passed",
            data={"exit_code": 0},
        )
        yield HarnessEvent(
            kind=HarnessEventKind.TEXT_DELTA,
            text="Harness result",
        )
        yield HarnessEvent(kind=HarnessEventKind.COMPLETED)


@pytest.mark.asyncio
async def test_system_harness_runners_share_task_contract(
    tmp_path: Path,
) -> None:
    harness_runtime = _TaskHarnessRuntime()
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        harness_runtime=harness_runtime,
        config=SimpleNamespace(backend="qwenpaw", backend_settings={}),
    )

    async def resolve_workspace(_agent_id: str):
        return workspace

    registry = GenerationRegistry()
    await registry.activate_bundle(
        SYSTEM_CAPABILITY_BUNDLE,
        system_contribution_factory(resolve_workspace),
    )
    lease = await registry.pin()
    codex = lease.implementation(SYSTEM_CODEX_RUNNER_ID)
    qoder = lease.implementation(SYSTEM_QODER_RUNNER_ID)

    assert isinstance(codex, TaskRunner)
    assert isinstance(qoder, TaskRunner)
    assert codex.cost_accounting is CostAccountingMode.UNKNOWN
    assert qoder.cost_accounting is CostAccountingMode.UNKNOWN
    assert lease.resolve(SYSTEM_CODEX_RUNNER_ID).slot == "harness.runner"
    assert lease.resolve(SYSTEM_QODER_RUNNER_ID).slot == "harness.runner"

    task_id = uuid4()
    run = Run(
        task_id=task_id,
        attempt=1,
        registry_generation=lease.generation,
        runner_id=SYSTEM_CODEX_RUNNER_ID,
    )
    order = TaskOrder(
        task_id=task_id,
        objective="Run the external agent",
        metadata={
            "agent_id": "default",
            "project_dir": str(tmp_path),
            "task_ledger_workspace_dir": str(tmp_path),
        },
    )
    context = runtime_context_from_order(order, run)
    signals = [
        signal async for signal in codex.execute_context(order, run, context)
    ]

    assert [signal.event_type for signal in signals] == [
        "conversation.user",
        "runner.dispatched",
        "runner.generating",
        "runner.reasoning.delta",
        "tool.started",
        "tool.completed",
        "runner.responding",
        "conversation.assistant.delta",
        "conversation.assistant.completed",
        "artifact.produced",
    ]
    assert signals[1].payload == {
        "runtime": "codex",
        "slot": "harness.runner",
        "approval_policy": "agent_profile",
    }
    assert signals[4].payload["name"] == "shell"
    assert signals[-1].evidence_refs[0].producer == SYSTEM_CODEX_RUNNER_ID
    assert harness_runtime.calls[0]["session_id"] == context.session_id
    settings = harness_runtime.calls[0]["settings"]
    assert isinstance(settings, dict)
    assert settings["_request_context"]["durable_task"] is True
    await lease.close()


@pytest.mark.asyncio
async def test_selected_system_harness_uses_task_execution_pipeline(
    tmp_path: Path,
) -> None:
    harness_runtime = _TaskHarnessRuntime()
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        harness_runtime=harness_runtime,
        config=SimpleNamespace(backend="qwenpaw", backend_settings={}),
    )

    async def resolve_workspace(_agent_id: str):
        return workspace

    registry = GenerationRegistry()
    await registry.activate_bundle(
        SYSTEM_CAPABILITY_BUNDLE,
        system_contribution_factory(resolve_workspace),
    )
    service = TaskService(
        store=SQLiteExecutionLedger(tmp_path / "tasks.sqlite3"),
        registry_generation=registry.generation,
    )
    task = await service.create_task(
        objective="Run Codex through the selected Harness capability",
        agent_id="default",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Run Codex", objective=task.objective),),
    )
    order = TaskOrder(
        task_id=task.task_id,
        objective=task.objective,
        metadata={
            "agent_id": "default",
            "project_dir": str(tmp_path),
            "task_ledger_workspace_dir": str(tmp_path),
        },
    )

    completed = await TaskExecutionCoordinator(
        service,
        registry,
    ).execute_selected(order, SYSTEM_CODEX_RUNNER_ID)

    stored_task = await service.get_task(task.task_id)
    events = await service.list_events(task.task_id)
    event_types = [event.event_type for event in events]
    assert completed.status is RunStatus.SUCCEEDED
    assert stored_task is not None
    assert stored_task.status is TaskStatus.COMPLETED
    assert "conversation.user" in event_types
    assert "runner.reasoning.delta" in event_types
    assert "tool.started" in event_types
    assert "tool.completed" in event_types
    assert "artifact.produced" in event_types
    assert event_types[-1] == "run.completed"
    artifact_event = next(
        event for event in events if event.event_type == "artifact.produced"
    )
    assert artifact_event.artifact_refs
    assert artifact_event.evidence_refs[0].producer == SYSTEM_CODEX_RUNNER_ID


@pytest.mark.asyncio
async def test_system_strategies_publish_bounded_mode_directives(
    tmp_path: Path,
) -> None:
    async def resolve_workspace(_agent_id: str):
        return SimpleNamespace(
            agent_id="default",
            workspace_dir=tmp_path,
        )

    registry = GenerationRegistry()
    await registry.activate_bundle(
        SYSTEM_CAPABILITY_BUNDLE,
        system_contribution_factory(resolve_workspace),
    )
    lease = await registry.pin()
    task_id = uuid4()
    run = Run(
        task_id=task_id,
        attempt=1,
        registry_generation=lease.generation,
        runner_id=SYSTEM_CONSOLE_RUNNER_ID,
    )
    order = TaskOrder(
        task_id=task_id,
        objective="Do the work",
        metadata={
            "agent_id": "default",
            "project_dir": str(tmp_path),
            "task_ledger_workspace_dir": str(tmp_path),
        },
    )
    context = runtime_context_from_order(order, run)
    expected = {
        SYSTEM_DEFAULT_STRATEGY_ID: {"mode": "default"},
        SYSTEM_CODING_STRATEGY_ID: {
            "mode": "coding",
            "activation": "request",
        },
        SYSTEM_GOAL_STRATEGY_ID: {
            "mode": "goal",
            "activation": "command",
            "command": "goal",
        },
        SYSTEM_MISSION_STRATEGY_ID: {
            "mode": "mission",
            "activation": "command",
            "command": "mission",
        },
    }

    assert {
        descriptor.capability_id
        for descriptor in lease.descriptors("strategy")
    } == set(expected)
    for strategy_id, directive in expected.items():
        strategy = lease.implementation(strategy_id)
        assert isinstance(strategy, RuntimeStrategy)
        assert await strategy.prepare(context, order) == directive
    await lease.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("strategy_id", "command"),
    [
        (SYSTEM_GOAL_STRATEGY_ID, "/goal"),
        (SYSTEM_MISSION_STRATEGY_ID, "/mission"),
    ],
)
async def test_console_runner_activates_command_strategies(
    tmp_path: Path,
    strategy_id: str,
    command: str,
) -> None:
    channel = _ConsoleChannel()
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        channel_manager=_ChannelManager(channel),
    )

    async def resolve_workspace(_agent_id: str):
        return workspace

    declaration = next(
        item
        for item in SYSTEM_CAPABILITY_BUNDLE.contributions
        if item.contribution_id == "console-agent"
    )
    runner = system_contribution_factory(resolve_workspace)(declaration)
    task_id = uuid4()
    run = Run(
        task_id=task_id,
        attempt=1,
        registry_generation=2,
        runner_id=SYSTEM_CONSOLE_RUNNER_ID,
    )
    order = TaskOrder(
        task_id=task_id,
        objective="Inspect the repository",
        metadata={
            "agent_id": "default",
            "project_dir": str(tmp_path),
            "task_ledger_workspace_dir": str(tmp_path),
        },
    )
    context = runtime_context_from_order(
        order,
        run,
        strategy_id=strategy_id,
    ).model_copy(
        update={
            "strategy": RuntimeStrategyDirective(
                strategy_id=strategy_id,
                parameters={
                    "mode": command.removeprefix("/"),
                    "activation": "command",
                    "command": command.removeprefix("/"),
                },
            ),
        },
    )

    _ = [
        signal async for signal in runner.execute_context(order, run, context)
    ]

    assert channel.payloads[0]["content_parts"][0].text == (
        f"{command} {order.objective}"
    )
    assert channel.payloads[0]["meta"]["request_context"][
        "runtime_strategy"
    ] == {
        "id": strategy_id,
        "parameters": context.strategy_parameters,
    }


@pytest.mark.asyncio
async def test_console_runner_does_not_bridge_plugin_strategy_parameters(
    tmp_path: Path,
) -> None:
    channel = _ConsoleChannel()
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        channel_manager=_ChannelManager(channel),
    )

    async def resolve_workspace(_agent_id: str):
        return workspace

    declaration = next(
        item
        for item in SYSTEM_CAPABILITY_BUNDLE.contributions
        if item.contribution_id == "console-agent"
    )
    runner = system_contribution_factory(resolve_workspace)(declaration)
    task_id = uuid4()
    run = Run(
        task_id=task_id,
        attempt=1,
        registry_generation=2,
        runner_id=SYSTEM_CONSOLE_RUNNER_ID,
    )
    order = TaskOrder(
        task_id=task_id,
        objective="Keep the objective unchanged",
        metadata={
            "agent_id": "default",
            "project_dir": str(tmp_path),
            "task_ledger_workspace_dir": str(tmp_path),
        },
    )
    context = runtime_context_from_order(
        order,
        run,
        strategy_id="plugin.coding-strategy",
    ).model_copy(
        update={
            "strategy": RuntimeStrategyDirective(
                strategy_id="plugin.coding-strategy",
                parameters={
                    "mode": "coding",
                    "activation": "request",
                },
            ),
        },
    )

    _ = [
        signal async for signal in runner.execute_context(order, run, context)
    ]

    assert channel.payloads[0]["content_parts"][0].text == order.objective
    assert (
        "runtime_strategy"
        not in channel.payloads[0]["meta"]["request_context"]
    )


@pytest.mark.asyncio
async def test_console_runner_clears_legacy_approvals_on_cancel(
    monkeypatch,
    tmp_path: Path,
) -> None:
    entered_stream = asyncio.Event()

    class BlockingConsoleChannel:
        async def stream_one(self, _payload):
            entered_stream.set()
            await asyncio.Event().wait()
            yield ""  # pragma: no cover

    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        channel_manager=_ChannelManager(BlockingConsoleChannel()),
    )

    async def resolve_workspace(_agent_id: str):
        return workspace

    approval_service = SimpleNamespace(
        cancel_all_pending_by_root_session=AsyncMock(return_value=1),
    )
    monkeypatch.setattr(
        "qwenpaw.app.approvals.get_approval_service",
        lambda: approval_service,
    )
    console_declaration = next(
        declaration
        for declaration in SYSTEM_CAPABILITY_BUNDLE.contributions
        if declaration.contribution_id == "console-agent"
    )
    runner = system_contribution_factory(resolve_workspace)(
        console_declaration,
    )
    task_id = uuid4()
    run = Run(
        task_id=task_id,
        attempt=1,
        registry_generation=1,
        runner_id=SYSTEM_CONSOLE_RUNNER_ID,
    )
    order = TaskOrder(
        task_id=task_id,
        objective="Wait for approval",
        metadata={
            "agent_id": "default",
            "project_dir": str(tmp_path),
        },
    )
    cancellation = RuntimeCancellationToken()
    context = runtime_context_from_order(
        order,
        run,
        cancellation=cancellation,
    )

    async def consume() -> None:
        async for _ in runner.execute_context(order, run, context):
            pass

    execution = asyncio.create_task(consume())
    await entered_stream.wait()
    cancellation.cancel("user cancelled the task")
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution

    cancel_pending = approval_service.cancel_all_pending_by_root_session
    cancel_pending.assert_awaited_once_with(context.session_id)
