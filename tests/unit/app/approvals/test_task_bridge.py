# -*- coding: utf-8 -*-
"""Tests for durable Task projection of runtime tool approvals."""

import asyncio
from pathlib import Path
from uuid import UUID

import pytest

import qwenpaw.app.approvals as approvals_package
from qwenpaw.app.approvals.service import ApprovalService
from qwenpaw.app.approvals.task_bridge import (
    task_approval_bridge_from_context,
)
from qwenpaw.interactions import InteractionService
from qwenpaw.kernel import ContinuationMode
from qwenpaw.kernel.models import PlanStep, RunStatus, TaskStatus
from qwenpaw.security.tool_guard.approval import ApprovalDecision
from qwenpaw.security.tool_guard.models import (
    GuardFinding,
    GuardSeverity,
    GuardThreatCategory,
    ToolGuardResult,
)
from qwenpaw.runtime.tool_guard import _ask_user_approval
from qwenpaw.tasks.bootstrap import task_service_for_workspace
from qwenpaw.tasks.approval_broker import TaskApprovalBroker


class _Workspace:
    def __init__(self, workspace_dir: Path) -> None:
        self.workspace_dir = workspace_dir


def _guard_result() -> ToolGuardResult:
    return ToolGuardResult(
        tool_name="execute_shell_command",
        params={"command": "echo safe"},
        findings=[
            GuardFinding(
                id="finding-1",
                rule_id="shell-review",
                category=GuardThreatCategory.CODE_EXECUTION,
                severity=GuardSeverity.HIGH,
                title="Shell execution",
                description="Command execution needs approval",
                tool_name="execute_shell_command",
            ),
        ],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime_decision", "durable_value"),
    [
        (ApprovalDecision.APPROVED, "approved"),
        (ApprovalDecision.DENIED, "denied"),
        (ApprovalDecision.TIMEOUT, "expired"),
    ],
)
async def test_runtime_decision_is_durable_before_future_wakes(
    tmp_path: Path,
    runtime_decision: ApprovalDecision,
    durable_value: str,
) -> None:
    workspace = _Workspace(tmp_path)
    task_service = task_service_for_workspace(workspace)
    task = await task_service.create_task(
        objective="Run a guarded command",
        agent_id="default",
    )
    await task_service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Guard", objective=task.objective),),
    )
    await task_service.start_task(task.task_id, runner_id="runner.local")

    runtime_service = ApprovalService()
    pending = await runtime_service.create_pending(
        session_id=f"task-{task.task_id}",
        root_session_id=f"task-{task.task_id}",
        owner_agent_id="default",
        user_id="local-user",
        channel="console",
        agent_id="default",
        tool_name="execute_shell_command",
        result=_guard_result(),
    )
    bridge = await task_approval_bridge_from_context(
        {
            "durable_task": True,
            "task_id": str(task.task_id),
            "task_ledger_workspace_dir": str(tmp_path),
        },
        pending.request_id,
    )
    assert bridge is not None
    await bridge.request(
        agent_id="default",
        tool_name="execute_shell_command",
        severity="HIGH",
        input_data={"api_key": "must-not-persist"},
    )
    pending.resolution_hook = bridge.resolve

    await runtime_service.resolve_request(
        pending.request_id,
        runtime_decision,
    )

    restored = await task_service.get_task(task.task_id)
    runs = await task_service.list_runs(task.task_id)
    record = await task_service.get_approval(bridge.approval_id)
    assert restored is not None
    assert restored.status is TaskStatus.RUNNING
    assert runs[-1].status is RunStatus.RUNNING
    assert record is not None
    assert record[1] is not None
    assert record[1].decision.value == durable_value
    assert "must-not-persist" not in str(record[0].redacted_arguments)
    assert pending.future.result() is runtime_decision


@pytest.mark.asyncio
async def test_injected_broker_does_not_rebuild_service_from_disk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qwenpaw.app.approvals import task_bridge

    service = task_service_for_workspace(_Workspace(tmp_path))
    task = await service.create_task(
        objective="Use the typed approval service",
        agent_id="default",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Guard", objective=task.objective),),
    )
    _, run = await service.start_task(
        task.task_id,
        runner_id="runner.local",
    )
    broker = TaskApprovalBroker(service, task.task_id, run.run_id)

    async def fail_legacy(_context):  # noqa: ANN
        raise AssertionError("legacy approval service rebuild was used")

    monkeypatch.setattr(
        task_bridge,
        "_legacy_approval_broker",
        fail_legacy,
    )
    approval_id = UUID("00000000-0000-0000-0000-000000000123")
    bridge = await task_approval_bridge_from_context(
        {
            "durable_task": True,
            "_task_approval_broker": broker,
        },
        str(approval_id),
    )

    assert bridge is not None
    await bridge.request(
        agent_id="default",
        tool_name="Read",
        severity="high",
        input_data={"path": "README.md"},
    )
    record = await service.get_approval(approval_id)
    assert record is not None
    assert record[0].run_id == run.run_id


@pytest.mark.asyncio
async def test_resolution_hook_failure_keeps_runtime_pending() -> None:
    runtime_service = ApprovalService()
    pending = await runtime_service.create_pending(
        session_id="task-session",
        root_session_id="task-session",
        owner_agent_id="default",
        user_id="local-user",
        channel="console",
        agent_id="default",
        tool_name="execute_shell_command",
        result=_guard_result(),
    )

    async def fail_persistence(*_args) -> None:
        raise RuntimeError("ledger unavailable")

    pending.resolution_hook = fail_persistence
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        await runtime_service.resolve_request(
            pending.request_id,
            ApprovalDecision.APPROVED,
        )

    assert await runtime_service.get_request(pending.request_id) is pending
    assert not pending.future.done()


@pytest.mark.asyncio
async def test_tool_guard_runtime_attaches_durable_task_bridge(
    tmp_path: Path,
    monkeypatch,
) -> None:
    invocation_id = UUID("00000000-0000-0000-0000-000000000321")
    correlation_id = UUID("00000000-0000-0000-0000-000000000654")
    workspace = _Workspace(tmp_path)
    task_service = task_service_for_workspace(workspace)
    task = await task_service.create_task(
        objective="Run through the guarded runtime",
        agent_id="default",
    )
    await task_service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Guard", objective=task.objective),),
    )
    _, run = await task_service.start_task(
        task.task_id,
        runner_id="runner.local",
    )
    broker = TaskApprovalBroker(
        task_service,
        task.task_id,
        run.run_id,
    )
    runtime_service = ApprovalService()
    interaction_service = InteractionService(
        tmp_path / "interactions.sqlite3",
    )
    monkeypatch.setattr(
        approvals_package,
        "get_approval_service",
        lambda: runtime_service,
    )

    permission_task = asyncio.create_task(
        _ask_user_approval(
            agent_id="default",
            tool_name="execute_shell_command",
            input_data={"command": "echo safe"},
            guard_result=_guard_result(),
            request_context={
                "durable_task": True,
                "_task_approval_broker": broker,
                "task_id": str(task.task_id),
                "task_ledger_workspace_dir": str(tmp_path),
                "execution_contract": {
                    "timeout_policy": {"approval_seconds": 5},
                },
                "session_id": f"task-{task.task_id}",
                "root_session_id": f"task-{task.task_id}",
                "user_id": "local-user",
                "channel": "console",
                "os_invocation_id": str(invocation_id),
                "os_correlation_id": str(correlation_id),
                "os_conversation_id": "chat-spec-approval",
                "_interaction_service": interaction_service,
            },
        ),
    )
    for _ in range(100):
        pending_items = await runtime_service.get_all_pending_by_session(
            f"task-{task.task_id}",
        )
        if (
            pending_items
            and pending_items[0].resolution_hook is not None
            and pending_items[0].interaction_id is not None
        ):
            break
        if permission_task.done():
            await permission_task
        await asyncio.sleep(0.01)

    assert len(pending_items) == 1
    pending = pending_items[0]
    assert pending.resolution_hook is not None
    durable = await task_service.get_approval(UUID(pending.request_id))
    assert durable is not None
    assert durable[0].invocation_id == invocation_id
    assert durable[0].correlation_id == correlation_id
    assert durable[0].expires_at is not None
    assert (
        4.9
        <= (durable[0].expires_at - durable[0].created_at).total_seconds()
        <= 5.1
    )
    interactions = await interaction_service.list_open(
        agent_id="default",
        conversation_id="chat-spec-approval",
    )
    assert [item.interaction_id for item in interactions] == [
        UUID(pending.request_id),
    ]
    assert (
        interactions[0].continuation_checkpoint_id == durable[0].checkpoint_id
    )
    waits = await interaction_service.list_wait_conditions(
        agent_id="default",
        conversation_id="chat-spec-approval",
    )
    assert waits[0].continuation.mode is ContinuationMode.CHECKPOINT
    assert waits[0].continuation.checkpoint_id == durable[0].checkpoint_id
    await runtime_service.resolve_request(
        pending.request_id,
        ApprovalDecision.DENIED,
    )
    permission = await permission_task

    restored = await task_service.get_task(task.task_id)
    assert permission.behavior.value == "deny"
    assert restored is not None
    assert restored.status is TaskStatus.RUNNING
