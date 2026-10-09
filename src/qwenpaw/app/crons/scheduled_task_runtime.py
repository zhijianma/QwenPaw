# -*- coding: utf-8 -*-
"""Shared Lite execution path for Schedule-backed Tasks."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from ...delivery import (
    DeliveryDispatchDisposition,
    DeliveryDispatcher,
    SQLiteDeliveryProjectionStore,
    TaskDeliveryProjector,
    TaskDeliveryWorker,
)
from ...inbox import SQLiteInboxProjectionStore
from ...kernel import (
    DeliveryKind,
    DeliveryMode,
    DeliveryPolicy,
    ScheduleDefinition,
    TaskStatus,
)
from ...scheduling import ScheduleDispatchResult, ScheduledTaskDispatcher
from ...scheduling import SQLiteSchedulerStore, SchedulerStoreHost
from ...tasks.service import TaskService
from ..task_runtime import (
    TaskApplicationBindings,
    TaskApplicationHost,
    task_application_host,
)


class LiteScheduledTaskRuntime:  # pylint: disable=too-few-public-methods
    """Execute one Kernel Schedule through Task, Delivery, and Inbox."""

    def __init__(
        self,
        workspace: Any,
        host: TaskApplicationHost | None = None,
    ) -> None:
        self._workspace = workspace
        self._host = host or task_application_host(
            workspace.capability_registry,
        )

    async def execute(
        self,
        definition: ScheduleDefinition,
        *,
        scheduled_for: datetime,
        idempotency_key: str,
        timeout_seconds: int,
        owner_prefix: str,
    ) -> dict[str, Any]:
        """Wait for terminal Task facts and durable Delivery receipts."""
        bindings, dispatcher = await self._dependencies()
        service = bindings.runtime.task_service
        result = await dispatcher.dispatch(
            definition,
            scheduled_for=scheduled_for,
            idempotency_key=idempotency_key,
            owner_id=(f"{owner_prefix}:{self._workspace.agent_id}:{uuid4()}"),
        )
        task_id = result.lease.task_id
        if task_id is None:
            raise RuntimeError("Schedule Fire has no Task binding")
        policy = DeliveryPolicy.model_validate(
            definition.metadata.get("delivery_policy"),
        )
        deliveries = await asyncio.wait_for(
            self._worker(bindings).follow(
                task_id,
                policy,
                owner_id=(
                    f"{owner_prefix}-delivery:" f"{self._workspace.agent_id}"
                ),
            ),
            timeout=timeout_seconds + 5,
        )
        return await self._summarize(
            service,
            definition,
            result,
            policy,
            deliveries,
        )

    async def upsert_definition(
        self,
        definition: ScheduleDefinition,
    ) -> ScheduleDefinition:
        """Synchronize one definition without creating a Task fire."""
        _bindings, dispatcher = await self._dependencies()
        return await dispatcher.upsert_definition(definition)

    async def remove_definition(
        self,
        *,
        agent_id: str,
        schedule_id: str,
    ) -> bool:
        """Remove one catalog definition while preserving fire history."""
        _bindings, dispatcher = await self._dependencies()
        return await dispatcher.remove_definition(
            agent_id=agent_id,
            schedule_id=schedule_id,
        )

    async def _dependencies(self):
        from ...constant import WORKING_DIR

        bindings = await self._host.compose(self._workspace)
        dispatcher = ScheduledTaskDispatcher(
            capability_resolver=bindings.runtime.capability_resolver,
            task_application=bindings.tasks,
            task_orchestrator=bindings.orchestrator,
            ledger_workspace_dir=Path(self._workspace.workspace_dir),
            scheduler_host=SchedulerStoreHost(
                SQLiteSchedulerStore(WORKING_DIR / "scheduler.db"),
            ),
        )
        return bindings, dispatcher

    def _worker(
        self,
        bindings: TaskApplicationBindings,
    ) -> TaskDeliveryWorker:
        service = bindings.runtime.task_service
        return TaskDeliveryWorker(
            events=bindings.events,
            projector=TaskDeliveryProjector(service),
            dispatcher=DeliveryDispatcher(
                projection=SQLiteDeliveryProjectionStore(
                    self._data_path("delivery.db"),
                ),
                capability_resolver=bindings.runtime.capability_resolver,
            ),
            inbox=SQLiteInboxProjectionStore(
                self._data_path("inbox.db"),
            ),
        )

    async def _summarize(
        self,
        service: TaskService,
        definition: ScheduleDefinition,
        result: ScheduleDispatchResult,
        policy: DeliveryPolicy,
        deliveries: tuple[Any, ...],
    ) -> dict[str, Any]:
        task_id = result.lease.task_id
        if task_id is None:  # pragma: no cover - checked by execute
            raise RuntimeError("Schedule Fire has no Task binding")
        task = await service.get_task(task_id)
        if task is None or task.status is not TaskStatus.COMPLETED:
            status = task.status.value if task is not None else "missing"
            raise RuntimeError(f"Scheduled Task ended with status: {status}")
        failed = any(
            item.disposition
            in {
                DeliveryDispatchDisposition.FAILED,
                DeliveryDispatchDisposition.UNCERTAIN,
            }
            for item in deliveries
        )
        runs = await service.list_runs(task_id)
        run_id = (
            result.run.run_id if result.run is not None else runs[-1].run_id
        )
        return {
            "task_id": str(task_id),
            "run_id": str(run_id),
            "conversation_id": definition.conversation_id,
            "delivery_status": self._delivery_status(
                policy,
                deliveries=deliveries,
                failed=failed,
            ),
            "delivery_error": (
                "delivery projection failed or is uncertain"
                if failed
                else None
            ),
        }

    def _data_path(self, filename: str) -> Path:
        return (
            Path(self._workspace.workspace_dir)
            / ".qwenpaw"
            / "lite"
            / filename
        )

    @staticmethod
    def _delivery_status(
        policy: DeliveryPolicy,
        *,
        deliveries: tuple[Any, ...],
        failed: bool,
    ) -> str:
        if failed:
            return "failed"
        if policy.mode is DeliveryMode.SILENT:
            return "suppressed"
        if not deliveries:
            if DeliveryKind.RESULT in policy.kinds:
                return "suppressed"
            return "not_requested"
        return "success"


__all__ = ["LiteScheduledTaskRuntime"]
