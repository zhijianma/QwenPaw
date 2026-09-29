# -*- coding: utf-8 -*-
"""Vertical tests for governed tool side-effect accounting."""

from pathlib import Path
from uuid import uuid4

import pytest
from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk

from qwenpaw.governance import tool_adapter
from qwenpaw.governance.tool_adapter import PolicyGuardedTool
from qwenpaw.governance.tool_registry import (
    DEFAULT_REGISTRY,
    register_tool_governance,
)
from qwenpaw.kernel.models import PlanStep, SideEffectStatus
from qwenpaw.tasks.ledger import SQLiteExecutionLedger
from qwenpaw.tasks.service import TaskService
from qwenpaw.tasks.side_effects import TaskSideEffectBroker


@pytest.mark.asyncio
async def test_governed_tool_reuses_durable_tool_call_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executions = 0

    async def mutating_tool(value: str) -> ToolChunk:
        """Perform one test mutation."""
        nonlocal executions
        executions += 1
        return ToolChunk(
            is_last=True,
            state=ToolResultState.SUCCESS,
            content=[TextBlock(type="text", text=value)],
        )

    store = SQLiteExecutionLedger(tmp_path / "ledger.db")
    service = TaskService(store=store, registry_generation=1)
    task = await service.create_task(
        objective="Exercise one mutating tool",
        agent_id="default",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Mutate", objective="Call the tool"),),
    )
    _, run = await service.start_task(
        task.task_id,
        runner_id="runner.local",
    )
    broker = TaskSideEffectBroker(
        service=service,
        task_id=task.task_id,
        run_id=run.run_id,
    )
    invocation_id = uuid4()
    owner = "test.side-effect-accounting"
    register_tool_governance(
        DEFAULT_REGISTRY,
        python_name="mutating_tool",
        tool_type="internal",
        policy_name="MutatingTool",
        effect="external_write",
        owner=owner,
    )
    monkeypatch.setattr(tool_adapter, "_tool_call_id", lambda: "call-1")
    tool = PolicyGuardedTool(
        mutating_tool,
        request_context={
            "session_id": "session-1",
            "os_invocation_id": str(invocation_id),
            "os_correlation_id": str(invocation_id),
            "_task_side_effect_broker": broker,
        },
    )
    try:
        first = await tool(value="one")  # pylint: disable=not-callable
        replay = await tool(value="one")  # pylint: disable=not-callable
    finally:
        DEFAULT_REGISTRY.unregister_owner(owner)

    assert first.state is ToolResultState.SUCCESS
    assert replay.state is ToolResultState.SUCCESS
    assert executions == 1
    records = await store.list_side_effects(task.task_id)
    assert len(records) == 1
    assert records[0].status is SideEffectStatus.SUCCEEDED
    assert records[0].invocation_id == invocation_id
