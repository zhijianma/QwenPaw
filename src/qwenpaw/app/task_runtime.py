# -*- coding: utf-8 -*-
"""Application-owned composition root for the shared Task runtime."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import Any
from weakref import ReferenceType, WeakKeyDictionary, ref

from fastapi import Request

from ..capabilities import GenerationRegistry
from ..constant import DEFAULT_STREAM_TASK_TIMEOUT_SECONDS
from ..editions import (
    EditionRuntimeBindings,
    build_deployment_adapter,
    resolve_edition,
)
from ..kernel.models import (
    ApprovalLevel,
    ModelSelection,
    RunnerSignal,
    RuntimeLaunchConfig,
    Task,
    TaskOrder,
)
from ..runtime.assembly import RuntimeAssemblyFactory
from ..schemas import TextContent
from ..tasks.application import (
    TaskApplicationService,
    TaskExecutionApplicationService,
)
from ..tasks.artifact_application import TaskArtifactApplicationService
from ..tasks.approval_application import TaskApprovalApplicationService
from ..tasks.capability_application import (
    TaskCapabilityApplicationService,
)
from ..tasks.event_application import TaskEventApplicationService
from ..tasks.runtime import TaskRuntimeOrchestrator, TaskRuntimeSupervisor
from ..tasks.sensor_application import TaskSensorApplicationService
from ..tasks.sensors import SensorContributionHost
from ..tasks.service import TaskService
from ..tasks.side_effect_application import (
    TaskSideEffectApplicationService,
)
from ..tasks.system_contributions import (
    SYSTEM_CAPABILITY_BUNDLE,
    SYSTEM_CONSOLE_RUNNER_ID,
    SYSTEM_DEFAULT_STRATEGY_ID,
    system_contribution_factory,
)
from .agent_context import get_agent_for_request
from .approvals.task_bridge import LiveRuntimeApprovalBridge
from .task_capability_compatibility import (
    SQLiteTaskCapabilityCompatibilityStore,
    TaskCapabilityAlias,
    canonical_task_capability_alias,
)

MAX_ARTIFACT_PREVIEW_BYTES = 256 * 1024


def canonical_task_capability_id(
    capability_id: str,
    *,
    on_legacy_hit: Callable[[TaskCapabilityAlias], None] | None = None,
) -> str:
    """Map pre-namespace Task IDs without changing stored records."""
    alias = canonical_task_capability_alias(capability_id)
    if alias is None:
        return capability_id
    if on_legacy_hit is not None:
        on_legacy_hit(alias)
    return alias.canonical_id


@dataclass(frozen=True, slots=True)
class TaskApplicationBindings:
    """One Workspace's Task application entrypoints and shared ports."""

    workspace: Any
    runtime: EditionRuntimeBindings
    tasks: TaskApplicationService
    orchestrator: TaskRuntimeOrchestrator
    execution: TaskExecutionApplicationService
    artifacts: TaskArtifactApplicationService
    approvals: TaskApprovalApplicationService
    capabilities: TaskCapabilityApplicationService
    events: TaskEventApplicationService
    sensors: TaskSensorApplicationService
    side_effects: TaskSideEffectApplicationService
    capability_compatibility: SQLiteTaskCapabilityCompatibilityStore


class ApprovedProposalDispatcher:
    """Own approved Proposal executions outside transport lifetimes."""

    def __init__(self) -> None:
        self._executions: set[asyncio.Task[None]] = set()

    def schedule(
        self,
        workspace: Any,
        service: TaskService,
        order: TaskOrder,
    ) -> None:
        """Keep an approved Proposal alive until completion."""
        execution = asyncio.create_task(
            self.execute(workspace, service, order),
        )
        self._executions.add(execution)
        execution.add_done_callback(self._executions.discard)

    async def execute(
        self,
        workspace: Any,
        service: TaskService,
        order: TaskOrder,
    ) -> None:
        """Execute an approved Proposal through the guarded runtime."""
        try:
            console_channel = await workspace.channel_manager.get_channel(
                "console",
            )
            if console_channel is None:
                raise RuntimeError("Console channel is unavailable")
            task = await service.get_task(order.task_id)
            if task is None or task.active_run_id is None:
                raise RuntimeError("Approved task has no active run")
            await service.record_runner_signal(
                order.task_id,
                task.active_run_id,
                RunnerSignal(
                    event_type="runner.dispatched",
                    payload={"runtime": "console-agent"},
                ),
            )
            payload = {
                "channel_id": "console",
                "sender_id": "qwenpaw-proactive",
                "content_parts": [TextContent(text=order.objective)],
                "message_metadata": {
                    "task_id": str(order.task_id),
                    "approval_ids": [
                        str(approval_id) for approval_id in order.approval_ids
                    ],
                },
                "meta": {
                    "session_id": f"task-{order.task_id}",
                    "user_id": "qwenpaw-proactive",
                    "request_context": {
                        "task_id": str(order.task_id),
                        "approved_proposal": True,
                        "durable_task": True,
                        "agent_id": task.agent_id,
                        "task_ledger_workspace_dir": str(
                            workspace.workspace_dir,
                        ),
                    },
                },
            }
            async for _ in console_channel.stream_one(payload):
                pass
            await service.complete_task(order.task_id)
        except Exception as exc:  # pragma: no cover - runtime boundary
            await service.fail_task(
                order.task_id,
                error_summary=type(exc).__name__,
            )


class TaskApplicationHost:
    """Compose all Task use cases over one shared capability registry."""

    def __init__(
        self,
        registry: GenerationRegistry,
        *,
        supervisor: TaskRuntimeSupervisor | None = None,
    ) -> None:
        self.registry = registry
        self.supervisor = supervisor or TaskRuntimeSupervisor()
        self._workspaces: dict[str, Any] = {}
        self._bindings: dict[str, TaskApplicationBindings] = {}
        self._compatibility_stores: dict[
            str,
            SQLiteTaskCapabilityCompatibilityStore,
        ] = {}
        self._proposals = ApprovedProposalDispatcher()

    async def compose(self, workspace: Any) -> TaskApplicationBindings:
        """Build one coherent set of Workspace-scoped use cases."""
        self._workspaces[workspace.agent_id] = workspace
        await RuntimeAssemblyFactory(self.registry).prepare()
        await self.registry.ensure_bundle(
            SYSTEM_CAPABILITY_BUNDLE,
            system_contribution_factory(self._resolve_workspace),
        )
        profile = resolve_edition()
        cached = self._bindings.get(workspace.agent_id)
        if (
            cached is not None
            and cached.workspace is workspace
            and cached.runtime.profile == profile
        ):
            cached.runtime.task_service.set_registry_generation(
                self.registry.generation,
            )
            return cached
        runtime = build_deployment_adapter(
            profile,
            self.registry,
        ).compose(workspace)
        tasks = TaskApplicationService(
            runtime.task_service,
            agent_id=workspace.agent_id,
            default_project_dir=Path(workspace.workspace_dir),
        )
        compatibility_store = self._compatibility_stores.get(
            workspace.agent_id,
        )
        if compatibility_store is None:
            compatibility_store = SQLiteTaskCapabilityCompatibilityStore(
                Path(workspace.workspace_dir)
                / ".qwenpaw"
                / "lite"
                / "tasks.db",
            )
            self._compatibility_stores[
                workspace.agent_id
            ] = compatibility_store
        await compatibility_store.start_observation(
            agent_id=workspace.agent_id,
        )

        def normalize_capability_id(capability_id: str) -> str:
            return canonical_task_capability_id(
                capability_id,
                on_legacy_hit=lambda alias: compatibility_store.record(
                    agent_id=workspace.agent_id,
                    alias=alias,
                ),
            )

        orchestrator = TaskRuntimeOrchestrator(
            runtime.task_service,
            runtime.capability_resolver,
            self.supervisor,
        )
        execution = TaskExecutionApplicationService(
            runtime.task_service,
            orchestrator,
            runtime_config_factory=lambda task: self._runtime_config(
                task,
                workspace,
            ),
            default_runner_id=SYSTEM_CONSOLE_RUNNER_ID,
            runner_id_normalizer=normalize_capability_id,
            timeout_seconds=DEFAULT_STREAM_TASK_TIMEOUT_SECONDS,
        )
        approvals = TaskApprovalApplicationService(
            runtime.task_service,
            LiveRuntimeApprovalBridge(),
            lambda order: self._proposals.schedule(
                workspace,
                runtime.task_service,
                order,
            ),
        )
        bindings = TaskApplicationBindings(
            workspace=workspace,
            runtime=runtime,
            tasks=tasks,
            orchestrator=orchestrator,
            execution=execution,
            artifacts=TaskArtifactApplicationService(
                tasks,
                runtime.capability_resolver,
                artifact_store_factory=runtime.artifact_store,
                legacy_workspace_dir=Path(workspace.workspace_dir),
                max_preview_bytes=MAX_ARTIFACT_PREVIEW_BYTES,
            ),
            approvals=approvals,
            capabilities=TaskCapabilityApplicationService(
                runtime.capability_resolver,
            ),
            events=TaskEventApplicationService(runtime.task_service),
            sensors=TaskSensorApplicationService(
                SensorContributionHost(
                    runtime.task_service,
                    runtime.capability_resolver,
                ),
                agent_id=workspace.agent_id,
            ),
            side_effects=TaskSideEffectApplicationService(
                runtime.task_service,
            ),
            capability_compatibility=compatibility_store,
        )
        self._bindings[workspace.agent_id] = bindings
        return bindings

    async def _resolve_workspace(self, agent_id: str) -> Any:
        workspace = self._workspaces.get(agent_id)
        if workspace is None:
            raise LookupError(agent_id)
        return workspace

    def _runtime_config(
        self,
        task: Task,
        workspace: Any,
    ) -> RuntimeLaunchConfig:
        approval_level = task.metadata.get("approval_level")
        raw_model_selection = task.metadata.get("model_selection")
        return RuntimeLaunchConfig(
            agent_id=task.agent_id,
            conversation_id=(
                task.metadata.get("conversation_id")
                if isinstance(task.metadata.get("conversation_id"), str)
                else None
            ),
            project_dir=str(task.metadata["workspace_dir"]),
            ledger_workspace_dir=str(workspace.workspace_dir),
            approval_level=(
                ApprovalLevel(approval_level)
                if isinstance(approval_level, str)
                else ApprovalLevel.AGENT_PROFILE
            ),
            model_selection=(
                ModelSelection.model_validate(raw_model_selection)
                if isinstance(raw_model_selection, dict)
                else None
            ),
            strategy_id=(
                canonical_task_capability_id(
                    task.metadata["strategy_id"],
                    on_legacy_hit=lambda alias: self._compatibility_stores[
                        workspace.agent_id
                    ].record(
                        agent_id=workspace.agent_id,
                        alias=alias,
                    ),
                )
                if isinstance(task.metadata.get("strategy_id"), str)
                else SYSTEM_DEFAULT_STRATEGY_ID
            ),
        )


_HOSTS_BY_REGISTRY: WeakKeyDictionary[
    GenerationRegistry,
    ReferenceType[TaskApplicationHost],
] = WeakKeyDictionary()


def task_application_host(
    registry: GenerationRegistry,
    *,
    supervisor: TaskRuntimeSupervisor | None = None,
) -> TaskApplicationHost:
    """Return the process-local Host that owns one capability registry."""
    host_ref = _HOSTS_BY_REGISTRY.get(registry)
    host = host_ref() if host_ref is not None else None
    if host is None:
        host = TaskApplicationHost(registry, supervisor=supervisor)
        _HOSTS_BY_REGISTRY[registry] = ref(host)
    return host


async def task_application_bindings(
    request: Request,
) -> TaskApplicationBindings:
    """Resolve and cache one request's coherent Task application bindings."""
    cached = getattr(request.state, "qwenpaw_task_applications", None)
    if isinstance(cached, TaskApplicationBindings):
        return cached

    workspace = await get_agent_for_request(request)
    loader = getattr(request.app.state, "plugin_loader", None)
    registry = getattr(loader, "capability_registry", None)
    if registry is None:
        registry = getattr(workspace, "capability_registry", None)
    if registry is None:
        registry = getattr(
            request.app.state,
            "lite_capability_registry",
            None,
        )
    if registry is None:
        registry = GenerationRegistry()
        request.app.state.lite_capability_registry = registry

    host = getattr(request.app.state, "task_application_host", None)
    if (
        not isinstance(host, TaskApplicationHost)
        or host.registry is not registry
    ):
        supervisor = getattr(
            request.app.state,
            "lite_task_runtime_supervisor",
            None,
        )
        host = task_application_host(registry, supervisor=supervisor)
        request.app.state.task_application_host = host
        request.app.state.lite_task_runtime_supervisor = host.supervisor

    bindings = await host.compose(workspace)
    request.state.qwenpaw_task_applications = bindings
    return bindings


__all__ = [
    "ApprovedProposalDispatcher",
    "TaskApplicationBindings",
    "TaskApplicationHost",
    "canonical_task_capability_id",
    "task_application_bindings",
    "task_application_host",
]
