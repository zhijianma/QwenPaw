# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Governed tools bind Sandbox evidence before executor admission."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from qwenpaw.governance import tool_adapter
from qwenpaw.kernel import (
    ActionIdempotencyMode,
    ActionKind,
    EnvironmentRef,
    EnvironmentResolution,
    EnvironmentResolutionStatus,
    ToolEffect,
)
from qwenpaw.runtime.environments import (
    EnvironmentContractUnsatisfiedError,
)
from qwenpaw.sandbox import SandboxConfig, SandboxMode


def _resolution(status: EnvironmentResolutionStatus):
    return EnvironmentResolution(
        invocation_id=uuid4(),
        action_id=uuid4(),
        contract_id="qwenpaw.system.environment.sandbox",
        contract_version="1.0.0",
        resolver_id="qwenpaw.system.environment.sandbox-resolver",
        status=status,
        os_family="macos",
        architecture="arm64",
        workspace_root="/workspace",
        violations=(
            ("environment.memory_limit.unenforced",)
            if status is EnvironmentResolutionStatus.UNSATISFIED
            else ()
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        EnvironmentResolutionStatus.SATISFIED,
        EnvironmentResolutionStatus.UNSATISFIED,
    ],
)
async def test_sandbox_resolution_is_recorded_before_admission(
    status: EnvironmentResolutionStatus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action_id = uuid4()
    reference = EnvironmentRef(
        resolution_id=uuid4(),
        contract_id="qwenpaw.system.environment.sandbox",
        contract_version="1.0.0",
        resolver_id="qwenpaw.system.environment.sandbox-resolver",
    )
    resolution = _resolution(status)
    recorder = SimpleNamespace(
        action_id=Mock(return_value=action_id),
        begin=AsyncMock(),
    )
    manager = SimpleNamespace(
        resolve=AsyncMock(return_value=(reference, resolution)),
    )
    context = SimpleNamespace(tool_call_id="call-1")
    monkeypatch.setattr(
        tool_adapter,
        "_active_tool_call_context",
        lambda: context,
    )
    tool = SimpleNamespace(
        _qp_request_context={
            "_action_recorder": recorder,
            "_sandbox_environment_manager": manager,
        },
        _qp_tc_spec=SimpleNamespace(effect=ToolEffect.PROCESS.value),
        _qp_sandbox_config=SandboxConfig(
            mode=SandboxMode.NONE,
            workspace_dir="/workspace",
        ),
        _qp_sandbox_mode=True,
        _qp_policy_decision=None,
        _qp_approval_id="",
    )

    if status is EnvironmentResolutionStatus.UNSATISFIED:
        with pytest.raises(EnvironmentContractUnsatisfiedError):
            await tool_adapter._begin_tool_action(tool)
    else:
        await tool_adapter._begin_tool_action(tool)

    recorder.action_id.assert_called_once_with(
        context,
        kind=ActionKind.SHELL,
    )
    manager.resolve.assert_awaited_once_with(
        tool._qp_sandbox_config,
        action_id=action_id,
    )
    recorder.begin.assert_awaited_once_with(
        context,
        effect=ToolEffect.PROCESS,
        policy_decision="allow",
        approval_id=None,
        environment_ref=reference,
        kind=ActionKind.SHELL,
        idempotency_mode=ActionIdempotencyMode.UNDECLARED,
    )
