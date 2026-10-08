# -*- coding: utf-8 -*-
"""Tests for private Action retry input checkpoints."""

import asyncio
import stat
from pathlib import Path
from uuid import uuid4

import pytest
from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolResponse

from qwenpaw.kernel import (
    ACTION_RETRY_HINT_METADATA_KEY,
    ActionKind,
    ActionRequest,
    ActionRetryDecision,
    ActionRetryDisposition,
    ActionRetryReason,
    CapabilitySelection,
    InvocationScope,
    ToolEffect,
)
from qwenpaw.runtime.action_retries import (
    ActionRetryInputConflictError,
    lite_action_retry_input_store,
)
from qwenpaw.runtime.actions import (
    FilesystemActionStore,
    RuntimeActionRecorder,
)
from qwenpaw.tool_calls import ToolCallContext


def _request() -> ActionRequest:
    return ActionRequest(
        invocation_id=uuid4(),
        correlation_id=uuid4(),
        conversation_id="chat-retry-input",
        registry_generation=4,
        capability_id="example.private-tool",
        kind=ActionKind.TOOL,
        action_name="private_tool",
        arguments={"content": "private retry body", "path": "report.md"},
        redacted_arguments={
            "content": "[CONTENT OMITTED]",
            "path": "report.md",
        },
        arguments_hash=f"sha256:{'a' * 64}",
        effect=ToolEffect.NONE,
        idempotency_key="private-retry-key",
    )


def _decision() -> ActionRetryDecision:
    return ActionRetryDecision(
        disposition=ActionRetryDisposition.RETRY_FROM_NEW_ACTION,
        reason=ActionRetryReason.TRANSIENT_FAILURE,
        provider_retryable=True,
        max_attempts=2,
        next_attempt=2,
        retry_after_seconds=0,
    )


@pytest.mark.asyncio
async def test_private_retry_input_is_owner_only_and_idempotent(
    tmp_path: Path,
) -> None:
    store = lite_action_retry_input_store(tmp_path)
    request = _request()

    first = await store.save(request, _decision())
    second = await store.save(request, _decision())
    loaded, arguments = await store.load(first.checkpoint_id)

    assert second == first
    assert loaded == first
    assert arguments == request.arguments
    assert "private retry body" not in first.model_dump_json()
    path = next(
        tmp_path.glob(".qwenpaw/lite/action-retry-inputs/*.json"),
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "private retry body" in path.read_text(encoding="utf-8")

    conflicting = request.model_copy(
        update={"arguments": {"content": "different"}},
    )
    with pytest.raises(ActionRetryInputConflictError, match="conflicts"):
        await store.save(conflicting, _decision())


@pytest.mark.asyncio
async def test_runtime_binds_private_input_before_retryable_result(
    tmp_path: Path,
) -> None:
    scope = InvocationScope(
        agent_id="default",
        conversation_id="chat-retry-input-runtime",
        session_id="transport-session",
        root_agent_id="default",
        root_session_id="transport-session",
        workspace_dir=str(tmp_path),
        registry_generation=4,
        selection=CapabilitySelection(),
    )
    action_store = FilesystemActionStore(tmp_path)
    input_store = lite_action_retry_input_store(tmp_path)
    recorder = RuntimeActionRecorder(
        scope,
        action_store,
        retry_input_store=input_store,
    )
    context = ToolCallContext(
        tool_call_id="call-private-retry",
        tool_name="read_private",
        session_id=scope.session_id,
        agent_id=scope.agent_id,
        root_session_id=scope.root_session_id,
        root_agent_id=scope.root_agent_id,
        started_at=0,
        offload_deadline=None,
        cancel_event=asyncio.Event(),
    )
    context.extra["tool_input"] = {
        "content": "private runtime retry body",
        "path": "report.md",
    }
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

    [record] = await action_store.list_for_conversation(
        scope.conversation_id,
    )
    assert record.result is not None
    assert record.result.retry_decision is not None
    checkpoint_id = record.result.retry_decision.input_checkpoint_id
    assert checkpoint_id is not None
    checkpoint, arguments = await input_store.load(checkpoint_id)
    assert checkpoint.action_id == record.request.action_id
    assert checkpoint.arguments_hash == record.request.arguments_hash
    assert arguments["content"] == "private runtime retry body"
    result_path = next(
        tmp_path.glob(".qwenpaw/lite/actions/*/*/*/result.json"),
    )
    assert "private runtime retry body" not in result_path.read_text(
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_retry_input_failure_downgrades_automatic_admission(
    tmp_path: Path,
) -> None:
    class FailingInputStore:
        async def save(self, _request, _decision):
            raise OSError("disk unavailable")

        async def load(self, _checkpoint_id):
            raise AssertionError("load must not run")

    scope = InvocationScope(
        agent_id="default",
        conversation_id="chat-retry-input-failure",
        session_id="transport-session",
        root_agent_id="default",
        root_session_id="transport-session",
        workspace_dir=str(tmp_path),
        registry_generation=4,
        selection=CapabilitySelection(),
    )
    action_store = FilesystemActionStore(tmp_path)
    recorder = RuntimeActionRecorder(
        scope,
        action_store,
        retry_input_store=FailingInputStore(),
    )
    context = ToolCallContext(
        tool_call_id="call-private-retry-failure",
        tool_name="read_private",
        session_id=scope.session_id,
        agent_id=scope.agent_id,
        root_session_id=scope.root_session_id,
        root_agent_id=scope.root_agent_id,
        started_at=0,
        offload_deadline=None,
        cancel_event=asyncio.Event(),
    )
    context.extra["tool_input"] = {"path": "report.md"}
    await recorder.begin(
        context,
        effect=ToolEffect.NONE,
        policy_decision="allow",
    )
    response = await recorder.complete(
        ToolResponse(
            content=[TextBlock(type="text", text="temporary failure")],
            id=context.tool_call_id,
            state=ToolResultState.ERROR,
            metadata={ACTION_RETRY_HINT_METADATA_KEY: True},
        ),
        context,
    )

    [record] = await action_store.list_for_conversation(
        scope.conversation_id,
    )
    assert record.result is not None
    assert record.result.retryable is False
    assert record.result.retry_decision is not None
    assert record.result.retry_decision.reason is (
        ActionRetryReason.RETRY_INPUT_UNAVAILABLE
    )
    assert response.metadata["qwenpaw_action_retry_decision"] == (
        record.result.retry_decision.model_dump(mode="json")
    )
