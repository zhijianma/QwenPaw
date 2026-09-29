# -*- coding: utf-8 -*-
"""Framework-independent Task commands and read use cases."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from uuid import UUID

from ..kernel.models import (
    ExecutionContract,
    JsonObject,
    Plan,
    Run,
    RuntimeLaunchConfig,
    Task,
    TaskSource,
    TaskStatus,
)
from .service import TaskNotFoundError, TaskService
from .workbench import TaskWorkbenchReadModel, load_task_workbench


class InvalidTaskProjectDirectoryError(ValueError):
    """Raised when a Task targets a directory that does not exist."""


class TaskExecutionOrchestrator(Protocol):
    """Minimum runtime port required by application commands."""

    async def start(
        self,
        task: Task,
        *,
        runner_id: str,
        runtime_config: RuntimeLaunchConfig,
        timeout_seconds: float,
    ) -> Run:
        """Start one execution attempt."""

    async def cancel(
        self,
        task_id: UUID,
        *,
        idempotency_key: str | None = None,
    ) -> Task:
        """Cancel one execution attempt."""

    async def recover_orphaned(self, task_id: UUID) -> Task:
        """Recover an active attempt with no runtime owner."""

    async def resume(
        self,
        task: Task,
        *,
        runtime_config: RuntimeLaunchConfig,
        idempotency_key: str | None,
        timeout_seconds: float,
    ) -> Run:
        """Resume one execution attempt."""


@dataclass(frozen=True, slots=True)
class CreateTaskCommand:  # pylint: disable=too-many-instance-attributes
    """Transport-neutral input for creating one durable Task."""

    objective: str
    source: TaskSource
    constraints: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    execution_contract: ExecutionContract | None = None
    project_dir: str | None = None
    runner_id: str | None = None
    strategy_id: str | None = None
    approval_level: str | None = None
    metadata: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TaskPage:
    """Cursor-ready Task page independent from HTTP serialization."""

    items: tuple[Task, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class TaskDetail:
    """Stable aggregate read used by API and future channel adapters."""

    task: Task
    runs: tuple[Run, ...]
    plan: Plan | None


@dataclass(frozen=True, slots=True)
class TaskExecutionResult:
    """Updated Task aggregate paired with its active attempt."""

    task: Task
    run: Run


class TaskApplicationService:
    """Application boundary shared by current and replacement routes."""

    def __init__(
        self,
        service: TaskService,
        *,
        agent_id: str,
        default_project_dir: Path,
    ) -> None:
        self._service = service
        self._agent_id = agent_id
        self._default_project_dir = Path(default_project_dir)

    async def create(
        self,
        command: CreateTaskCommand,
        *,
        idempotency_key: str | None = None,
    ) -> Task:
        """Validate application scope and create one durable Task."""
        project_dir = Path(
            command.project_dir or self._default_project_dir,
        ).expanduser()
        if not project_dir.is_dir():
            raise InvalidTaskProjectDirectoryError(str(project_dir))
        project_dir = project_dir.resolve()
        metadata: JsonObject = dict(command.metadata)
        metadata["workspace_dir"] = str(project_dir)
        optional_metadata = {
            "runner_id": command.runner_id,
            "strategy_id": command.strategy_id,
            "approval_level": command.approval_level,
        }
        metadata.update(
            {
                key: value
                for key, value in optional_metadata.items()
                if value is not None
            },
        )
        return await self._service.create_task(
            objective=command.objective,
            agent_id=self._agent_id,
            source=command.source,
            constraints=command.constraints,
            acceptance_criteria=command.acceptance_criteria,
            execution_contract=command.execution_contract,
            metadata=metadata,
            idempotency_key=idempotency_key,
        )

    async def list(
        self,
        *,
        cursor: str | None,
        limit: int,
    ) -> TaskPage:
        """Return one bounded page without exposing storage details."""
        tasks = tuple(
            await self._service.list_tasks(cursor=cursor, limit=limit),
        )
        return TaskPage(
            items=tasks,
            next_cursor=(
                str(tasks[-1].task_id) if len(tasks) == limit else None
            ),
        )

    async def detail(self, task_id: UUID) -> TaskDetail:
        """Return the Task aggregate or a domain-level not-found error."""
        task = await self._service.get_task(task_id)
        if task is None:
            raise TaskNotFoundError(str(task_id))
        return TaskDetail(
            task=task,
            runs=tuple(await self._service.list_runs(task_id)),
            plan=await self._service.latest_plan(task_id),
        )

    async def workbench(self, task_id: UUID) -> TaskWorkbenchReadModel:
        """Return the transport-neutral Task Workbench projection."""
        return await load_task_workbench(self._service, task_id)


class TaskExecutionApplicationService:
    """Transport-neutral commands for Task execution lifecycle."""

    def __init__(
        self,
        service: TaskService,
        orchestrator: TaskExecutionOrchestrator,
        *,
        runtime_config_factory: Callable[[Task], RuntimeLaunchConfig],
        default_runner_id: str,
        runner_id_normalizer: Callable[[str], str],
        timeout_seconds: float,
    ) -> None:
        self._service = service
        self._orchestrator = orchestrator
        self._runtime_config_factory = runtime_config_factory
        self._default_runner_id = default_runner_id
        self._runner_id_normalizer = runner_id_normalizer
        self._timeout_seconds = timeout_seconds

    async def start(self, task_id: UUID) -> TaskExecutionResult:
        """Start one created Task using its selected capability."""
        task = await self._required_task(task_id)
        requested_runner = task.metadata.get("runner_id")
        runner_id = (
            self._runner_id_normalizer(requested_runner)
            if isinstance(requested_runner, str)
            else self._default_runner_id
        )
        run = await self._orchestrator.start(
            task,
            runner_id=runner_id,
            runtime_config=self._runtime_config_factory(task),
            timeout_seconds=self._timeout_seconds,
        )
        return TaskExecutionResult(
            task=await self._required_task(task_id),
            run=run,
        )

    async def cancel(
        self,
        task_id: UUID,
        *,
        idempotency_key: str | None = None,
    ) -> Task:
        """Cancel one non-terminal Task and its attached execution."""
        return await self._orchestrator.cancel(
            task_id,
            idempotency_key=idempotency_key,
        )

    async def resume(
        self,
        task_id: UUID,
        *,
        idempotency_key: str | None = None,
    ) -> TaskExecutionResult:
        """Recover ownership when needed and resume from a checkpoint."""
        task = await self._required_task(task_id)
        if task.status in {
            TaskStatus.RUNNING,
            TaskStatus.WAITING_APPROVAL,
        }:
            task = await self._orchestrator.recover_orphaned(task_id)
        run = await self._orchestrator.resume(
            task,
            runtime_config=self._runtime_config_factory(task),
            idempotency_key=idempotency_key,
            timeout_seconds=self._timeout_seconds,
        )
        return TaskExecutionResult(
            task=await self._required_task(task_id),
            run=run,
        )

    async def _required_task(self, task_id: UUID) -> Task:
        task = await self._service.get_task(task_id)
        if task is None:
            raise TaskNotFoundError(str(task_id))
        return task


__all__ = [
    "CreateTaskCommand",
    "InvalidTaskProjectDirectoryError",
    "TaskApplicationService",
    "TaskDetail",
    "TaskExecutionApplicationService",
    "TaskExecutionOrchestrator",
    "TaskExecutionResult",
    "TaskPage",
    "TaskWorkbenchReadModel",
]
