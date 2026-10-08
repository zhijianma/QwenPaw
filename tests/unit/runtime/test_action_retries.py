# -*- coding: utf-8 -*-
"""Tests for private Action retry input checkpoints."""

import asyncio
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolResponse

from qwenpaw.kernel import (
    ACTION_RETRY_HINT_METADATA_KEY,
    ActionKind,
    ActionRequest,
    ActionResult,
    ActionRetryContinuationStatus,
    ActionRetryDecision,
    ActionRetryDisposition,
    ActionRetryInputCheckpoint,
    ActionRetryReason,
    ActionStatus,
    CapabilitySelection,
    InvocationScope,
    ToolEffect,
)
from qwenpaw.runtime.action_retries import (
    ActionRetryContinuationConflictError,
    ActionRetryInputConflictError,
    lite_action_retry_continuation_store,
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
        agent_id="default",
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


async def _admitted_retry(
    tmp_path: Path,
    *,
    delay: float = 0,
) -> tuple[ActionResult, ActionRetryInputCheckpoint]:
    request = _request()
    input_store = lite_action_retry_input_store(tmp_path)
    decision = _decision().model_copy(
        update={"retry_after_seconds": delay},
    )
    checkpoint = await input_store.save(request, decision)
    bound = decision.model_copy(
        update={"input_checkpoint_id": checkpoint.checkpoint_id},
    )
    result = ActionResult(
        action_id=request.action_id,
        invocation_id=request.invocation_id,
        conversation_id=request.conversation_id,
        status=ActionStatus.FAILED,
        observation_digest=f"sha256:{'b' * 64}",
        error_code="temporary_failure",
        retryable=True,
        retry_decision=bound,
        completed_at=datetime(2026, 10, 8, tzinfo=timezone.utc),
    )
    return result, checkpoint


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
    continuation_store = lite_action_retry_continuation_store(tmp_path)
    recorder = RuntimeActionRecorder(
        scope,
        action_store,
        retry_input_store=input_store,
        retry_continuation_store=continuation_store,
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
    [continuation] = await continuation_store.list_pending(
        agent_id=scope.agent_id,
    )
    assert continuation.checkpoint == checkpoint
    assert continuation.source_observation_digest == (
        record.result.observation_digest
    )
    assert continuation.status is ActionRetryContinuationStatus.READY
    outbox_path = next(
        tmp_path.glob(
            ".qwenpaw/lite/action-retry-continuations/*.json",
        ),
    )
    assert stat.S_IMODE(outbox_path.stat().st_mode) == 0o600
    assert "private runtime retry body" not in outbox_path.read_text(
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


@pytest.mark.asyncio
async def test_retry_outbox_promotes_delay_and_dispatches_once(
    tmp_path: Path,
) -> None:
    result, checkpoint = await _admitted_retry(tmp_path, delay=30)
    now = [result.completed_at]
    store = lite_action_retry_continuation_store(
        tmp_path,
        clock=lambda: now[0],
    )

    waiting = await store.defer(checkpoint, result)
    assert waiting.status is ActionRetryContinuationStatus.WAITING_DELAY
    [not_ready] = await store.list_pending(
        agent_id=checkpoint.agent_id,
    )
    assert not_ready.status is ActionRetryContinuationStatus.WAITING_DELAY

    now[0] += timedelta(seconds=30)
    [ready] = await store.list_pending(agent_id=checkpoint.agent_id)
    assert ready.status is ActionRetryContinuationStatus.READY
    dispatch_id = uuid4()
    calls = []

    async def dispatch(continuation):
        calls.append(continuation.continuation_id)
        return dispatch_id

    dispatched = await store.dispatch(ready.continuation_id, dispatch)
    repeated = await store.dispatch(ready.continuation_id, dispatch)

    assert dispatched.status is ActionRetryContinuationStatus.DISPATCHED
    assert dispatched.dispatch_id == dispatch_id
    assert repeated == dispatched
    assert calls == [ready.continuation_id]
    assert await store.list_pending(agent_id=checkpoint.agent_id) == ()


@pytest.mark.asyncio
async def test_retry_outbox_rejects_mismatch_and_cancels(
    tmp_path: Path,
) -> None:
    result, checkpoint = await _admitted_retry(tmp_path)
    store = lite_action_retry_continuation_store(tmp_path)

    with pytest.raises(
        ActionRetryContinuationConflictError,
        match="identity",
    ):
        await store.defer(
            checkpoint,
            result.model_copy(update={"action_id": uuid4()}),
        )

    ready = await store.defer(checkpoint, result)
    cancelled = await store.cancel(ready.continuation_id)
    repeated = await store.cancel(ready.continuation_id)

    assert cancelled.status is ActionRetryContinuationStatus.CANCELLED
    assert repeated == cancelled
    assert await store.list_pending(agent_id=checkpoint.agent_id) == ()


@pytest.mark.asyncio
async def test_retry_outbox_cancels_one_conversation(
    tmp_path: Path,
) -> None:
    result, checkpoint = await _admitted_retry(tmp_path)
    store = lite_action_retry_continuation_store(tmp_path)
    ready = await store.defer(checkpoint, result)
    conversation_id = checkpoint.conversation_id
    assert conversation_id is not None

    assert await store.cancel_for_conversation(
        agent_id=checkpoint.agent_id,
        conversation_id="another-chat",
    ) == ()
    [cancelled] = await store.cancel_for_conversation(
        agent_id=checkpoint.agent_id,
        conversation_id=conversation_id,
    )

    assert cancelled.continuation_id == ready.continuation_id
    assert cancelled.status is ActionRetryContinuationStatus.CANCELLED


@pytest.mark.asyncio
async def test_result_remains_committed_when_outbox_publish_fails(
    tmp_path: Path,
) -> None:
    scope = InvocationScope(
        agent_id="default",
        conversation_id="chat-retry-outbox-failure",
        session_id="transport-session",
        root_agent_id="default",
        root_session_id="transport-session",
        workspace_dir=str(tmp_path),
        registry_generation=4,
        selection=CapabilitySelection(),
    )
    action_store = FilesystemActionStore(tmp_path)

    class FailingContinuationStore:
        async def defer(self, _checkpoint, result):
            records = await action_store.list_for_conversation(
                scope.conversation_id,
            )
            assert records[0].result == result
            raise OSError("outbox unavailable")

    recorder = RuntimeActionRecorder(
        scope,
        action_store,
        retry_input_store=lite_action_retry_input_store(tmp_path),
        retry_continuation_store=FailingContinuationStore(),
    )
    context = ToolCallContext(
        tool_call_id="call-retry-outbox-failure",
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
    assert record.result.retryable is True


@pytest.mark.asyncio
async def test_retry_outbox_repairs_post_result_crash_once(
    tmp_path: Path,
) -> None:
    request = _request()
    input_store = lite_action_retry_input_store(tmp_path)
    checkpoint = await input_store.save(request, _decision())
    decision = _decision().model_copy(
        update={"input_checkpoint_id": checkpoint.checkpoint_id},
    )
    result = ActionResult(
        action_id=request.action_id,
        invocation_id=request.invocation_id,
        conversation_id=request.conversation_id,
        status=ActionStatus.FAILED,
        observation_digest=f"sha256:{'c' * 64}",
        error_code="temporary_failure",
        retryable=True,
        retry_decision=decision,
    )
    action_store = FilesystemActionStore(tmp_path)
    await action_store.begin(request)
    await action_store.complete(result)
    outbox = lite_action_retry_continuation_store(tmp_path)

    assert await outbox.list_pending(agent_id=checkpoint.agent_id) == ()
    [first] = await outbox.repair(
        agent_id=checkpoint.agent_id,
        input_store=input_store,
        action_store=action_store,
    )
    [second] = await outbox.repair(
        agent_id=checkpoint.agent_id,
        input_store=input_store,
        action_store=action_store,
    )

    assert first == second
    assert first.checkpoint == checkpoint
    assert first.source_observation_digest == result.observation_digest
    [pending] = await outbox.list_pending(agent_id=checkpoint.agent_id)
    assert pending == first


@pytest.mark.asyncio
async def test_retry_outbox_repair_rejects_checkpoint_request_drift(
    tmp_path: Path,
) -> None:
    request = _request()
    input_store = lite_action_retry_input_store(tmp_path)
    checkpoint = await input_store.save(request, _decision())
    decision = _decision().model_copy(
        update={"input_checkpoint_id": checkpoint.checkpoint_id},
    )
    result = ActionResult(
        action_id=request.action_id,
        invocation_id=request.invocation_id,
        conversation_id=request.conversation_id,
        status=ActionStatus.FAILED,
        observation_digest=f"sha256:{'d' * 64}",
        retryable=True,
        retry_decision=decision,
    )
    action_store = FilesystemActionStore(tmp_path)
    await action_store.begin(request)
    await action_store.complete(result)

    class DriftedInputStore:
        async def list_checkpoints(self, *, agent_id):
            assert agent_id == checkpoint.agent_id
            return (
                checkpoint.model_copy(
                    update={"arguments_hash": f"sha256:{'e' * 64}"},
                ),
            )

    with pytest.raises(
        ActionRetryContinuationConflictError,
        match="does not match",
    ):
        await lite_action_retry_continuation_store(tmp_path).repair(
            agent_id=checkpoint.agent_id,
            input_store=DriftedInputStore(),
            action_store=action_store,
        )
