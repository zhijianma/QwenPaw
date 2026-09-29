# -*- coding: utf-8 -*-
"""Tests for the typed task runtime context boundary."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from qwenpaw.kernel.models import (
    ApprovalLevel,
    ExecutionCheckpoint,
    ExecutionContract,
    ExitCondition,
    ModelSelection,
    Run,
    RuntimeLaunchConfig,
    TaskOrder,
)
from qwenpaw.tasks.context import (
    legacy_request_context,
    order_metadata_from_launch_config,
    runtime_context_from_order,
)


def test_runtime_context_round_trip_and_legacy_adapter() -> None:
    task_id = uuid4()
    invocation_id = uuid4()
    correlation_id = uuid4()
    run = Run(
        task_id=task_id,
        attempt=2,
        registry_generation=7,
        runner_id="plugin.runner",
        invocation_id=invocation_id,
        correlation_id=correlation_id,
    )
    checkpoint = ExecutionCheckpoint(
        task_id=task_id,
        run_id=uuid4(),
        sequence=9,
        safe_to_resume=True,
        runner_cursor={"next_step": 3},
    )
    config = RuntimeLaunchConfig(
        agent_id="default",
        conversation_id="chat-7",
        project_dir="/workspace/project",
        ledger_workspace_dir="/workspace/agent",
        approval_level=ApprovalLevel.STRICT,
        model_selection=ModelSelection(
            provider_id="dashscope",
            model="qwen-max",
        ),
        strategy_id="plugin.strategy",
    )
    order = TaskOrder(
        task_id=task_id,
        objective="Resume work",
        acceptance_criteria=("Work resumes",),
        execution_contract=ExecutionContract(
            goal="Resume work",
            acceptance=("Work resumes",),
        ),
        metadata=order_metadata_from_launch_config(
            config,
            resume_checkpoint=checkpoint,
        ),
    )

    context = runtime_context_from_order(
        order,
        run,
        strategy_id="plugin.strategy",
    )
    legacy = legacy_request_context(context)

    assert context.registry_generation == 7
    assert context.invocation_id == invocation_id
    assert context.correlation_id == correlation_id
    assert context.conversation_id == "chat-7"
    assert context.model_selection == config.model_selection
    assert context.capability_ids == (
        "plugin.runner",
        "plugin.strategy",
    )
    assert context.strategy is not None
    assert context.strategy.strategy_id == "plugin.strategy"
    assert context.resume_checkpoint == checkpoint
    assert context.checkpoint_broker is not None
    assert context.execution_contract == order.execution_contract
    assert legacy["_task_approval_broker"] is context.approval_broker
    assert legacy["_task_checkpoint_broker"] is context.checkpoint_broker
    assert legacy["os_registry_generation"] == 7
    assert legacy["os_invocation_id"] == str(invocation_id)
    assert legacy["os_correlation_id"] == str(correlation_id)
    assert legacy["os_conversation_id"] == "chat-7"
    assert legacy["approval_level"] == "strict"
    assert legacy["model_slot_override"] == {
        "provider_id": "dashscope",
        "model": "qwen-max",
    }
    assert legacy["execution_contract"]["goal"] == "Resume work"
    assert "runtime_strategy" not in legacy
    assert legacy["resume_checkpoint"]["runner_cursor"] == {
        "next_step": 3,
    }


def test_runtime_context_rejects_partial_checkpoint() -> None:
    task_id = uuid4()
    run = Run(
        task_id=task_id,
        attempt=1,
        registry_generation=1,
        runner_id="runner.local",
    )
    order = TaskOrder(
        task_id=task_id,
        objective="Reject incomplete state",
        metadata={
            "agent_id": "default",
            "project_dir": "/workspace/project",
            "task_ledger_workspace_dir": "/workspace/agent",
            "resume_checkpoint": {"runner_cursor": {"next_step": 1}},
        },
    )

    with pytest.raises(ValidationError):
        runtime_context_from_order(order, run)


def test_runtime_context_adapts_an_empty_legacy_order() -> None:
    task_id = uuid4()
    run = Run(
        task_id=task_id,
        attempt=1,
        registry_generation=3,
        runner_id="runner.legacy",
    )

    context = runtime_context_from_order(
        TaskOrder(task_id=task_id, objective="Run legacy callback"),
        run,
    )

    assert context.agent_id == "default"
    assert context.project_dir == "."
    assert context.ledger_workspace_dir == "."
    assert context.approval_level is ApprovalLevel.AGENT_PROFILE
    assert context.invocation_id == run.run_id
    assert context.correlation_id == run.run_id
    assert context.conversation_id is None
    assert "os_conversation_id" not in legacy_request_context(context)


def test_legacy_context_applies_strictest_iteration_exit_condition() -> None:
    task_id = uuid4()
    contract = ExecutionContract(
        goal="Bound the agent loop",
        exit_conditions=(
            ExitCondition(
                condition_id="outer-limit",
                kind="max_iterations",
                parameters={"limit": 12},
            ),
            ExitCondition(
                condition_id="strict-limit",
                kind="max_iterations",
                parameters={"limit": 4},
            ),
        ),
    )
    context = runtime_context_from_order(
        TaskOrder(
            task_id=task_id,
            objective=contract.goal,
            execution_contract=contract,
        ),
        Run(
            task_id=task_id,
            attempt=1,
            registry_generation=1,
            runner_id="runner.console",
        ),
    )

    assert legacy_request_context(context)["max_react_iterations"] == 4
