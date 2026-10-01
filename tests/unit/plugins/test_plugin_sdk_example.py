# -*- coding: utf-8 -*-
"""Keep the example plugin on the stable public SDK surface."""

import ast
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from qwenpaw.kernel.models import (
    ApprovalLevel,
    PlanStep,
    RuntimeLaunchConfig,
    TaskOrder,
)
from qwenpaw.plugins.architecture import PluginManifest
from qwenpaw.plugins.generations import GenerationRegistry
from qwenpaw.runtime.assembly import RuntimeAssemblyFactory
from qwenpaw.tasks.context import order_metadata_from_launch_config
from qwenpaw.tasks.ledger import SQLiteExecutionLedger
from qwenpaw.tasks.runner import TaskExecutionCoordinator
from qwenpaw.tasks.service import TaskService


async def _assert_project_command(
    commands: Any,
    command_module: Any,
    scope: Any,
) -> None:
    command_session = await commands.open(scope, SimpleNamespace())
    command_result = await command_session.dispatch(
        command_module.CommandRequest(
            raw_text="/project-name",
            command_name="project-name",
        ),
    )
    assert command_result.message is not None
    assert command_result.message.text == "Project: sample-project"
    await command_session.close()


def _assert_harness_result_events(
    events: list[Any],
    generation: int,
) -> None:
    assert events[-3].event_type == (
        "plugin.runtime-provider-kit.harness.completed"
    )
    assert events[-3].payload["registry_generation"] == generation
    assert events[-3].source == "runtime-provider-kit.echo-harness"
    assert events[-2].event_type == "artifact.produced"
    assert events[-2].artifact_refs[0].metadata["name"] == "task-result.md"
    assert events[-2].evidence_refs[0].producer == (
        "runtime-provider-kit.echo-harness"
    )


def test_example_plugin_imports_only_public_sdk() -> None:
    root = Path(__file__).parents[3]
    source_root = (
        root / "examples" / "plugins" / "task-insights" / "task_insights"
    )
    for source_path in (
        source_root / "planner.py",
        source_root / "strategy.py",
        source_root / "runner.py",
    ):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        qwenpaw_imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("qwenpaw")
        }
        assert qwenpaw_imports == {"qwenpaw.plugins.sdk"}


def test_example_sensor_imports_only_public_sdk() -> None:
    root = Path(__file__).parents[3]
    source = (
        root
        / "examples"
        / "plugins"
        / "task-insights"
        / "task_insights"
        / "sensor.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert imports == {"qwenpaw.plugins.sdk"}


def test_chat_tool_provider_imports_only_public_sdk() -> None:
    root = Path(__file__).parents[3]
    source = (
        root
        / "examples"
        / "plugins"
        / "chat-tool-provider"
        / "chat_tool_provider"
        / "provider.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert imports == {"collections.abc", "qwenpaw.plugins.sdk"}


def test_runtime_provider_kit_imports_only_public_sdk() -> None:
    root = (
        Path(__file__).parents[3]
        / "examples"
        / "plugins"
        / "runtime-provider-kit"
        / "runtime_provider_kit"
    )
    for source_path in (
        root / "memory.py",
        root / "harness.py",
        root / "driver.py",
        root / "prompt.py",
        root / "command.py",
        root / "hook.py",
        root / "stop_gate.py",
        root / "mode.py",
    ):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        qwenpaw_imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("qwenpaw")
        }
        assert qwenpaw_imports == {"qwenpaw.plugins.sdk"}


def test_scheduler_provider_imports_only_public_sdk() -> None:
    root = (
        Path(__file__).parents[3]
        / "examples"
        / "plugins"
        / "scheduler-provider"
        / "scheduler_provider"
        / "provider.py"
    )
    tree = ast.parse(root.read_text(encoding="utf-8"))
    qwenpaw_imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith("qwenpaw")
    }

    assert qwenpaw_imports == {"qwenpaw.plugins.sdk"}


def test_delivery_provider_imports_only_public_sdk() -> None:
    root = (
        Path(__file__).parents[3]
        / "examples"
        / "plugins"
        / "delivery-provider"
        / "delivery_provider"
        / "provider.py"
    )
    tree = ast.parse(root.read_text(encoding="utf-8"))
    qwenpaw_imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith("qwenpaw")
    }

    assert qwenpaw_imports == {"qwenpaw.plugins.sdk"}


def test_public_sdk_exports_runtime_interaction_contract() -> None:
    from qwenpaw.plugins import sdk

    assert not hasattr(sdk, "AgentFactory")
    assert not hasattr(sdk, "ActionStore")
    assert sdk.ActionKind is not None
    assert sdk.ActionRecord is not None
    assert sdk.ActionRequest is not None
    assert sdk.ActionResult is not None
    assert sdk.ActionStatus is not None
    assert sdk.CapabilityCredentialHandle is not None
    assert sdk.CapabilityCredentialUnavailableError is not None
    assert sdk.CostAccountingMode is not None
    assert sdk.CostAwareTaskRunner is not None
    assert sdk.InteractionOption is not None
    assert sdk.InteractionResolution is not None
    assert sdk.MemoryStateConflictError is not None
    assert sdk.MemoryStateScope is not None
    assert sdk.MemoryStateStore is not None
    assert sdk.ModelSelection is not None
    assert sdk.PlanStep is not None
    assert sdk.RuntimeInteractionProducer is not None
    assert sdk.ContextualProposalSensor is not None
    assert sdk.SensorContext is not None
    assert sdk.TaskPlanner is not None
    assert sdk.DriverApprovalRejectedError is not None
    assert sdk.DriverApprovalRequest is not None
    assert sdk.DriverCredentialHandle is not None
    assert sdk.DriverCredentialUnavailableError is not None
    assert sdk.DriverToolDefinition is not None
    assert sdk.ScheduleDefinition is not None
    assert sdk.ScheduleDefinitionNotFoundError is not None
    assert sdk.ScheduleFire is not None
    assert sdk.ScheduleFireConflictError is not None
    assert sdk.ScheduleLease is not None
    assert sdk.ScheduleLeaseConflictError is not None
    assert sdk.ScheduleLeaseNotFoundError is not None
    assert sdk.ScheduleLeaseStatus is not None
    assert sdk.ScheduleTrigger is not None
    assert sdk.SchedulerPort is not None
    assert sdk.SQLiteSchedulerStore is not None
    assert sdk.ConversationForkBoundary is not None
    assert sdk.ConversationForkCommand is not None
    assert sdk.ConversationForkOrigin is not None
    assert sdk.ConversationForkPort is not None
    assert sdk.ConversationForkResult is not None
    assert sdk.DeliveryAdapter is not None
    assert sdk.DeliveryReceipt is not None
    assert sdk.DeliveryRequest is not None
    assert sdk.InboxItem is not None
    assert sdk.InboxProjectionPort is not None


@pytest.mark.asyncio
async def test_scheduler_provider_activates_and_persists_definition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qwenpaw.plugins import sdk

    root = (
        Path(__file__).parents[3]
        / "examples"
        / "plugins"
        / "scheduler-provider"
    )
    database_path = tmp_path / "scheduler.db"
    monkeypatch.setenv(
        "QWENPAW_EXAMPLE_SCHEDULER_DB",
        str(database_path),
    )
    monkeypatch.syspath_prepend(str(root))
    provider_module = importlib.import_module("scheduler_provider.provider")
    manifest = PluginManifest.from_dict(
        json.loads((root / "plugin.json").read_text(encoding="utf-8")),
    )
    registry = GenerationRegistry()
    snapshot = await registry.activate(
        manifest,
        lambda _: provider_module.create_scheduler(),
    )
    lease = await registry.pin(snapshot.generation)
    scheduler = lease.implementation("scheduler-provider.local-durable")
    definition = sdk.ScheduleDefinition(
        agent_id="default",
        schedule_id="daily-review",
        name="Daily review",
        trigger=sdk.ScheduleTrigger(
            kind="cron",
            cron="0 9 * * *",
            timezone="UTC",
        ),
        objective="Review yesterday's work",
        runner_id="qwenpaw.system.console-agent",
    )

    assert isinstance(scheduler, sdk.SchedulerPort)
    await scheduler.upsert(definition)
    assert await scheduler.list_definitions(agent_id="default") == (
        definition,
    )
    assert database_path.exists()
    await lease.close()


@pytest.mark.asyncio
async def test_chat_tool_provider_uses_pinned_public_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = (
        Path(__file__).parents[3]
        / "examples"
        / "plugins"
        / "chat-tool-provider"
    )
    monkeypatch.syspath_prepend(str(root))
    provider_module = importlib.import_module("chat_tool_provider.provider")
    manifest = PluginManifest.from_dict(
        json.loads((root / "plugin.json").read_text(encoding="utf-8")),
    )
    registry = GenerationRegistry()
    await registry.activate(
        manifest,
        lambda _: provider_module.create_provider(),
    )

    assembly = await RuntimeAssemblyFactory(registry).open(
        agent_id="default",
        session_id="example",
        root_agent_id="default",
        root_session_id="example",
        workspace_dir=str(root),
    )
    provider = assembly.require(
        "chat-tool-provider.workspace-info",
        "tool.provider",
    )
    broker = SimpleNamespace(suggest=AsyncMock(return_value=None))
    host = SimpleNamespace(
        interaction_broker=lambda: broker,
        config_snapshot=lambda: {},
        credential=lambda _alias: None,
    )
    definitions = await provider.list_tools(
        assembly.scope,
        provider_module.ToolSelection(),
        host,
    )

    assert "chat-tool-provider.workspace-info" in (
        assembly.scope.selection.tool_provider_ids
    )
    assert definitions[0].name == "describe_qwenpaw_invocation"
    assert definitions[0].tool_type == "internal"
    result = await definitions[1].function()
    assert result == "Plugin suggestion delivered without blocking the run."
    broker.suggest.assert_awaited_once()
    request = broker.suggest.await_args.kwargs
    assert request["metadata"] == {
        "source": "chat-tool-provider.workspace-info",
    }
    await assembly.close()


def test_example_plugin_uses_real_contextual_ui_slot_api() -> None:
    root = Path(__file__).parents[3] / "examples" / "plugins" / "task-insights"
    manifest = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    frontend = (root / "frontend" / "inspector.js").read_text(
        encoding="utf-8",
    )
    slots = {
        item["slot"]
        for item in manifest["contributions"]
        if item["slot"].startswith("ui.")
    }

    assert slots == {
        "ui.task.toolbar",
        "ui.task.tab",
        "ui.task.inspector",
        "ui.artifact.preview",
    }
    assert "host.slot.fill" in frontend
    assert "context?.task" in frontend
    assert "context?.artifact" in frontend
    assert "context.slots.register" not in frontend


@pytest.mark.asyncio
async def test_example_runner_receives_pinned_runtime_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).parents[3] / "examples" / "plugins" / "task-insights"
    monkeypatch.syspath_prepend(str(root))
    runner_module = importlib.import_module("task_insights.runner")
    manifest_data = json.loads(
        (root / "plugin.json").read_text(encoding="utf-8"),
    )
    manifest_data["contributions"] = [
        contribution
        for contribution in manifest_data["contributions"]
        if contribution["slot"] == "runner"
    ]
    registry = GenerationRegistry()
    await registry.activate(
        PluginManifest.from_dict(manifest_data),
        lambda _: runner_module.create_runner(),
    )
    service = TaskService(
        store=SQLiteExecutionLedger(tmp_path / "ledger.db"),
        registry_generation=1,
    )
    task = await service.create_task(
        objective="Inspect one task",
        agent_id="plugin-author",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Inspect", objective=task.objective),),
    )
    order = TaskOrder(
        task_id=task.task_id,
        objective=task.objective,
        metadata=order_metadata_from_launch_config(
            RuntimeLaunchConfig(
                agent_id=task.agent_id,
                project_dir=str(tmp_path),
                ledger_workspace_dir=str(tmp_path),
                approval_level=ApprovalLevel.STRICT,
            ),
        ),
    )

    completed = await TaskExecutionCoordinator(
        service,
        registry,
    ).execute_selected(order, "task-insights.summary-runner")

    events = await service.list_events(task.task_id)
    assert completed.registry_generation == 2
    assert events[-3].event_type == "plugin.task-insights.summary"
    assert events[-3].payload["registry_generation"] == 2
    assert events[-3].payload["approval_level"] == "strict"
    assert events[-2].event_type == "artifact.produced"
    assert events[-2].artifact_refs[0].metadata["name"] == ("task-insight.md")
    assert events[-2].evidence_refs[0].producer == (
        "task-insights.summary-runner"
    )


@pytest.mark.asyncio
async def test_runtime_provider_kit_activates_runtime_contributions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = (
        Path(__file__).parents[3]
        / "examples"
        / "plugins"
        / "runtime-provider-kit"
    )
    monkeypatch.syspath_prepend(str(root))
    memory_module = importlib.import_module("runtime_provider_kit.memory")
    harness_module = importlib.import_module("runtime_provider_kit.harness")
    driver_module = importlib.import_module("runtime_provider_kit.driver")
    prompt_module = importlib.import_module("runtime_provider_kit.prompt")
    command_module = importlib.import_module("runtime_provider_kit.command")
    hook_module = importlib.import_module("runtime_provider_kit.hook")
    mode_module = importlib.import_module("runtime_provider_kit.mode")
    manifest = PluginManifest.from_dict(
        json.loads((root / "plugin.json").read_text(encoding="utf-8")),
    )
    factories = {
        "project-memory": memory_module.create_provider,
        "echo-harness": harness_module.create_runner,
        "example-driver": driver_module.create_provider,
        "project-prompt": prompt_module.create_provider,
        "project-commands": command_module.create_provider,
        "project-hooks": hook_module.create_provider,
        "review-gates": importlib.import_module(
            "runtime_provider_kit.stop_gate",
        ).create_provider,
        "review-mode": mode_module.create_provider,
    }
    registry = GenerationRegistry()
    snapshot = await registry.activate(
        manifest,
        lambda declaration: factories[declaration.contribution_id](),
    )
    lease = await registry.pin(snapshot.generation)
    memory = lease.implementation("runtime-provider-kit.project-memory")
    prompt = lease.implementation("runtime-provider-kit.project-prompt")
    commands = lease.implementation("runtime-provider-kit.project-commands")
    memory_state = SimpleNamespace(
        read=AsyncMock(return_value=None),
        write=AsyncMock(),
    )
    memory_session = await memory.open(
        memory_module.InvocationScope(
            agent_id="default",
            session_id="chat",
            root_agent_id="default",
            root_session_id="chat",
            workspace_dir=str(tmp_path / "sample-project"),
            registry_generation=snapshot.generation,
        ),
        SimpleNamespace(
            config_snapshot=lambda: {},
            state=lambda _scope: memory_state,
        ),
    )

    assert memory_session.get_prompt() == (
        "Keep terminology consistent for sample-project."
    )
    prompt_fragments = await prompt.list_fragments(
        memory_module.InvocationScope(
            agent_id="default",
            conversation_id="chat",
            session_id="transport-chat",
            root_agent_id="default",
            root_session_id="transport-chat",
            workspace_dir=str(tmp_path / "sample-project"),
            registry_generation=snapshot.generation,
        ),
        SimpleNamespace(build_workspace_prompt=AsyncMock()),
    )
    assert prompt_fragments[0].fragment_id == (
        "runtime-provider-kit.project-prompt.project-guidance"
    )
    assert "sample-project" in prompt_fragments[0].content
    await _assert_project_command(
        commands,
        command_module,
        memory_module.InvocationScope(
            agent_id="default",
            conversation_id="chat",
            session_id="transport-chat",
            root_agent_id="default",
            root_session_id="transport-chat",
            workspace_dir=str(tmp_path / "sample-project"),
            registry_generation=snapshot.generation,
        ),
    )
    assert memory_session.list_tools()[0].name == "remember_project_label"
    await memory_session.close()

    driver = lease.implementation("runtime-provider-kit.example-driver")
    require_approval = AsyncMock(return_value=None)
    driver_session = await driver.open(
        memory_module.InvocationScope(
            agent_id="default",
            session_id="chat",
            root_agent_id="default",
            root_session_id="chat",
            workspace_dir=str(tmp_path),
            registry_generation=snapshot.generation,
        ),
        SimpleNamespace(
            load=AsyncMock(return_value=((), ())),
            require_approval=require_approval,
            config_snapshot=lambda: {"echo_prefix": "configured:"},
            credential=lambda _alias: None,
        ),
    )
    definitions = driver_session.list_tools()
    assert definitions[0].provider_id == (
        "runtime-provider-kit.example-driver"
    )
    assert await definitions[0].invoke({"text": "hello"}) == {
        "echo": "configured:hello",
        "credential_configured": False,
    }
    require_approval.assert_awaited_once()
    approval = require_approval.await_args.args[0]
    assert approval.provider_id == "runtime-provider-kit.example-driver"
    assert approval.redacted_arguments == {"text": "hello"}
    assert driver_session.prompt_fragments()[0].fragment_id == (
        "runtime-provider-kit.example-driver.policy"
    )
    await driver_session.close()
    await lease.close()

    service = TaskService(
        store=SQLiteExecutionLedger(tmp_path / "ledger.db"),
        registry_generation=1,
    )
    task = await service.create_task(
        objective="Run through a contributed harness",
        agent_id="plugin-author",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Run", objective=task.objective),),
    )
    order = TaskOrder(
        task_id=task.task_id,
        objective=task.objective,
        metadata=order_metadata_from_launch_config(
            RuntimeLaunchConfig(
                agent_id=task.agent_id,
                project_dir=str(tmp_path),
                ledger_workspace_dir=str(tmp_path),
            ),
        ),
    )

    completed = await TaskExecutionCoordinator(
        service,
        registry,
    ).execute_selected(order, "runtime-provider-kit.echo-harness")

    events = await service.list_events(task.task_id)
    assert completed.runner_id == "runtime-provider-kit.echo-harness"
    assert completed.registry_generation == snapshot.generation
    _assert_harness_result_events(events, snapshot.generation)
