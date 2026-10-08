# -*- coding: utf-8 -*-
"""Tests for model-independent governed Action execution."""

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolChunk, ToolResponse

from qwenpaw.kernel import (
    ACTION_RETRY_HINT_METADATA_KEY,
    CapabilitySelection,
    InvocationScope,
    ToolEffect,
)
from qwenpaw.runtime.action_execution import (
    GovernedActionDeniedError,
    GovernedActionExecutor,
)
from qwenpaw.runtime.actions import (
    FilesystemActionStore,
    RuntimeActionRecorder,
)
from qwenpaw.tool_calls import ToolCoordinator
from qwenpaw.tool_calls._context import ToolCallContext
from qwenpaw.tool_calls._ctxvars import get_call_context


def _scope(
    tmp_path: Path,
    *,
    correlation_id=None,
) -> InvocationScope:
    correlation_id = correlation_id or uuid4()
    return InvocationScope(
        invocation_id=uuid4(),
        correlation_id=correlation_id,
        agent_id="default",
        conversation_id="chat-governed-retry",
        session_id="transport-session",
        root_agent_id="default",
        root_session_id="transport-session",
        workspace_dir=str(tmp_path),
        registry_generation=5,
        selection=CapabilitySelection(),
    )


@pytest.mark.asyncio
async def test_governed_executor_creates_new_retry_attempt(
    tmp_path: Path,
) -> None:
    store = FilesystemActionStore(tmp_path)
    first_scope = _scope(tmp_path)
    first_recorder = RuntimeActionRecorder(first_scope, store)
    first_context = ToolCallContext(
        tool_call_id="call-first-attempt",
        tool_name="stable_tool",
        session_id=first_scope.session_id,
        agent_id=first_scope.agent_id,
        root_session_id=first_scope.root_session_id,
        root_agent_id=first_scope.root_agent_id,
        started_at=0,
        offload_deadline=None,
        cancel_event=asyncio.Event(),
    )
    first_context.extra["tool_input"] = {"value": "same-input"}
    await first_recorder.begin(
        first_context,
        effect=ToolEffect.NONE,
        policy_decision="allow",
    )
    await first_recorder.complete(
        ToolResponse(
            content=[TextBlock(type="text", text="temporary failure")],
            id=first_context.tool_call_id,
            state=ToolResultState.ERROR,
            metadata={ACTION_RETRY_HINT_METADATA_KEY: True},
        ),
        first_context,
    )
    [previous] = await store.list_for_conversation(
        first_scope.conversation_id,
    )
    retry_scope = _scope(
        tmp_path,
        correlation_id=first_scope.correlation_id,
    )
    retry_recorder = RuntimeActionRecorder(
        retry_scope,
        store,
        retry_of=previous,
    )
    permission_checks = []

    class GovernedTool:
        name = "stable_tool"
        _qp_request_context = {"_action_recorder": retry_recorder}

        async def check_permissions(self, arguments, _context):
            permission_checks.append(dict(arguments))
            return PermissionDecision(
                behavior=PermissionBehavior.ALLOW,
                message="allowed",
            )

        async def __call__(self, *, value):
            context = get_call_context()
            assert context is not None
            await retry_recorder.begin(
                context,
                effect=ToolEffect.NONE,
                policy_decision="allow",
            )
            return ToolChunk(
                is_last=True,
                state=ToolResultState.SUCCESS,
                content=[TextBlock(type="text", text=value)],
            )

    response = await GovernedActionExecutor(ToolCoordinator()).execute(
        tool=GovernedTool(),
        arguments={"value": "same-input"},
        tool_call_id="call-retry-attempt",
        scope=retry_scope,
        recorder=retry_recorder,
    )

    records = await store.list_for_conversation(
        retry_scope.conversation_id,
    )
    retry = next(record for record in records if record.request.attempt == 2)
    assert permission_checks == [{"value": "same-input"}]
    assert response.state is ToolResultState.SUCCESS
    assert retry.request.retry_of_action_id == previous.request.action_id
    assert retry.request.retry_root_action_id == previous.request.action_id
    assert retry.request.correlation_id == previous.request.correlation_id
    assert retry.request.registry_generation == 5
    assert retry.result is not None
    assert retry.result.status.value == "succeeded"


@pytest.mark.asyncio
async def test_governed_executor_denial_creates_no_action(
    tmp_path: Path,
) -> None:
    store = FilesystemActionStore(tmp_path)
    scope = _scope(tmp_path)
    recorder = RuntimeActionRecorder(scope, store)

    class DeniedTool:
        name = "denied_tool"
        _qp_request_context = {"_action_recorder": recorder}

        @staticmethod
        async def check_permissions(_arguments, _context):
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message="denied by current policy",
            )

    with pytest.raises(
        GovernedActionDeniedError,
        match="current policy",
    ):
        await GovernedActionExecutor(ToolCoordinator()).execute(
            tool=DeniedTool(),
            arguments={"value": "private"},
            tool_call_id="call-denied-retry",
            scope=scope,
            recorder=recorder,
        )

    assert not await store.list_for_conversation(scope.conversation_id)


@pytest.mark.asyncio
async def test_governed_executor_rejects_foreign_recorder(
    tmp_path: Path,
) -> None:
    store = FilesystemActionStore(tmp_path)
    scope = _scope(tmp_path)
    bound_recorder = RuntimeActionRecorder(scope, store)
    foreign_recorder = RuntimeActionRecorder(scope, store)

    class GovernedTool:
        name = "stable_tool"
        _qp_request_context = {"_action_recorder": bound_recorder}

        @staticmethod
        async def check_permissions(_arguments, _context):
            raise AssertionError("permission must follow recorder validation")

    with pytest.raises(TypeError, match="not bound to its retry recorder"):
        await GovernedActionExecutor(ToolCoordinator()).execute(
            tool=GovernedTool(),
            arguments={"value": "same-input"},
            tool_call_id="call-foreign-recorder",
            scope=scope,
            recorder=foreign_recorder,
        )

    assert not await store.list_for_conversation(scope.conversation_id)
