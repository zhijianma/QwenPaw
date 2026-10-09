# -*- coding: utf-8 -*-
"""Application service connecting Schedule Fires to the Task Runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from ..delivery import (
    DeliveryDispatchDisposition,
    DeliveryDispatchResult,
    DeliveryDispatcher,
)
from ..kernel import (
    ApprovalLevel,
    ModelSelection,
    Run,
    RuntimeLaunchConfig,
    ScheduleDefinition,
    ScheduleFire,
    ScheduleLease,
    ScheduleLeaseStatus,
    ScheduleTriggerCursorStore,
    ScheduleWorkKind,
    DeliveryProjectionPort,
    DeliveryRequest,
    SchedulerHost,
    SchedulerPort,
    SchedulerProvider,
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
from .trigger_worker import (
    DurableScheduleTriggerWorker,
    ScheduleOccurrenceHandler,
    ScheduleTriggerTickReport,
)

DEFAULT_SCHEDULER_CAPABILITY_ID = (
    "qwenpaw.system.tasks.local-durable-scheduler"
)


class ScheduleDispatchDisposition(str, Enum):
    """Outcome of one trigger admission attempt."""

    STARTED = "started"
    RECOVERED = "recovered"
    OWNED_ELSEWHERE = "owned_elsewhere"
    REPLAYED = "replayed"


class ServiceScheduleDispatchDisposition(str, Enum):
    """Outcome of one service callback occurrence admission."""

    EXECUTED = "executed"
    REPLAYED = "replayed"
    OWNED_ELSEWHERE = "owned_elsewhere"


@dataclass(frozen=True, slots=True)
class ServiceScheduleDispatchResult:
    """Lease evidence for one service callback occurrence."""

    lease: ScheduleLease
    disposition: ServiceScheduleDispatchDisposition


@dataclass(frozen=True, slots=True)
class DeliveryScheduleDispatchResult:
    """Schedule admission plus its durable Delivery attempt."""

    lease: ScheduleLease
    delivery: DeliveryDispatchResult | None
    disposition: ScheduleDispatchDisposition


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
        scheduler_host: SchedulerHost | None = None,
        lease_seconds: float = 30.0,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("schedule lease duration must be positive")
        self._capability_resolver = capability_resolver
        self._task_application = task_application
        self._task_orchestrator = task_orchestrator
        self._ledger_workspace_dir = Path(ledger_workspace_dir)
        self._scheduler_capability_id = scheduler_capability_id
        self._scheduler_host = scheduler_host
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
        if definition.work_kind is not ScheduleWorkKind.TASK:
            raise ValueError(
                "ScheduledTaskDispatcher accepts task schedules only",
            )
        generation_lease = await self._capability_resolver.pin()
        claimed: ScheduleLease | None = None
        task: Task | None = None
        try:
            scheduler = await self._resolve_scheduler(generation_lease)
            fire = ScheduleFire(
                agent_id=definition.agent_id,
                schedule_id=definition.schedule_id,
                registry_generation=generation_lease.generation,
                scheduled_for=scheduled_for,
                idempotency_key=idempotency_key,
            )
            await scheduler.upsert(definition)
            lease = await scheduler.claim(
                fire,
                owner_id=owner_id,
                lease_seconds=self._lease_seconds,
            )
            claimed = lease
            if lease.status is not ScheduleLeaseStatus.CLAIMED:
                return await self._replay_bound_task(
                    definition,
                    lease,
                )
            if lease.owner_id != owner_id:
                return ScheduleDispatchResult(
                    lease=lease,
                    disposition=(ScheduleDispatchDisposition.OWNED_ELSEWHERE),
                )
            task = await self._create_task(definition, fire)
            completed = await scheduler.complete(
                lease.lease_id,
                owner_id=owner_id,
                expected_revision=lease.revision,
                task_id=task.task_id,
            )
            return await self._start_bound_task(
                definition,
                completed,
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

    async def upsert_definition(
        self,
        definition: ScheduleDefinition,
    ) -> ScheduleDefinition:
        """Persist one definition through the generation-pinned Scheduler."""
        generation_lease = await self._capability_resolver.pin()
        try:
            scheduler = await self._resolve_scheduler(generation_lease)
            return await scheduler.upsert(definition)
        finally:
            await generation_lease.close()

    async def remove_definition(
        self,
        *,
        agent_id: str,
        schedule_id: str,
    ) -> bool:
        """Remove one definition without deleting immutable fire history."""
        generation_lease = await self._capability_resolver.pin()
        try:
            scheduler = await self._resolve_scheduler(generation_lease)
            return await scheduler.remove(
                agent_id=agent_id,
                schedule_id=schedule_id,
            )
        finally:
            await generation_lease.close()

    async def list_definitions(
        self,
        *,
        agent_id: str,
    ) -> tuple[ScheduleDefinition, ...]:
        """Read one Agent catalog through the pinned Scheduler Provider."""
        generation_lease = await self._capability_resolver.pin()
        try:
            scheduler = await self._resolve_scheduler(generation_lease)
            return await scheduler.list_definitions(agent_id=agent_id)
        finally:
            await generation_lease.close()

    async def run_due_triggers(
        self,
        *,
        agent_id: str,
        now: datetime,
        cursors: ScheduleTriggerCursorStore,
        handle: ScheduleOccurrenceHandler,
        batch_size: int = 100,
    ) -> ScheduleTriggerTickReport:
        """Consume due cursors through one generation-pinned Scheduler."""
        generation_lease = await self._capability_resolver.pin()
        try:
            scheduler = await self._resolve_scheduler(generation_lease)
            return await DurableScheduleTriggerWorker(
                catalog=scheduler,
                cursors=cursors,
                batch_size=batch_size,
            ).tick(
                agent_id=agent_id,
                now=now,
                handle=handle,
            )
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

    async def _resolve_scheduler(self, lease) -> SchedulerPort:
        return await _resolve_scheduler(
            lease,
            capability_id=self._scheduler_capability_id,
            scheduler_host=self._scheduler_host,
        )


class ScheduledServiceCallbackDispatcher:
    """Fence one process-local service callback with a durable Fire lease."""

    def __init__(
        self,
        *,
        capability_resolver: CapabilityResolver,
        scheduler_capability_id: str = DEFAULT_SCHEDULER_CAPABILITY_ID,
        scheduler_host: SchedulerHost | None = None,
        lease_seconds: float = 30.0,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("schedule lease duration must be positive")
        self._capability_resolver = capability_resolver
        self._scheduler_capability_id = scheduler_capability_id
        self._scheduler_host = scheduler_host
        self._lease_seconds = lease_seconds

    async def dispatch(
        self,
        definition: ScheduleDefinition,
        *,
        scheduled_for: datetime,
        idempotency_key: str,
        owner_id: str,
        callback: Callable[[], Awaitable[None]],
    ) -> ServiceScheduleDispatchResult:
        """Execute at most once; expired uncertain work is not replayed."""
        if definition.work_kind is not ScheduleWorkKind.SERVICE:
            raise ValueError(
                "ScheduledServiceCallbackDispatcher accepts service "
                "schedules only",
            )
        generation_lease = await self._capability_resolver.pin()
        try:
            scheduler = await _resolve_scheduler(
                generation_lease,
                capability_id=self._scheduler_capability_id,
                scheduler_host=self._scheduler_host,
            )
            await scheduler.upsert(definition)
            await scheduler.recover_expired(
                agent_id=definition.agent_id,
                now=datetime.now(scheduled_for.tzinfo),
                schedule_id=definition.schedule_id,
            )
            fire = ScheduleFire(
                agent_id=definition.agent_id,
                schedule_id=definition.schedule_id,
                registry_generation=generation_lease.generation,
                scheduled_for=scheduled_for,
                idempotency_key=idempotency_key,
            )
            lease = await scheduler.claim(
                fire,
                owner_id=owner_id,
                lease_seconds=self._lease_seconds,
            )
            if lease.status is not ScheduleLeaseStatus.CLAIMED:
                return ServiceScheduleDispatchResult(
                    lease=lease,
                    disposition=ServiceScheduleDispatchDisposition.REPLAYED,
                )
            if lease.owner_id != owner_id:
                return ServiceScheduleDispatchResult(
                    lease=lease,
                    disposition=(
                        ServiceScheduleDispatchDisposition.OWNED_ELSEWHERE
                    ),
                )
            current = lease

            async def invoke_callback() -> None:
                await callback()

            callback_task = asyncio.create_task(invoke_callback())
            try:
                while True:
                    done, _pending = await asyncio.wait(
                        {callback_task},
                        timeout=self._lease_seconds / 3,
                    )
                    if done:
                        await callback_task
                        break
                    current = await scheduler.renew(
                        current.lease_id,
                        owner_id=owner_id,
                        expected_revision=current.revision,
                        lease_seconds=self._lease_seconds,
                    )
            except asyncio.CancelledError:
                callback_task.cancel()
                try:
                    await callback_task
                except asyncio.CancelledError:
                    pass
                raise
            except Exception as error:
                try:
                    await scheduler.fail(
                        current.lease_id,
                        owner_id=owner_id,
                        expected_revision=current.revision,
                        error_code=type(error).__name__,
                    )
                except Exception as accounting_error:
                    raise ScheduleDispatchAccountingError(
                        "service schedule callback failure was not "
                        "persisted",
                    ) from accounting_error
                raise
            completed = await scheduler.complete(
                current.lease_id,
                owner_id=owner_id,
                expected_revision=current.revision,
                completion_ref=f"service:{definition.schedule_id}",
            )
            return ServiceScheduleDispatchResult(
                lease=completed,
                disposition=ServiceScheduleDispatchDisposition.EXECUTED,
            )
        finally:
            await generation_lease.close()


class ScheduledDeliveryDispatcher:
    """Materialize one stable Delivery from a durable Schedule Fire."""

    def __init__(
        self,
        *,
        capability_resolver: CapabilityResolver,
        delivery_projection: DeliveryProjectionPort,
        scheduler_capability_id: str = DEFAULT_SCHEDULER_CAPABILITY_ID,
        scheduler_host: SchedulerHost | None = None,
        lease_seconds: float = 30.0,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("schedule lease duration must be positive")
        self._capability_resolver = capability_resolver
        self._delivery_projection = delivery_projection
        self._scheduler_capability_id = scheduler_capability_id
        self._scheduler_host = scheduler_host
        self._lease_seconds = lease_seconds

    async def dispatch(
        self,
        definition: ScheduleDefinition,
        *,
        scheduled_for: datetime,
        idempotency_key: str,
        owner_id: str,
        request_factory: Callable[[ScheduleFire], DeliveryRequest],
    ) -> DeliveryScheduleDispatchResult:
        """Bind the Fire before executing its independently durable send."""
        if definition.work_kind is not ScheduleWorkKind.DELIVERY:
            raise ValueError(
                "ScheduledDeliveryDispatcher accepts delivery schedules only",
            )
        generation_lease = await self._capability_resolver.pin()
        claimed: ScheduleLease | None = None
        try:
            scheduler = await _resolve_scheduler(
                generation_lease,
                capability_id=self._scheduler_capability_id,
                scheduler_host=self._scheduler_host,
            )
            fire = ScheduleFire(
                agent_id=definition.agent_id,
                schedule_id=definition.schedule_id,
                registry_generation=generation_lease.generation,
                scheduled_for=scheduled_for,
                idempotency_key=idempotency_key,
            )
            await scheduler.upsert(definition)
            lease = await scheduler.claim(
                fire,
                owner_id=owner_id,
                lease_seconds=self._lease_seconds,
            )
            claimed = lease
            if lease.status is ScheduleLeaseStatus.CLAIMED:
                if lease.owner_id != owner_id:
                    return DeliveryScheduleDispatchResult(
                        lease=lease,
                        delivery=None,
                        disposition=(
                            ScheduleDispatchDisposition.OWNED_ELSEWHERE
                        ),
                    )
                request = request_factory(lease.fire)
                delivery_id = request.delivery_id
                if delivery_id is None:  # pragma: no cover - validator
                    raise ValueError("delivery request has no stable identity")
                lease = await scheduler.complete(
                    lease.lease_id,
                    owner_id=owner_id,
                    expected_revision=lease.revision,
                    completion_ref=f"delivery:{delivery_id}",
                )
                claimed = lease
                disposition = ScheduleDispatchDisposition.STARTED
            else:
                if lease.completion_ref is None:
                    return DeliveryScheduleDispatchResult(
                        lease=lease,
                        delivery=None,
                        disposition=ScheduleDispatchDisposition.REPLAYED,
                    )
                request = request_factory(lease.fire)
                expected_ref = f"delivery:{request.delivery_id}"
                if lease.completion_ref != expected_ref:
                    raise ValueError(
                        "schedule completion does not match Delivery identity",
                    )
                disposition = ScheduleDispatchDisposition.REPLAYED
            await self._delivery_projection.recover_expired(
                agent_id=request.agent_id,
                now=datetime.now(scheduled_for.tzinfo),
            )
            delivery = await DeliveryDispatcher(
                projection=self._delivery_projection,
                capability_resolver=self._capability_resolver,
                lease_seconds=self._lease_seconds,
            ).dispatch(
                request,
                attempt=1,
                owner_id=owner_id,
            )
            if (
                delivery.disposition
                is DeliveryDispatchDisposition.OWNED_ELSEWHERE
            ):
                disposition = ScheduleDispatchDisposition.OWNED_ELSEWHERE
            return DeliveryScheduleDispatchResult(
                lease=lease,
                delivery=delivery,
                disposition=disposition,
            )
        except Exception as error:
            if (
                claimed is not None
                and claimed.status is ScheduleLeaseStatus.CLAIMED
                and claimed.owner_id == owner_id
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
                        "delivery schedule materialization failure was not "
                        "persisted",
                    ) from accounting_error
            raise
        finally:
            await generation_lease.close()


async def _resolve_scheduler(
    lease,
    *,
    capability_id: str,
    scheduler_host: SchedulerHost | None,
) -> SchedulerPort:
    """Resolve one generation-pinned Scheduler implementation."""
    descriptor = lease.resolve(capability_id)
    if descriptor is None:
        raise SchedulerCapabilityUnavailableError(
            f"scheduler capability '{capability_id}' is absent",
        )
    if descriptor.slot not in {"scheduler", "scheduler.provider"}:
        raise SchedulerCapabilityUnavailableError(
            f"capability '{capability_id}' declares "
            f"slot '{descriptor.slot}'",
        )
    if not isinstance(lease, ExecutableCapabilityLease):
        raise SchedulerCapabilityUnavailableError(
            "scheduler resolver does not expose implementations",
        )
    implementation = lease.implementation(capability_id)
    if descriptor.slot == "scheduler.provider":
        if not isinstance(implementation, SchedulerProvider):
            raise SchedulerCapabilityUnavailableError(
                f"capability '{capability_id}' does not "
                "implement SchedulerProvider",
            )
        if scheduler_host is None:
            raise SchedulerCapabilityUnavailableError(
                "scheduler provider requires a Host-owned Store",
            )
        implementation = await implementation.open(scheduler_host)
    if not isinstance(implementation, SchedulerPort):
        raise SchedulerCapabilityUnavailableError(
            f"capability '{capability_id}' does not implement SchedulerPort",
        )
    return implementation
