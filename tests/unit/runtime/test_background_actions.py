# -*- coding: utf-8 -*-
"""Tests for durable background Action continuation infrastructure."""

import stat
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from agentscope.message import Msg, TextBlock, ToolResultState
from agentscope.tool import ToolResponse

from qwenpaw.kernel import (
    COMMITTED_ACTION_ITEM_METADATA_KEY,
    BackgroundActionContinuationStatus,
    CommittedActionItem,
    SubmissionStatus,
)
from qwenpaw.runtime.background_actions import (
    BackgroundActionCompletionHandler,
    build_background_action_checkpoint,
    build_background_action_snapshot,
    lite_background_action_context_store,
    lite_background_action_continuation_store,
)


def _item() -> CommittedActionItem:
    return CommittedActionItem(
        action_id=uuid4(),
        invocation_id=uuid4(),
        conversation_id="chat-1",
        executor_item_id="call-1",
        observation_digest=f"sha256:{'a' * 64}",
    )


def _state() -> dict:
    return {"state": {"context": []}}


def _hint(item: CommittedActionItem) -> Msg:
    return Msg(
        name="system",
        role="assistant",
        content=[TextBlock(type="text", text="private result")],
        metadata={
            COMMITTED_ACTION_ITEM_METADATA_KEY: item.model_dump(mode="json"),
        },
    )


@pytest.mark.asyncio
async def test_background_snapshot_repairs_and_dispatches_once(
    tmp_path: Path,
) -> None:
    item = _item()
    checkpoint = build_background_action_checkpoint(
        committed_item=item,
        source_submission_id=uuid4(),
        correlation_id=uuid4(),
        agent_id="default",
        recovery_cycle=1,
    )
    snapshot = build_background_action_snapshot(
        _state(),
        _hint(item),
        item,
    )
    contexts = lite_background_action_context_store(tmp_path)
    outbox = lite_background_action_continuation_store(tmp_path)

    await contexts.save(checkpoint, snapshot)
    [repair_checkpoint] = await contexts.list_checkpoints(
        agent_id="default",
    )
    continuation = await outbox.defer(repair_checkpoint)
    continuation = await outbox.transition(
        continuation.continuation_id,
        BackgroundActionContinuationStatus.READY,
    )
    submission_id = uuid4()
    calls = 0

    async def enqueue(_continuation):
        nonlocal calls
        calls += 1
        return submission_id

    first = await outbox.dispatch(continuation.continuation_id, enqueue)
    second = await outbox.dispatch(continuation.continuation_id, enqueue)

    assert first.status is BackgroundActionContinuationStatus.DISPATCHED
    assert first.submission_id == submission_id
    assert second == first
    assert calls == 1
    context_path = next(
        tmp_path.glob(".qwenpaw/lite/background-action-contexts/*.json"),
    )
    assert stat.S_IMODE(context_path.stat().st_mode) == 0o600
    assert "private result" in context_path.read_text(encoding="utf-8")
    outbox_path = next(
        tmp_path.glob(
            ".qwenpaw/lite/background-action-continuations/*.json",
        ),
    )
    assert "private result" not in outbox_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_completion_handler_commits_snapshot_before_wake(
    tmp_path: Path,
) -> None:
    item = _item()

    class Dispatcher:
        wakes = 0

        def wake_resource_waits(self) -> None:
            self.wakes += 1

    class Control:
        @staticmethod
        async def get_submission(_submission_id):
            return SimpleNamespace(status=SubmissionStatus.SUCCEEDED)

    dispatcher = Dispatcher()
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        submission_dispatcher=dispatcher,
        invocation_control=Control(),
    )
    handler = BackgroundActionCompletionHandler(
        workspace=workspace,
        source_submission_id=uuid4(),
        invocation_id=item.invocation_id,
        conversation_id="chat-1",
        correlation_id=uuid4(),
        agent_id="default",
        recovery_cycle=1,
    )
    response = ToolResponse(
        id="call-1",
        state=ToolResultState.SUCCESS,
        metadata={
            COMMITTED_ACTION_ITEM_METADATA_KEY: item.model_dump(mode="json"),
        },
    )
    entry = SimpleNamespace(
        final_response=response,
        ctx=SimpleNamespace(
            tool_call_id="call-1",
            tool_name="slow_tool",
        ),
        end_state="success",
    )

    await handler(entry, _state())

    [continuation] = (
        await lite_background_action_continuation_store(
            tmp_path,
        ).list_pending(agent_id="default")
    )
    assert continuation.checkpoint.committed_item == item
    assert continuation.status is (
        BackgroundActionContinuationStatus.WAITING_SOURCE
    )
    assert dispatcher.wakes == 2


@pytest.mark.asyncio
async def test_background_action_stops_after_recovery_budget(
    tmp_path: Path,
) -> None:
    item = _item()
    checkpoint = build_background_action_checkpoint(
        committed_item=item,
        source_submission_id=uuid4(),
        correlation_id=uuid4(),
        agent_id="default",
        recovery_cycle=3,
    )
    continuation = await lite_background_action_continuation_store(
        tmp_path,
    ).defer(checkpoint)

    assert continuation.status is (
        BackgroundActionContinuationStatus.RECOVERY_EXHAUSTED
    )
