# -*- coding: utf-8 -*-
"""Typed task runtime context and legacy request-context translation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..kernel.models import (
    ApprovalLevel,
    ApprovalBroker,
    CancellationToken,
    CheckpointBroker,
    ExecutionCheckpoint,
    Run,
    RuntimeContext,
    RuntimeLaunchConfig,
    RuntimeStrategyDirective,
    TaskOrder,
    UsageMeter,
)
from .artifacts import StoredArtifactEmitter, lite_artifact_store
from .approval_broker import TaskApprovalBroker
from .cancellation import RuntimeCancellationToken
from .checkpoints import TaskCheckpointBroker
from .ledger import SQLiteExecutionLedger
from .service import TaskService
from .side_effects import TaskSideEffectBroker
from .usage_scope import (
    RECORD_MODEL_USAGE_CONTEXT_KEY,
    USAGE_SCOPE_CONTEXT_KEY,
    USAGE_SCOPE_LEASE_CONTEXT_KEY,
    lite_usage_scope_registry,
)

_LAUNCH_CONFIG_KEY = "runtime_launch_config"


def order_metadata_from_launch_config(
    config: RuntimeLaunchConfig,
    *,
    resume_checkpoint: ExecutionCheckpoint | None = None,
) -> dict[str, Any]:
    """Encode typed launch data for legacy TaskOrder compatibility."""
    return {
        _LAUNCH_CONFIG_KEY: config.model_dump(mode="json"),
        **(
            {"resume_checkpoint": resume_checkpoint.model_dump(mode="json")}
            if resume_checkpoint is not None
            else {}
        ),
    }


def runtime_context_from_order(
    order: TaskOrder,
    run: Run,
    *,
    approval_broker: ApprovalBroker | None = None,
    checkpoint_broker: CheckpointBroker | None = None,
    cancellation: CancellationToken | None = None,
    usage_meter: UsageMeter | None = None,
    strategy_id: str | None = None,
) -> RuntimeContext:
    """Build the immutable context used by a contextual runner."""
    raw_config = order.metadata.get(_LAUNCH_CONFIG_KEY)
    if not isinstance(raw_config, dict):
        raw_config = {
            "agent_id": order.metadata.get("agent_id", "default"),
            "project_dir": order.metadata.get(
                "project_dir",
                order.metadata.get("workspace_dir", "."),
            ),
            "ledger_workspace_dir": order.metadata.get(
                "task_ledger_workspace_dir",
                ".",
            ),
            "approval_level": order.metadata.get(
                "approval_level",
                ApprovalLevel.AGENT_PROFILE.value,
            ),
            "strategy_id": order.metadata.get(
                "strategy_id",
                "qwenpaw.system.tasks.default-strategy",
            ),
        }
    config = RuntimeLaunchConfig.model_validate(raw_config)
    raw_checkpoint = order.metadata.get("resume_checkpoint")
    checkpoint = (
        ExecutionCheckpoint.model_validate(raw_checkpoint)
        if isinstance(raw_checkpoint, dict)
        else None
    )
    service = TaskService(
        store=SQLiteExecutionLedger(
            Path(config.ledger_workspace_dir)
            / ".qwenpaw"
            / "lite"
            / "tasks.db",
        ),
        registry_generation=run.registry_generation,
    )
    if approval_broker is None:
        approval_broker = TaskApprovalBroker(
            service=service,
            task_id=order.task_id,
            run_id=run.run_id,
        )
    if cancellation is None:
        cancellation = RuntimeCancellationToken()
    if checkpoint_broker is None:
        checkpoint_broker = TaskCheckpointBroker(
            service=service,
            task_id=order.task_id,
            run_id=run.run_id,
        )
    invocation_id = run.invocation_id or run.correlation_id or run.run_id
    correlation_id = run.correlation_id or invocation_id
    return RuntimeContext(
        task_id=order.task_id,
        run_id=run.run_id,
        invocation_id=invocation_id,
        correlation_id=correlation_id,
        agent_id=config.agent_id,
        conversation_id=config.conversation_id,
        session_id=f"task-{order.task_id}",
        project_dir=config.project_dir,
        ledger_workspace_dir=config.ledger_workspace_dir,
        registry_generation=run.registry_generation,
        approval_level=config.approval_level,
        model_selection=config.model_selection,
        execution_contract=order.execution_contract,
        capability_ids=(
            (run.runner_id, strategy_id)
            if strategy_id is not None
            else (run.runner_id,)
        ),
        strategy=(
            RuntimeStrategyDirective(strategy_id=strategy_id)
            if strategy_id is not None
            else None
        ),
        resume_checkpoint=checkpoint,
        artifact_emitter=StoredArtifactEmitter(
            lite_artifact_store(Path(config.project_dir)),
            producer=run.runner_id,
        ),
        checkpoint_broker=checkpoint_broker,
        approval_broker=approval_broker,
        side_effect_broker=TaskSideEffectBroker(
            service=service,
            task_id=order.task_id,
            run_id=run.run_id,
        ),
        usage_meter=usage_meter,
        cancellation=cancellation,
    )


def legacy_request_context(
    context: RuntimeContext,
    *,
    share_usage_scope: bool = True,
) -> dict[str, Any]:
    """Translate the stable context for the existing Console runtime."""
    request_context: dict[str, Any] = {
        "task_id": str(context.task_id),
        "durable_task": True,
        "agent_id": context.agent_id,
        "task_ledger_workspace_dir": context.ledger_workspace_dir,
        "os_registry_generation": context.registry_generation,
        "os_invocation_id": str(context.invocation_id),
        "os_correlation_id": str(context.correlation_id),
        "execution_contract": (
            context.execution_contract.model_dump(mode="json")
            if context.execution_contract is not None
            else None
        ),
        "_task_approval_broker": context.approval_broker,
        "_task_checkpoint_broker": context.checkpoint_broker,
        "_task_side_effect_broker": context.side_effect_broker,
        "_task_usage_meter": context.usage_meter,
        "_task_cancellation": context.cancellation,
        "session_project_dirs": [
            {"path": context.project_dir, "label": None},
        ],
    }
    if context.usage_meter is not None and share_usage_scope:
        usage_scope = lite_usage_scope_registry.open_root(
            context.usage_meter,
            agent_id=context.agent_id,
        )
        request_context[USAGE_SCOPE_CONTEXT_KEY] = usage_scope.scope_id
        request_context[USAGE_SCOPE_LEASE_CONTEXT_KEY] = usage_scope
        # The Console runner converts root response usage into a persisted
        # signal. Descendants have no such adapter, so they account directly.
        request_context[RECORD_MODEL_USAGE_CONTEXT_KEY] = False
    contract = context.execution_contract
    if contract is not None:
        iteration_limits = tuple(
            condition.parameters["limit"]
            for condition in contract.exit_conditions
            if condition.kind == "max_iterations"
        )
        if iteration_limits:
            request_context["max_react_iterations"] = min(iteration_limits)
    if context.conversation_id is not None:
        request_context["os_conversation_id"] = context.conversation_id
    if context.approval_level is not ApprovalLevel.AGENT_PROFILE:
        request_context["approval_level"] = context.approval_level.value
    if context.model_selection is not None:
        request_context[
            "model_slot_override"
        ] = context.model_selection.model_dump(
            mode="json",
            exclude={"schema_id"},
        )
    if context.resume_checkpoint is not None:
        request_context[
            "resume_checkpoint"
        ] = context.resume_checkpoint.model_dump(mode="json")
    return request_context
