# -*- coding: utf-8 -*-
"""Unified orchestration for planning and executing durable tasks."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from ..kernel.models import (
    ExecutionCheckpoint,
    Run,
    RuntimeLaunchConfig,
    Task,
    TaskOrder,
    TaskStatus,
)
from ..kernel.ports import (
    CapabilityLease,
    CapabilityResolver,
    ExecutableCapabilityLease,
    RuntimeStrategy,
    TaskPlanner,
)
from .context import order_metadata_from_launch_config
from .cancellation import RuntimeCancellationToken
from .runner import (
    TaskExecutionBudgetExceededError,
    TaskExecutionCoordinator,
)
from .service import CheckpointNotResumableError, TaskService
from .system_contributions import (
    SYSTEM_BASIC_PLANNER_ID,
    SYSTEM_DEFAULT_STRATEGY_ID,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _SupervisedExecution:
    execution: asyncio.Task[Run]
    cancellation: RuntimeCancellationToken


class PlannerCapabilityUnavailableError(LookupError):
    """Raised when a pinned generation cannot supply a planner."""

    def __init__(self, capability_id: str, reason: str) -> None:
        self.capability_id = capability_id
        self.reason = reason
        super().__init__(
            f"planner capability '{capability_id}' is unavailable: {reason}",
        )


class StrategyCapabilityUnavailableError(LookupError):
    """Raised when a pinned generation cannot supply a strategy."""

    def __init__(self, capability_id: str, reason: str) -> None:
        self.capability_id = capability_id
        self.reason = reason
        super().__init__(
            f"strategy capability '{capability_id}' is unavailable: "
            f"{reason}",
        )


class TaskExecutionStillAttachedError(RuntimeError):
    """Raised when recovery targets a run still owned by this process."""


class TaskRuntimeSupervisor:
    """Own process-local execution handles outside the HTTP layer."""

    def __init__(self) -> None:
        self._executions: dict[UUID, _SupervisedExecution] = {}

    def register(
        self,
        task_id: UUID,
        execution: asyncio.Task[Run],
        cancellation: RuntimeCancellationToken,
    ) -> None:
        """Track one live execution and consume its terminal exception."""
        current = self._executions.get(task_id)
        if current is not None and not current.execution.done():
            raise RuntimeError(f"task already executing: {task_id}")
        self._executions[task_id] = _SupervisedExecution(
            execution=execution,
            cancellation=cancellation,
        )
        execution.add_done_callback(
            lambda finished: self._forget(task_id, finished),
        )

    def _forget(
        self,
        task_id: UUID,
        execution: asyncio.Task[Run],
    ) -> None:
        current = self._executions.get(task_id)
        if current is not None and current.execution is execution:
            self._executions.pop(task_id, None)
        if execution.cancelled():
            return
        error = execution.exception()
        if error is not None:
            logger.warning(
                "Durable task execution failed for %s: %s",
                task_id,
                type(error).__name__,
            )

    async def cancel(
        self,
        task_id: UUID,
        *,
        reason: str = "Task cancellation requested",
    ) -> bool:
        """Cancel and drain the matching local execution when present."""
        supervised = self._executions.pop(task_id, None)
        if supervised is None or supervised.execution.done():
            return False
        supervised.cancellation.cancel(reason)
        await asyncio.sleep(0)
        if not supervised.execution.done():
            supervised.execution.cancel()
        await asyncio.gather(
            supervised.execution,
            return_exceptions=True,
        )
        return True

    def contains(self, task_id: UUID) -> bool:
        """Return whether a live handle is owned for diagnostics/tests."""
        supervised = self._executions.get(task_id)
        return supervised is not None and not supervised.execution.done()


class TaskRuntimeOrchestrator:
    """Resolve one generation and own the full start execution boundary."""

    def __init__(
        self,
        service: TaskService,
        capability_resolver: CapabilityResolver,
        supervisor: TaskRuntimeSupervisor,
    ) -> None:
        self._service = service
        self._capability_resolver = capability_resolver
        self._supervisor = supervisor
        self._coordinator = TaskExecutionCoordinator(
            service,
            capability_resolver,
        )
        self._resume_locks: dict[UUID, asyncio.Lock] = {}

    async def start(
        self,
        task: Task,
        *,
        runner_id: str,
        planner_id: str = SYSTEM_BASIC_PLANNER_ID,
        strategy_id: str = SYSTEM_DEFAULT_STRATEGY_ID,
        runtime_config: RuntimeLaunchConfig | None = None,
        timeout_seconds: float | None = None,
        registry_generation: int | None = None,
    ) -> Run:
        """Plan and start a task from one immutable capability lease."""
        lease = await self._capability_resolver.pin(registry_generation)
        try:
            selected_strategy_id = (
                runtime_config.strategy_id
                if runtime_config is not None
                else strategy_id
            )
            order = TaskOrder(
                task_id=task.task_id,
                objective=task.objective,
                constraints=task.constraints,
                acceptance_criteria=task.acceptance_criteria,
                execution_contract=task.execution_contract,
                metadata=(
                    order_metadata_from_launch_config(runtime_config)
                    if runtime_config is not None
                    else {"strategy_id": selected_strategy_id}
                ),
            )
            if task.status is TaskStatus.CREATED:
                planner = self._resolve_planner(lease, planner_id)
                steps = tuple(await planner.plan(order))
                task, _ = await self._service.plan_task(
                    task.task_id,
                    steps=steps,
                )
            runner = self._coordinator.resolve_runner(lease, runner_id)
            strategy = self.resolve_strategy(
                lease,
                selected_strategy_id,
            )
            (
                run,
                execution,
                cancellation,
            ) = await self._coordinator.start_pinned(
                order,
                runner,
                lease,
                strategy=strategy,
                timeout_seconds=timeout_seconds,
            )
        except Exception:
            await lease.close()
            raise
        try:
            self._supervisor.register(
                task.task_id,
                execution,
                cancellation,
            )
        except Exception:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            raise
        return run

    async def cancel(
        self,
        task_id: UUID,
        *,
        idempotency_key: str | None = None,
    ) -> Task:
        """Stop the owned execution before committing terminal state."""
        await self._supervisor.cancel(
            task_id,
            reason="Task cancelled by user",
        )
        return await self._service.cancel_task(
            task_id,
            idempotency_key=idempotency_key,
        )

    async def recover_orphaned(self, task_id: UUID) -> Task:
        """Convert a process-orphaned active run into a resumable failure."""
        if self._supervisor.contains(task_id):
            raise TaskExecutionStillAttachedError(str(task_id))
        return await self._service.recover_orphaned_task(task_id)

    async def suspend(
        self,
        task_id: UUID,
        *,
        runner_cursor: Any = None,
        workspace_checkpoint_ref: str | None = None,
    ) -> ExecutionCheckpoint:
        """Stop the local execution and commit a safe resume boundary."""
        await self._supervisor.cancel(
            task_id,
            reason="Task suspended at a safe checkpoint",
        )
        return await self._service.suspend_task(
            task_id,
            runner_cursor=runner_cursor,
            workspace_checkpoint_ref=workspace_checkpoint_ref,
        )

    async def resume(
        self,
        task: Task,
        *,
        runtime_config: RuntimeLaunchConfig | None = None,
        strategy_id: str | None = None,
        idempotency_key: str | None = None,
        timeout_seconds: float | None = None,
    ) -> Run:
        """Serialize resume commands for one Task and start at most once."""
        lock = self._resume_locks.setdefault(task.task_id, asyncio.Lock())
        async with lock:
            return await self._resume_once(
                task,
                runtime_config=runtime_config,
                strategy_id=strategy_id,
                idempotency_key=idempotency_key,
                timeout_seconds=timeout_seconds,
            )

    async def _resume_once(
        self,
        task: Task,
        *,
        runtime_config: RuntimeLaunchConfig | None = None,
        strategy_id: str | None = None,
        idempotency_key: str | None = None,
        timeout_seconds: float | None = None,
    ) -> Run:
        """Resume a checkpoint through its recorded runtime capabilities."""
        snapshot = await self._service.projection_snapshot(task.task_id)
        checkpoint = snapshot.checkpoint
        if checkpoint is None:
            raise CheckpointNotResumableError(str(task.task_id))
        runs = list(await self._service.list_runs(task.task_id))
        if not runs:
            raise RuntimeError("checkpoint task has no prior run")
        selected_strategy_id = (
            runtime_config.strategy_id
            if runtime_config is not None
            else (
                strategy_id
                or runs[-1].strategy_id
                or SYSTEM_DEFAULT_STRATEGY_ID
            )
        )
        replayed = await self._service.replayed_resume_task(
            task.task_id,
            strategy_id=selected_strategy_id,
            idempotency_key=idempotency_key,
        )
        if replayed is not None:
            return replayed.run
        contract = task.execution_contract
        if contract is not None:
            retries_used = max(len(runs) - 1, 0)
            if retries_used >= contract.budget.max_retries:
                raise TaskExecutionBudgetExceededError(
                    "task execution exhausted its retry budget",
                )
        runner_id = runs[-1].runner_id
        lease = await self._capability_resolver.pin()
        try:
            runner = self._coordinator.resolve_runner(lease, runner_id)
            strategy = self.resolve_strategy(
                lease,
                selected_strategy_id,
            )
            order = TaskOrder(
                task_id=task.task_id,
                objective=task.objective,
                constraints=task.constraints,
                acceptance_criteria=task.acceptance_criteria,
                execution_contract=task.execution_contract,
                metadata=(
                    order_metadata_from_launch_config(
                        runtime_config,
                        resume_checkpoint=checkpoint,
                    )
                    if runtime_config is not None
                    else {
                        "resume_checkpoint": checkpoint.model_dump(
                            mode="json",
                        ),
                        "strategy_id": selected_strategy_id,
                    }
                ),
            )
            (
                run,
                execution,
                cancellation,
            ) = await self._coordinator.resume_pinned(
                order,
                runner,
                lease,
                strategy=strategy,
                idempotency_key=idempotency_key,
                timeout_seconds=timeout_seconds,
            )
        except Exception:
            await lease.close()
            raise
        if execution is None or cancellation is None:
            return run
        try:
            self._supervisor.register(
                task.task_id,
                execution,
                cancellation,
            )
        except Exception:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            raise
        return run

    @staticmethod
    def _resolve_planner(
        lease: CapabilityLease,
        capability_id: str,
    ) -> TaskPlanner:
        descriptor = lease.resolve(capability_id)
        if descriptor is None:
            raise PlannerCapabilityUnavailableError(
                capability_id,
                "not present in the pinned registry generation",
            )
        if descriptor.slot != "planner":
            raise PlannerCapabilityUnavailableError(
                capability_id,
                f"declares slot '{descriptor.slot}' instead of 'planner'",
            )
        if not isinstance(lease, ExecutableCapabilityLease):
            raise PlannerCapabilityUnavailableError(
                capability_id,
                "resolver does not expose executable implementations",
            )
        implementation = lease.implementation(capability_id)
        if not isinstance(implementation, TaskPlanner):
            raise PlannerCapabilityUnavailableError(
                capability_id,
                "implementation does not satisfy TaskPlanner",
            )
        if implementation.planner_id != capability_id:
            raise PlannerCapabilityUnavailableError(
                capability_id,
                "implementation planner_id does not match capability ID",
            )
        return implementation

    @staticmethod
    def resolve_strategy(
        lease: CapabilityLease,
        capability_id: str,
    ) -> RuntimeStrategy:
        """Validate a strategy descriptor and implementation as one unit."""
        descriptor = lease.resolve(capability_id)
        if descriptor is None:
            raise StrategyCapabilityUnavailableError(
                capability_id,
                "not present in the pinned registry generation",
            )
        if descriptor.slot != "strategy":
            raise StrategyCapabilityUnavailableError(
                capability_id,
                f"declares slot '{descriptor.slot}' instead of 'strategy'",
            )
        if not isinstance(lease, ExecutableCapabilityLease):
            raise StrategyCapabilityUnavailableError(
                capability_id,
                "resolver does not expose executable implementations",
            )
        implementation = lease.implementation(capability_id)
        if not isinstance(implementation, RuntimeStrategy):
            raise StrategyCapabilityUnavailableError(
                capability_id,
                "implementation does not satisfy RuntimeStrategy",
            )
        if implementation.strategy_id != capability_id:
            raise StrategyCapabilityUnavailableError(
                capability_id,
                "implementation strategy_id does not match capability ID",
            )
        return implementation
