# -*- coding: utf-8 -*-
"""Tests for model-independent governed Action execution."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
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
    ToolSelection,
)
from qwenpaw.runtime.action_execution import (
    ActionRetryAdmissionError,
    ActionRetryExecutionAdmission,
    ActionRetryExecutionPlan,
    GovernedActionDeniedError,
    GovernedActionExecutor,
    RuntimeActionRetryRunner,
)
from qwenpaw.runtime.action_retries import (
    lite_action_retry_continuation_store,
    lite_action_retry_input_store,
)
from qwenpaw.runtime.actions import (
    FilesystemActionStore,
    RuntimeActionRecorder,
)
from qwenpaw.runtime.sandbox_environments import (
    RuntimeSandboxEnvironmentManager,
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
            context = get_call_context()
            assert context is not None
            assert context.tool_call_id == "call-retry-attempt"
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
            context = get_call_context()
            assert context is not None
            assert context.tool_call_id == "call-denied-retry"
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


@pytest.mark.asyncio
async def test_retry_execution_admission_rebuilds_durable_authority(
    tmp_path: Path,
) -> None:
    store = FilesystemActionStore(tmp_path)
    scope = _scope(tmp_path)
    inputs = lite_action_retry_input_store(tmp_path)
    outbox = lite_action_retry_continuation_store(tmp_path)
    recorder = RuntimeActionRecorder(
        scope,
        store,
        retry_input_store=inputs,
        retry_continuation_store=outbox,
        tool_selection=ToolSelection(active_modes=("coding",)),
        provider_execution_digests={
            "qwenpaw.system.workspace-tools": f"sha256:{'a' * 64}",
        },
    )
    context = ToolCallContext(
        tool_call_id="call-admission-source",
        tool_name="stable_tool",
        session_id=scope.session_id,
        agent_id=scope.agent_id,
        root_session_id=scope.root_session_id,
        root_agent_id=scope.root_agent_id,
        started_at=0,
        offload_deadline=None,
        cancel_event=asyncio.Event(),
    )
    context.extra["tool_input"] = {"value": "private"}
    await recorder.begin(
        context,
        effect=ToolEffect.NONE,
        policy_decision="allow",
    )
    await recorder.complete(
        ToolResponse(
            content=[TextBlock(type="text", text="temporary failure")],
            id=context.tool_call_id,
            state=ToolResultState.ERROR,
            metadata={ACTION_RETRY_HINT_METADATA_KEY: True},
        ),
        context,
    )
    [continuation] = await outbox.list_pending(agent_id=scope.agent_id)
    admission = ActionRetryExecutionAdmission(
        input_store=inputs,
        action_store=store,
    )

    prepared = await admission.prepare(continuation)
    plan = ActionRetryExecutionPlan.from_prepared(prepared)

    assert prepared.arguments == {"value": "private"}
    assert prepared.previous.request.action_id == (
        continuation.checkpoint.action_id
    )
    assert plan.chat_id == scope.conversation_id
    assert plan.correlation_id == scope.correlation_id
    assert plan.registry_generation == scope.registry_generation
    assert plan.capability_selection.tool_provider_ids == (
        continuation.checkpoint.capability_id,
    )
    assert plan.capability_selection.memory_provider_id is None
    assert plan.tool_selection == continuation.checkpoint.tool_selection
    assert plan.provider_execution_digest == (
        continuation.checkpoint.provider_execution_digest
    )
    assert plan == ActionRetryExecutionPlan.from_prepared(prepared)

    class Assembly:
        def __init__(self, invocation_scope):
            self.scope = invocation_scope
            self.closed = False

        async def close(self):
            self.closed = True

    assembly = Assembly(
        scope.model_copy(
            update={
                "invocation_id": plan.invocation_id,
                "correlation_id": plan.correlation_id,
                "session_id": plan.chat_id,
                "root_session_id": plan.chat_id,
                "selection": plan.capability_selection,
            },
        ),
    )

    class Factory:
        async def open(self, **kwargs):
            assert kwargs["invocation_id"] == plan.invocation_id
            assert kwargs["session_id"] == plan.chat_id
            return assembly

    resolved_context = {}

    class Builder:
        async def resolve_governed_action_tool(
            self,
            *,
            request_context,
            provider_id,
            **_kwargs,
        ):
            request_context["_tool_provider_execution_digests"] = {
                provider_id: plan.provider_execution_digest,
            }
            resolved_context.update(request_context)
            return SimpleNamespace(name="stable_tool")

    executed = {}

    class Executor:
        async def execute(self, **kwargs):
            executed.update(kwargs)
            return ToolResponse(
                content=[TextBlock(type="text", text="retried")],
                id=plan.tool_call_id,
                state=ToolResultState.SUCCESS,
            )

    coordinator = ToolCoordinator()
    approval_coordinator = object()
    runner = RuntimeActionRetryRunner(
        workspace=SimpleNamespace(
            agent_id=scope.agent_id,
            workspace_dir=tmp_path,
            interaction_service=None,
            app_services=SimpleNamespace(
                approval_coordinator=approval_coordinator,
                tool_coordinator=coordinator,
            ),
        ),
        coordinator=coordinator,
        action_store=store,
        retry_input_store=inputs,
        retry_continuation_store=outbox,
        builder=Builder(),
        executor=Executor(),
        assembly_factory=Factory(),
    )

    response = await runner.run(
        prepared=prepared,
        plan=plan,
        agent_config=SimpleNamespace(),
        governor=None,
    )

    assert response.state is ToolResultState.SUCCESS
    assert executed["arguments"] == {"value": "private"}
    assert executed["tool_call_id"] == plan.tool_call_id
    assert resolved_context["approval_coordinator"] is approval_coordinator
    assert resolved_context["tool_coordinator"] is coordinator
    assert isinstance(
        resolved_context["_sandbox_environment_manager"],
        RuntimeSandboxEnvironmentManager,
    )
    assert assembly.closed is True
    drifted = continuation.model_copy(
        update={
            "source_observation_digest": f"sha256:{'b' * 64}",
        },
    )
    with pytest.raises(
        ActionRetryAdmissionError,
        match="no longer authorizes",
    ):
        await admission.prepare(drifted)
