# -*- coding: utf-8 -*-
"""Tests for the transport-neutral Task application boundary."""

from pathlib import Path
from uuid import uuid4

import pytest

from qwenpaw.kernel.models import Run, RuntimeLaunchConfig, TaskSource
from qwenpaw.tasks.application import (
    CreateTaskCommand,
    InvalidTaskProjectDirectoryError,
    TaskApplicationService,
    TaskExecutionApplicationService,
)
from qwenpaw.tasks.ledger import SQLiteExecutionLedger
from qwenpaw.tasks.service import TaskNotFoundError, TaskService


class _RecordingOrchestrator:
    """Minimal runtime port used to verify application decisions."""

    def __init__(self) -> None:
        self.runner_id: str | None = None
        self.runtime_config: RuntimeLaunchConfig | None = None
        self.timeout_seconds: float | None = None

    async def start(
        self,
        task,
        *,
        runner_id,
        runtime_config,
        timeout_seconds,
    ) -> Run:
        self.runner_id = runner_id
        self.runtime_config = runtime_config
        self.timeout_seconds = timeout_seconds
        return Run(
            task_id=task.task_id,
            attempt=1,
            registry_generation=1,
            runner_id=runner_id,
        )


async def _application(
    tmp_path: Path,
) -> tuple[TaskApplicationService, TaskService]:
    store = SQLiteExecutionLedger(tmp_path / "ledger.db")
    await store.initialize()
    service = TaskService(store=store, registry_generation=1)
    return (
        TaskApplicationService(
            service,
            agent_id="default",
            default_project_dir=tmp_path,
        ),
        service,
    )


@pytest.mark.asyncio
async def test_create_normalizes_transport_input(
    tmp_path: Path,
) -> None:
    application, _ = await _application(tmp_path)
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    task = await application.create(
        CreateTaskCommand(
            objective="Prepare a report",
            source=TaskSource.USER,
            project_dir=str(project_dir),
            runner_id="runner.local",
            strategy_id="strategy.goal",
            approval_level="smart",
            metadata={"schedule_id": "reports.daily"},
        ),
    )

    assert task.metadata == {
        "workspace_dir": str(project_dir.resolve()),
        "runner_id": "runner.local",
        "strategy_id": "strategy.goal",
        "approval_level": "smart",
        "schedule_id": "reports.daily",
    }


@pytest.mark.asyncio
async def test_create_rejects_missing_project_directory(
    tmp_path: Path,
) -> None:
    application, _ = await _application(tmp_path)

    with pytest.raises(InvalidTaskProjectDirectoryError):
        await application.create(
            CreateTaskCommand(
                objective="Prepare a report",
                source=TaskSource.USER,
                project_dir=str(tmp_path / "missing"),
            ),
        )


@pytest.mark.asyncio
async def test_list_and_detail_return_transport_neutral_models(
    tmp_path: Path,
) -> None:
    application, service = await _application(tmp_path)
    created = await application.create(
        CreateTaskCommand(
            objective="Prepare a report",
            source=TaskSource.USER,
        ),
    )

    page = await application.list(cursor=None, limit=1)
    detail = await application.detail(created.task_id)

    assert page.items == (created,)
    assert page.next_cursor == str(created.task_id)
    assert detail.task == created
    assert detail.runs == ()
    assert detail.plan is None
    assert await service.get_task(created.task_id) == created


@pytest.mark.asyncio
async def test_detail_rejects_unknown_task(tmp_path: Path) -> None:
    application, _ = await _application(tmp_path)

    with pytest.raises(TaskNotFoundError):
        await application.detail(uuid4())


@pytest.mark.asyncio
async def test_workbench_returns_application_owned_read_model(
    tmp_path: Path,
) -> None:
    application, _ = await _application(tmp_path)
    created = await application.create(
        CreateTaskCommand(
            objective="Inspect the workbench",
            source=TaskSource.USER,
        ),
    )

    workbench = await application.workbench(created.task_id)

    assert workbench.task == created
    assert workbench.last_sequence == 1
    assert workbench.results.artifacts == ()
    assert workbench.usage.total_tokens == 0


@pytest.mark.asyncio
async def test_execution_start_selects_and_normalizes_runner(
    tmp_path: Path,
) -> None:
    application, service = await _application(tmp_path)
    created = await application.create(
        CreateTaskCommand(
            objective="Prepare a report",
            source=TaskSource.USER,
            runner_id="runner.legacy",
        ),
    )
    orchestrator = _RecordingOrchestrator()
    launch_config = RuntimeLaunchConfig(
        agent_id="default",
        project_dir=str(tmp_path),
        ledger_workspace_dir=str(tmp_path),
    )
    execution = TaskExecutionApplicationService(
        service,
        orchestrator,
        runtime_config_factory=lambda _task: launch_config,
        default_runner_id="runner.default",
        runner_id_normalizer=lambda value: f"normalized.{value}",
        timeout_seconds=45,
    )

    result = await execution.start(created.task_id)

    assert result.task == created
    assert result.run.task_id == created.task_id
    assert orchestrator.runner_id == "normalized.runner.legacy"
    assert orchestrator.runtime_config == launch_config
    assert orchestrator.timeout_seconds == 45
