# -*- coding: utf-8 -*-
"""Application service connecting Schedule Fires to the Task Runtime."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from ..kernel import (
    ApprovalLevel,
    ModelSelection,
    Run,
    RuntimeLaunchConfig,
    ScheduleDefinition,
    ScheduleFire,
    ScheduleLease,
    ScheduleLeaseStatus,
    SchedulerPort,
    Task,
    TaskSource,
    TaskStatus,
)
from ..kernel.ports import CapabilityResolver, ExecutableCapabilityLease
from ..kernel.state_machine import InvalidTaskTransition
from ..tasks.application import CreateTaskCommand, TaskApplicationService
from ..tasks.ledger import EventSequenceError, TaskVersionConflictError
from ..tasks.runtime import TaskRuntimeOrchestrator
from ..tasks.system_contributions import SYSTEM_DEFAULT_STRATEGY_ID

DEFAULT_SCHEDULER_CAPABILITY_ID = (
    "qwenpaw.system.tasks.local-durable-scheduler"
)


class ScheduleDispatchDisposition(str, Enum):
    """Outcome of one trigger admission attempt."""

    STARTED = "started"
    RECOVERED = "recovered"
    OWNED_ELSEWHERE = "owned_elsewhere"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True)
class ScheduleDispatchResult:
    """Authoritative lease plus optional Task execution facts."""

    lease: ScheduleLease
    disposition: ScheduleDispatchDisposition
    task: Task | None = None
    run: Run | None = None


class SchedulerCapabilityUnavailableError(LookupError):
    """Raised when a pinned generation cannot supply its Scheduler."""


class ScheduleDispatchAccountingError(RuntimeError):
    """Raised when a failed dispatch cannot be durably accounted for."""


class ScheduledTaskDispatcher:
    """Claim one Fire and create exactly one generation-pinned Task."""

    def __init__(
        self,
        *,
        capability_resolver: CapabilityResolver,
        task_application: TaskApplicationService,
        task_orchestrator: TaskRuntimeOrchestrator,
        ledger_workspace_dir: Path,
        scheduler_capability_id: str = DEFAULT_SCHEDULER_CAPABILITY_ID,
        lease_seconds: float = 30.0,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("schedule lease duration must be positive")
        self._capability_resolver = capability_resolver
        self._task_application = task_application
        self._task_orchestrator = task_orchestrator
        self._ledger_workspace_dir = Path(ledger_workspace_dir)
        self._scheduler_capability_id = scheduler_capability_id
        self._lease_seconds = lease_seconds

    async def dispatch(
        self,
        definition: ScheduleDefinition,
        *,
        scheduled_for: datetime,
        idempotency_key: str,
        owner_id: str,
    ) -> ScheduleDispatchResult:
        """Claim, materialize, bind, and start one scheduled Task."""
        generation_lease = await self._capability_resolver.pin()
        claimed: ScheduleLease | None = None
        task: Task | None = None
        bound: ScheduleLease | None = None
        try:
            scheduler = self._resolve_scheduler(generation_lease)
            fire = ScheduleFire(
                agent_id=definition.agent_id,
                schedule_id=definition.schedule_id,
                registry_generation=generation_lease.generation,
                scheduled_for=scheduled_for,
                idempotency_key=idempotency_key,
            )
            await scheduler.upsert(definition)
            claimed = await scheduler.claim(
                fire,
                owner_id=owner_id,
                lease_seconds=self._lease_seconds,
            )
            if claimed.status is not ScheduleLeaseStatus.CLAIMED:
                return await self._replay_bound_task(
                    definition,
                    claimed,
                )
            if claimed.owner_id != owner_id:
                return ScheduleDispatchResult(
                    lease=claimed,
                    disposition=(ScheduleDispatchDisposition.OWNED_ELSEWHERE),
                )
            task = await self._create_task(definition, fire)
            bound = await scheduler.complete(
                claimed.lease_id,
                owner_id=owner_id,
                expected_revision=claimed.revision,
                task_id=task.task_id,
            )
            return await self._start_bound_task(
                definition,
                bound,
                task,
                started_disposition=ScheduleDispatchDisposition.STARTED,
            )
        except Exception as error:
            if (
                claimed is not None
                and claimed.status is ScheduleLeaseStatus.CLAIMED
                and claimed.owner_id == owner_id
                and task is None
            ):
                try:
                    await scheduler.fail(
                        claimed.lease_id,
                        owner_id=owner_id,
                        expected_revision=claimed.revision,
                        error_code=type(error).__name__,
                    )
                except Exception as accounting_error:
                    raise ScheduleDispatchAccountingError(
                        "schedule dispatch failure was not persisted",
                    ) from accounting_error
            raise
        finally:
            await generation_lease.close()

    async def _replay_bound_task(
        self,
        definition: ScheduleDefinition,
        lease: ScheduleLease,
    ) -> ScheduleDispatchResult:
        """Recover a bound Task or replay an already-started execution."""
        if lease.task_id is None:
            return ScheduleDispatchResult(
                lease=lease,
                disposition=ScheduleDispatchDisposition.REPLAYED,
            )
        detail = await self._task_application.detail(lease.task_id)
        return await self._start_bound_task(
            definition,
            lease,
            detail.task,
            started_disposition=ScheduleDispatchDisposition.RECOVERED,
        )

    async def _start_bound_task(
        self,
        definition: ScheduleDefinition,
        lease: ScheduleLease,
        task: Task,
        *,
        started_disposition: ScheduleDispatchDisposition,
    ) -> ScheduleDispatchResult:
        """Start a bound Task once, tolerating another recovery winner."""
        current = task
        for attempt in range(3):
            detail = await self._task_application.detail(current.task_id)
            current = detail.task
            if current.status not in {
                TaskStatus.CREATED,
                TaskStatus.PLANNED,
            }:
                return ScheduleDispatchResult(
                    lease=lease,
                    disposition=ScheduleDispatchDisposition.REPLAYED,
                    task=current,
                    run=(detail.runs[-1] if detail.runs else None),
                )
            try:
                run = await self._task_orchestrator.start(
                    current,
                    planner_id=definition.planner_id,
                    runner_id=definition.runner_id,
                    strategy_id=(
                        definition.strategy_id or SYSTEM_DEFAULT_STRATEGY_ID
                    ),
                    runtime_config=self._runtime_config(
                        current,
                        definition,
                    ),
                    registry_generation=lease.fire.registry_generation,
                )
                return ScheduleDispatchResult(
                    lease=lease,
                    disposition=started_disposition,
                    task=current,
                    run=run,
                )
            except (
                EventSequenceError,
                InvalidTaskTransition,
                TaskVersionConflictError,
            ):
                if attempt == 2:
                    raise
                await asyncio.sleep(0)
        raise AssertionError("schedule recovery retry loop exhausted")

    async def _create_task(
        self,
        definition: ScheduleDefinition,
        fire: ScheduleFire,
    ) -> Task:
        fire_id = fire.fire_id
        if fire_id is None:  # pragma: no cover - Kernel validator
            raise ValueError("schedule fire has no stable identity")
        return await self._task_application.create(
            CreateTaskCommand(
                objective=definition.objective,
                source=TaskSource.SCHEDULE,
                execution_contract=definition.execution_contract,
                runner_id=definition.runner_id,
                strategy_id=definition.strategy_id,
                metadata={
                    "schedule_id": definition.schedule_id,
                    "schedule_fire_id": str(fire_id),
                    "schedule_idempotency_key": fire.idempotency_key,
                    "schedule_registry_generation": (fire.registry_generation),
                    "conversation_id": definition.conversation_id,
                    "model_selection": definition.metadata.get(
                        "model_selection",
                    ),
                },
            ),
            idempotency_key=f"schedule-task:{fire_id}",
        )

    def _runtime_config(
        self,
        task: Task,
        definition: ScheduleDefinition,
    ) -> RuntimeLaunchConfig:
        project_dir = task.metadata.get("workspace_dir")
        if not isinstance(project_dir, str):
            raise ValueError("scheduled Task has no workspace_dir")
        raw_approval_level = definition.metadata.get("approval_level")
        approval_level = (
            ApprovalLevel(raw_approval_level)
            if isinstance(raw_approval_level, str)
            else ApprovalLevel.AGENT_PROFILE
        )
        raw_model_selection = definition.metadata.get("model_selection")
        model_selection = (
            ModelSelection.model_validate(raw_model_selection)
            if isinstance(raw_model_selection, dict)
            else None
        )
        return RuntimeLaunchConfig(
            agent_id=definition.agent_id,
            conversation_id=definition.conversation_id,
            project_dir=project_dir,
            ledger_workspace_dir=str(self._ledger_workspace_dir),
            approval_level=approval_level,
            model_selection=model_selection,
            strategy_id=(definition.strategy_id or SYSTEM_DEFAULT_STRATEGY_ID),
        )

    def _resolve_scheduler(self, lease) -> SchedulerPort:
        descriptor = lease.resolve(self._scheduler_capability_id)
        if descriptor is None:
            raise SchedulerCapabilityUnavailableError(
                f"scheduler capability '{self._scheduler_capability_id}' "
                "is absent",
            )
        if descriptor.slot != "scheduler":
            raise SchedulerCapabilityUnavailableError(
                f"capability '{self._scheduler_capability_id}' declares "
                f"slot '{descriptor.slot}'",
            )
        if not isinstance(lease, ExecutableCapabilityLease):
            raise SchedulerCapabilityUnavailableError(
                "scheduler resolver does not expose implementations",
            )
        implementation = lease.implementation(
            self._scheduler_capability_id,
        )
        if not isinstance(implementation, SchedulerPort):
            raise SchedulerCapabilityUnavailableError(
                f"capability '{self._scheduler_capability_id}' does not "
                "implement SchedulerPort",
            )
        return implementation
