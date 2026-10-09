# -*- coding: utf-8 -*-
"""Tests for explicit Cron legacy/durable runtime routing."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qwenpaw.app.crons.api import get_job
from qwenpaw.app.crons.manager import CronManager
from qwenpaw.app.crons.models import CronRuntimeDecisionCode
from qwenpaw.app.crons.task_runtime import LiteCronTaskRuntime
from qwenpaw.inbox import SQLiteInboxProjectionStore
from qwenpaw.operations import SQLiteOperationalEventStore
from qwenpaw.plugins.generations import GenerationRegistry
from tests.unit.app.conftest import (
    InMemoryJobRepository,
    make_cron_job_spec,
)


class _DurableRuntime:
    def __init__(self, *, supported: bool) -> None:
        self.supported = supported
        self.execute = AsyncMock(
            return_value={
                "run_id": "run-durable",
                "delivery_status": "success",
            },
        )

    def supports(self, _job) -> bool:
        return self.supported


@pytest.mark.asyncio
@pytest.mark.parametrize("supported", [True, False])
async def test_manager_routes_only_lossless_jobs_to_durable_runtime(
    monkeypatch,
    supported: bool,
) -> None:
    repo = InMemoryJobRepository()
    durable = _DurableRuntime(supported=supported)
    manager = CronManager(
        repo=repo,
        workspace=AsyncMock(),
        channel_manager=AsyncMock(),
        agent_id="default",
        task_runtime=durable,
    )
    legacy = AsyncMock(
        return_value={
            "run_id": "run-legacy",
            "delivery_status": "success",
        },
    )
    manager._executor.execute = legacy  # pylint: disable=protected-access
    monkeypatch.setattr(
        "qwenpaw.app.crons.manager.publish_cron_event",
        AsyncMock(),
    )
    job = make_cron_job_spec(job_id="daily")
    job.save_result_to_inbox = False

    await manager._execute_once(  # pylint: disable=protected-access
        job,
        trigger="manual",
    )

    if supported:
        call = durable.execute.await_args
        assert call.args == (job,)
        assert call.kwargs["trigger"] == "manual"
        assert call.kwargs["scheduled_for"].tzinfo is not None
        legacy.assert_not_awaited()
    else:
        durable.execute.assert_not_awaited()
        legacy.assert_awaited_once_with(job)
    state = manager.get_state("daily")
    assert state.last_status == "success"
    assert state.runtime_decision is not None
    history = await manager.get_history("daily")
    assert history[0].runtime_decision == state.runtime_decision
    assert state.runtime_decision.reason_code is (
        CronRuntimeDecisionCode.RUNTIME_DECISION_UNAVAILABLE
        if supported
        else CronRuntimeDecisionCode.RUNTIME_DECLINED
    )


def test_manager_reports_missing_durable_runtime() -> None:
    manager = CronManager(
        repo=InMemoryJobRepository(),
        workspace=AsyncMock(),
        channel_manager=AsyncMock(),
        agent_id="default",
    )

    decision = manager.runtime_decision(
        make_cron_job_spec(job_id="legacy"),
    )

    assert (
        decision.reason_code
        is CronRuntimeDecisionCode.TASK_RUNTIME_UNAVAILABLE
    )
    assert not decision.uses_durable_runtime


@pytest.mark.asyncio
async def test_job_detail_exposes_decision_before_first_run() -> None:
    repo = InMemoryJobRepository()
    job = make_cron_job_spec(job_id="stream")
    await repo.upsert_job(job)
    manager = CronManager(
        repo=repo,
        workspace=AsyncMock(),
        channel_manager=AsyncMock(),
        agent_id="default",
        task_runtime=_DurableRuntime(supported=False),
    )

    view = await get_job("stream", manager)

    assert view.runtime_decision is not None
    assert (
        view.runtime_decision.reason_code
        is CronRuntimeDecisionCode.RUNTIME_DECLINED
    )
    assert view.state.runtime_decision is None


@pytest.mark.asyncio
async def test_stream_fallback_publishes_persistent_migration_warning(
    monkeypatch,
    tmp_path,
) -> None:
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        capability_registry=GenerationRegistry(),
    )
    manager = CronManager(
        repo=InMemoryJobRepository(),
        workspace=workspace,
        channel_manager=AsyncMock(),
        agent_id="default",
        task_runtime=LiteCronTaskRuntime(workspace),
    )
    legacy = AsyncMock(
        return_value={
            "run_id": "legacy-stream",
            "delivery_status": "success",
        },
    )
    manager._executor.execute = legacy  # pylint: disable=protected-access
    publish = AsyncMock()
    monkeypatch.setattr(
        "qwenpaw.app.crons.manager.publish_cron_event",
        publish,
    )
    job = make_cron_job_spec(job_id="stream")
    job.save_result_to_inbox = False

    await manager._execute_once(  # pylint: disable=protected-access
        job,
        trigger="scheduled",
    )

    legacy.assert_awaited_once_with(job)
    publish.assert_awaited_once()
    warning = publish.await_args.kwargs
    assert warning["event_type"] == "cron_compatibility_fallback"
    assert warning["severity"] == "warning"
    assert warning["title"] == "Cron compatibility path active"
    assert "job_name" not in warning["payload"]
    assert warning["payload"] == {
        "job_id": "stream",
        "task_type": "agent",
        "dispatch_mode": "stream",
        "trigger": "scheduled",
        "runtime_path": "legacy_executor",
        "reason_code": "stream_delivery_unverified",
        "removal_gates": [
            "Pass browser stream equivalence validation.",
            "Pass one live external media Channel validation.",
        ],
    }


@pytest.mark.asyncio
async def test_stream_warning_failure_does_not_block_legacy_execution(
    monkeypatch,
    tmp_path,
) -> None:
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        capability_registry=GenerationRegistry(),
    )
    manager = CronManager(
        repo=InMemoryJobRepository(),
        workspace=workspace,
        channel_manager=AsyncMock(),
        agent_id="default",
        task_runtime=LiteCronTaskRuntime(workspace),
    )
    legacy = AsyncMock(
        return_value={"run_id": "legacy", "delivery_status": "success"},
    )
    manager._executor.execute = legacy  # pylint: disable=protected-access
    monkeypatch.setattr(
        "qwenpaw.app.crons.manager.publish_cron_event",
        AsyncMock(side_effect=RuntimeError("operations unavailable")),
    )
    job = make_cron_job_spec(job_id="stream")
    job.save_result_to_inbox = False

    await manager._execute_once(job)  # pylint: disable=protected-access

    legacy.assert_awaited_once_with(job)
    assert manager.get_state("stream").last_status == "success"


@pytest.mark.asyncio
async def test_stream_warning_is_durable_and_idempotent(
    tmp_path,
) -> None:
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        capability_registry=GenerationRegistry(),
    )
    manager = CronManager(
        repo=InMemoryJobRepository(),
        workspace=workspace,
        channel_manager=AsyncMock(),
        agent_id="default",
        task_runtime=LiteCronTaskRuntime(workspace),
    )
    manager._executor.execute = AsyncMock(  # pylint: disable=protected-access
        return_value={"run_id": "legacy", "delivery_status": "success"},
    )
    job = make_cron_job_spec(job_id="stream")
    job.save_result_to_inbox = False

    await manager._execute_once(job)  # pylint: disable=protected-access
    await manager._execute_once(job)  # pylint: disable=protected-access

    data_dir = tmp_path / ".qwenpaw" / "lite"
    events = await SQLiteOperationalEventStore(
        data_dir / "operations.db",
    ).list_events(agent_id="default")
    items = await SQLiteInboxProjectionStore(
        data_dir / "inbox.db",
    ).list_items(agent_id="default")
    history = await manager.get_history("stream")

    assert len(history) == 2
    assert len(events) == 1
    assert events[0].event_type == "cron_compatibility_fallback"
    assert events[0].payload["reason_code"] == ("stream_delivery_unverified")
    assert len(items) == 1
    assert items[0].source_event_id == events[0].event_id
