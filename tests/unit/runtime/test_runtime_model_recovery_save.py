# -*- coding: utf-8 -*-
"""Session persistence at a partial-model-step recovery boundary."""

# pylint: disable=protected-access

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from agentscope.message import (
    Msg,
    TextBlock,
    ToolResultBlock,
    ToolResultState,
)
from agentscope.state import AgentState

from qwenpaw.app.chats.session import SafeJSONSession
from qwenpaw.kernel import (
    ActionKind,
    ActionRequest,
    ActionResult,
    ActionStatus,
    ModelCallAttempt,
    ModelCallResult,
    ModelCallStatus,
    ModelFailureClass,
    ModelOutputBoundary,
    ModelRecoveryDisposition,
    ModelStepContinuationStatus,
)
from qwenpaw.recovery import ModelResourceWaitService
from qwenpaw.runtime.actions import lite_action_store
from qwenpaw.runtime.model_calls import ModelStepRecoveryError
from qwenpaw.runtime.model_step_contexts import (
    lite_model_step_context_store,
)
from qwenpaw.runtime.runtime import Runtime


class _Agent:
    def __init__(self, context: list[Msg]) -> None:
        self.name = "QwenPaw"
        self.state = AgentState(context=context)

    def _save_to_context(self, blocks: list[object]) -> None:
        self.state.context.append(
            Msg(name=self.name, role="assistant", content=blocks),
        )

    def state_dict(self) -> dict:
        return {"state": self.state.model_dump(mode="json")}


@pytest.mark.asyncio
async def test_model_step_recovery_save_excludes_partial_output(
    tmp_path,
) -> None:
    original = Msg(
        name="user",
        role="user",
        content=[TextBlock(text="finish the task")],
    )
    agent = _Agent([original])
    session = SafeJSONSession(save_dir=str(tmp_path))
    envelope = SimpleNamespace(
        collect_partial_blocks=lambda: [
            ("text", "uncommitted partial output"),
        ],
        collect_tool_output=lambda: {},
    )
    ctx = SimpleNamespace(
        extras={},
        error=RuntimeError("partial model stream"),
        agent=agent,
        session_id="chat:model-recovery",
        request=SimpleNamespace(
            user_id="user",
            channel="console",
            request_context={},
            input=[],
        ),
        workspace=SimpleNamespace(session=session),
        _envelope=envelope,
    )

    runtime = Runtime(workspace=ctx.workspace, app_services=None)
    await runtime._try_save_on_cancel(
        ctx,
        include_partial=False,
    )

    saved = await session.get_session_state_dict(
        "chat:model-recovery",
        "user",
        "console",
    )
    context = saved["agent"]["state"]["context"]
    serialized = str(context)
    assert len(context) == 1
    assert context[0]["id"] == original.id
    assert "uncommitted partial output" not in serialized


@pytest.mark.asyncio
async def test_user_interruption_save_still_includes_partial_output(
    tmp_path,
) -> None:
    agent = _Agent([])
    session = SafeJSONSession(save_dir=str(tmp_path))
    envelope = SimpleNamespace(
        collect_partial_blocks=lambda: [("text", "visible partial output")],
        collect_tool_output=lambda: {},
    )
    ctx = SimpleNamespace(
        extras={},
        error=RuntimeError("interrupted"),
        agent=agent,
        session_id="chat:user-interrupt",
        request=SimpleNamespace(
            user_id="user",
            channel="console",
            request_context={},
            input=[],
        ),
        workspace=SimpleNamespace(session=session),
        _envelope=envelope,
    )

    runtime = Runtime(workspace=ctx.workspace, app_services=None)
    await runtime._try_save_on_cancel(ctx)

    saved = await session.get_session_state_dict(
        "chat:user-interrupt",
        "user",
        "console",
    )
    context = saved["agent"]["state"]["context"]
    assert len(context) == 1
    assert context[0]["role"] == "assistant"
    assert context[0]["content"][0]["text"] == "visible partial output"


@pytest.mark.asyncio
async def test_terminal_action_context_becomes_recoverable_checkpoint(
    tmp_path,
) -> None:
    invocation_id = uuid4()
    correlation_id = uuid4()
    source_submission_id = uuid4()
    attempt = ModelCallAttempt(
        attempt_id=uuid4(),
        route_decision_id=uuid4(),
        invocation_id=invocation_id,
        correlation_id=correlation_id,
        conversation_id="chat-1",
        registry_generation=1,
        context_manifest_id=uuid4(),
        model_call_index=2,
        attempt_index=1,
        provider_id="provider-a",
        model_id="model-a",
    )
    recovery = ModelResourceWaitService(
        tmp_path / "resource-waits.sqlite3",
        agent_id="default",
    )
    continuation = await recovery.defer_model_step(
        attempt,
        ModelCallResult(
            attempt_id=attempt.attempt_id,
            invocation_id=invocation_id,
            conversation_id="chat-1",
            status=ModelCallStatus.FAILED,
            emitted_content=True,
            output_boundary=ModelOutputBoundary.PARTIAL_STREAM,
            failure_class=ModelFailureClass.STREAM_INTERRUPTED,
            recovery_disposition=(
                ModelRecoveryDisposition.CONTINUE_MODEL_STEP
            ),
        ),
    )
    assert continuation is not None
    action = ActionRequest(
        invocation_id=invocation_id,
        correlation_id=correlation_id,
        conversation_id="chat-1",
        registry_generation=1,
        capability_id="qwenpaw.system.workspace-tools",
        kind=ActionKind.TOOL,
        action_name="write_file",
        redacted_arguments={},
        arguments_hash=f"sha256:{'a' * 64}",
        idempotency_key=f"tool:{invocation_id}:call-1",
    )
    action_store = lite_action_store(tmp_path)
    await action_store.begin(action)
    await action_store.complete(
        ActionResult(
            action_id=action.action_id,
            invocation_id=invocation_id,
            conversation_id="chat-1",
            status=ActionStatus.SUCCEEDED,
            observation_digest=f"sha256:{'b' * 64}",
        ),
    )
    agent = _Agent(
        [
            Msg(
                name="QwenPaw",
                role="assistant",
                content=[
                    ToolResultBlock(
                        id="call-1",
                        name="write_file",
                        output="written",
                        state=ToolResultState.SUCCESS,
                        metadata={
                            "qwenpaw_action_id": str(action.action_id),
                        },
                    ),
                ],
            ),
        ],
    )
    session = SafeJSONSession(save_dir=str(tmp_path / "sessions"))
    workspace = SimpleNamespace(
        session=session,
        workspace_dir=tmp_path,
        model_resource_wait_service=recovery,
    )
    ctx = SimpleNamespace(
        extras={},
        error=RuntimeError("partial model stream"),
        agent=agent,
        session_id="console:chat-1",
        request=SimpleNamespace(
            user_id="user",
            channel="console",
            request_context={
                "os_submission_id": str(source_submission_id),
            },
            input=[],
        ),
        workspace=workspace,
        _envelope=SimpleNamespace(
            collect_partial_blocks=lambda: [("text", "discard me")],
            collect_tool_output=lambda: {},
        ),
    )
    runtime = Runtime(workspace=workspace, app_services=None)
    saved_state = await runtime._try_save_on_cancel(
        ctx,
        include_partial=False,
    )
    await runtime._checkpoint_model_step_context(
        ctx,
        ModelStepRecoveryError(
            continuation.continuation_id,
            ModelStepContinuationStatus.READY,
        ),
        saved_state,
    )

    ready = await recovery.get_model_step(continuation.continuation_id)
    assert ready is not None
    assert ready.context_checkpoint is not None
    assert ready.context_checkpoint.source_submission_id == (
        source_submission_id
    )
    _, snapshot = await lite_model_step_context_store(tmp_path).load(
        ready.context_checkpoint.checkpoint_id,
    )
    serialized = str(snapshot)
    assert "written" in serialized
    assert "discard me" not in serialized
