# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""End-to-end Lite Cron execution through Task and Delivery facts."""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from qwenpaw.app.crons.task_runtime import LiteCronTaskRuntime
from qwenpaw.app.crons.scheduled_task_runtime import LiteScheduledTaskRuntime
from qwenpaw.app.crons.models import (
    CronJobRequest,
    CronRuntimeDecisionCode,
    CronRuntimePath,
    ScheduleSpec,
)
from qwenpaw.app.task_runtime import task_application_host
from qwenpaw.inbox import SQLiteInboxProjectionStore
from qwenpaw.kernel import ScheduleDefinition, ScheduleTrigger
from qwenpaw.plugins.generations import GenerationRegistry
from qwenpaw.scheduling import (
    SQLiteSchedulerStore,
    ScheduleDispatchAccountingError,
    ScheduleOccurrenceHandling,
    ScheduleTriggerTickReport,
)
from qwenpaw.tasks.bootstrap import task_service_for_workspace
from tests.unit.app.conftest import make_cron_job_spec


class _ConsoleChannel:
    def __init__(self) -> None:
        self.payloads = []

    async def stream_one(self, payload):
        self.payloads.append(payload)
        yield (
            'data: {"object":"response","status":"completed",'
            '"output":[{"role":"assistant","content":['
            '{"type":"text","text":"Scheduled result"}]}]}\n\n'
        )


class _ChannelManager:
    def __init__(self) -> None:
        self.deliveries = []
        self.console = _ConsoleChannel()

    async def get_channel(self, name: str):
        return self.console if name == "console" else None

    async def send_event(self, **kwargs) -> None:
        self.deliveries.append(kwargs)


class _ChatManager:
    async def get_or_create_chat(self, **kwargs):
        return SimpleNamespace(id="chat-daily", **kwargs)

    async def touch_chat(self, _chat_id: str) -> None:
        return None


class _ScheduledTriggerStub:
    def __init__(self, definition: ScheduleDefinition) -> None:
        self.definition = definition
        self.handling = None

    async def run_due(self, *, now, handle, batch_size=100):
        del batch_size
        self.handling = await handle(self.definition, now)
        return ScheduleTriggerTickReport(outcomes=())


def _trigger_definition() -> ScheduleDefinition:
    return ScheduleDefinition(
        schedule_id="cron.test",
        agent_id="default",
        name="Test Cron",
        objective="Run test",
        trigger=ScheduleTrigger(
            kind="once",
            run_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        ),
        runner_id="qwenpaw.system.tasks.console-agent",
        metadata={"legacy_cron_job_id": "daily"},
    )


def test_scheduled_runtimes_share_the_registry_host(tmp_path: Path) -> None:
    registry = GenerationRegistry()
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        capability_registry=registry,
    )

    first = LiteScheduledTaskRuntime(workspace)
    second = LiteScheduledTaskRuntime(workspace)

    assert first._host is second._host  # pylint: disable=protected-access
    assert first._host is task_application_host(registry)  # noqa: SLF001


@pytest.mark.asyncio
async def test_due_cron_failure_is_handled_when_policy_forbids_retry(
    tmp_path: Path,
) -> None:
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        capability_registry=GenerationRegistry(),
    )
    runtime = LiteCronTaskRuntime(workspace)
    scheduled = _ScheduledTriggerStub(_trigger_definition())
    runtime._scheduled = scheduled

    async def fail(_job_id, _scheduled_for):
        raise RuntimeError("terminal task failure")

    await runtime.run_due(
        now=datetime(2030, 1, 1, tzinfo=timezone.utc),
        execute=fail,
    )

    assert scheduled.handling is ScheduleOccurrenceHandling.HANDLED


@pytest.mark.asyncio
async def test_due_cron_accounting_failure_remains_retryable(
    tmp_path: Path,
) -> None:
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        capability_registry=GenerationRegistry(),
    )
    runtime = LiteCronTaskRuntime(workspace)
    scheduled = _ScheduledTriggerStub(_trigger_definition())
    runtime._scheduled = scheduled

    async def fail(_job_id, _scheduled_for):
        raise ScheduleDispatchAccountingError("not accounted")

    await runtime.run_due(
        now=datetime(2030, 1, 1, tzinfo=timezone.utc),
        execute=fail,
    )

    assert scheduled.handling is ScheduleOccurrenceHandling.RETRY


def test_lite_cron_runtime_explains_each_compatibility_path(
    tmp_path: Path,
) -> None:
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        capability_registry=GenerationRegistry(),
    )
    runtime = LiteCronTaskRuntime(workspace)

    migrated = make_cron_job_spec(job_id="migrated")
    migrated.dispatch.mode = "final"
    text = make_cron_job_spec(job_id="text", task_type="text")
    stream = make_cron_job_spec(job_id="stream")
    repeating = make_cron_job_spec(job_id="repeating")
    repeating.dispatch.mode = "final"
    repeating.schedule = ScheduleSpec(
        type="once",
        run_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        repeat_every_days=1,
    )
    interactive = make_cron_job_spec(job_id="interactive")
    interactive.dispatch.mode = "final"
    interactive.runtime.tool_safety = True
    missing_request = make_cron_job_spec(job_id="missing").model_copy(
        update={"request": None},
    )
    invalid_model = make_cron_job_spec(job_id="invalid-model")
    invalid_model.dispatch.mode = "final"
    invalid_model.request = CronJobRequest(
        input="ping",
        model_slot_override="missing-separator",
    )

    expected = {
        CronRuntimeDecisionCode.MIGRATED: migrated,
        CronRuntimeDecisionCode.TEXT_DELIVERY_ONLY: text,
        CronRuntimeDecisionCode.STREAM_DELIVERY_UNVERIFIED: stream,
        CronRuntimeDecisionCode.AGENT_REQUEST_MISSING: missing_request,
        CronRuntimeDecisionCode.MODEL_SELECTION_INVALID: invalid_model,
    }
    for code, job in expected.items():
        decision = runtime.decision(job)
        assert decision.reason_code is code
        assert decision.path is (
            CronRuntimePath.DURABLE_TASK
            if code is CronRuntimeDecisionCode.MIGRATED
            else CronRuntimePath.LEGACY_EXECUTOR
        )
        assert runtime.supports(job) is decision.uses_durable_runtime
        if not decision.uses_durable_runtime:
            assert decision.removal_gates
    repeating_decision = runtime.decision(repeating)
    assert repeating_decision.reason_code is CronRuntimeDecisionCode.MIGRATED
    assert repeating_decision.uses_durable_runtime
    interactive_decision = runtime.decision(interactive)
    assert interactive_decision.reason_code is CronRuntimeDecisionCode.MIGRATED
    assert interactive_decision.uses_durable_runtime


@pytest.mark.asyncio
async def test_lite_cron_runtime_synchronizes_catalog_before_first_fire(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("qwenpaw.constant.WORKING_DIR", tmp_path)
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        capability_registry=GenerationRegistry(),
        chat_manager=_ChatManager(),
    )
    runtime = LiteCronTaskRuntime(workspace)
    job = make_cron_job_spec(job_id="catalog")
    job.dispatch.mode = "final"

    await runtime.synchronize(job)

    store = SQLiteSchedulerStore(tmp_path / "scheduler.db")
    [definition] = await store.list_definitions(agent_id="default")
    cursor = await store.get_cursor(
        agent_id="default",
        schedule_id=definition.schedule_id,
    )
    assert definition.enabled is True
    assert definition.metadata["legacy_cron_job_id"] == "catalog"
    assert cursor is not None
    assert cursor.next_fire_at is not None

    disabled = job.model_copy(update={"enabled": False})
    await runtime.synchronize(disabled)
    [updated] = await store.list_definitions(agent_id="default")
    disabled_cursor = await store.get_cursor(
        agent_id="default",
        schedule_id=updated.schedule_id,
    )
    assert updated.enabled is False
    assert disabled_cursor is not None
    assert disabled_cursor.next_fire_at is None

    legacy_stream = job.model_copy(deep=True)
    legacy_stream.dispatch.mode = "stream"
    await runtime.synchronize(legacy_stream)
    assert await store.list_definitions(agent_id="default") == ()
    assert (
        await store.get_cursor(
            agent_id="default",
            schedule_id=definition.schedule_id,
        )
        is None
    )


@pytest.mark.asyncio
async def test_durable_trigger_runs_real_task_and_advances_once_cursor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("qwenpaw.constant.WORKING_DIR", tmp_path)
    channels = _ChannelManager()
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        capability_registry=GenerationRegistry(),
        channel_manager=channels,
        chat_manager=_ChatManager(),
    )
    runtime = LiteCronTaskRuntime(workspace)
    scheduled_for = datetime(2030, 1, 1, tzinfo=timezone.utc)
    job = make_cron_job_spec(job_id="worker-once")
    job.dispatch.mode = "final"
    job.save_result_to_inbox = False
    job.schedule = ScheduleSpec(type="once", run_at=scheduled_for)
    await runtime.synchronize(job)
    runtime = LiteCronTaskRuntime(workspace)
    executions = []

    async def execute(definition, occurrence: datetime) -> None:
        assert definition.metadata["legacy_cron_job_id"] == "worker-once"
        executions.append(
            await runtime.execute(
                job,
                trigger="scheduled",
                scheduled_for=occurrence,
            ),
        )

    report = await runtime.run_due(
        now=scheduled_for,
        execute=execute,
    )

    assert report.outcomes[0].disposition.value == "dispatched"
    assert report.outcomes[0].next_fire_at is None
    assert len(executions) == 1
    assert executions[0]["task_id"]
    assert executions[0]["delivery_status"] == "success"
    assert len(channels.deliveries) == 1

    replay = await runtime.run_due(
        now=scheduled_for,
        execute=execute,
    )
    assert replay.outcomes == ()
    assert len(executions) == 1


@pytest.mark.asyncio
async def test_lite_cron_runtime_waits_for_task_and_delivery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("qwenpaw.constant.WORKING_DIR", tmp_path)
    channels = _ChannelManager()
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        capability_registry=GenerationRegistry(),
        channel_manager=channels,
        chat_manager=_ChatManager(),
    )
    job = make_cron_job_spec(job_id="daily")
    job.dispatch.mode = "final"
    job.runtime.tool_safety = True
    job.runtime.approval_timeout_seconds = 15
    job.schedule = ScheduleSpec(
        type="once",
        run_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        repeat_every_days=2,
        repeat_end_type="count",
        repeat_count=3,
    )
    job.request.model_slot_override = {
        "provider_id": "dashscope",
        "model": "qwen-max",
    }
    job.request.model_fields_set.add("model_slot_override")

    result = await LiteCronTaskRuntime(workspace).execute(
        job,
        trigger="scheduled",
        scheduled_for=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    replay = await LiteCronTaskRuntime(workspace).execute(
        job,
        trigger="scheduled",
        scheduled_for=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )

    assert result["task_id"]
    assert result["run_id"]
    assert result["conversation_id"] == "chat-daily"
    assert result["delivery_status"] == "success"
    assert replay["task_id"] == result["task_id"]
    assert replay["run_id"] == result["run_id"]
    assert replay["delivery_status"] == "success"
    definitions = await SQLiteSchedulerStore(
        tmp_path / "scheduler.db",
    ).list_definitions(agent_id="default")
    assert len(definitions) == 1
    assert definitions[0].trigger.kind == "interval"
    assert definitions[0].trigger.start_at == datetime(
        2030,
        1,
        1,
        tzinfo=timezone.utc,
    )
    assert definitions[0].trigger.end_at == datetime(
        2030,
        1,
        5,
        tzinfo=timezone.utc,
    )
    assert len(channels.deliveries) == 1
    assert (
        channels.console.payloads[0]["meta"]["request_context"][
            "approval_level"
        ]
        == "auto"
    )
    execution_contract = channels.console.payloads[0]["meta"][
        "request_context"
    ]["execution_contract"]
    assert execution_contract["timeout_policy"]["approval_seconds"] == 15
    assert channels.console.payloads[0]["meta"]["request_context"][
        "model_slot_override"
    ] == {"provider_id": "dashscope", "model": "qwen-max"}
    delivered = channels.deliveries[0]["event"]
    assert delivered.content[0].text == "Scheduled result"
    inbox = SQLiteInboxProjectionStore(
        tmp_path / ".qwenpaw" / "lite" / "inbox.db",
    )
    items = await inbox.list_items(agent_id="default")
    assert len(items) == 1
    assert items[0].task_id is not None
    assert items[0].summary == "Scheduled result"
    task_service = task_service_for_workspace(workspace)
    task_id = UUID(result["task_id"])
    events_before = await task_service.list_events(
        task_id,
        after_sequence=0,
        limit=200,
    )
    await inbox.mark_read(
        items[0].item_id,
        agent_id="default",
        expected_revision=items[0].revision,
    )
    events_after = await task_service.list_events(
        task_id,
        after_sequence=0,
        limit=200,
    )
    assert events_after == events_before
