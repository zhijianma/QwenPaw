# -*- coding: utf-8 -*-
"""Contract tests for provider-neutral Harness Action evidence."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from qwenpaw.harnesses.actions import (
    HARNESS_ACTION_TRACKER_KEY,
    HarnessActionTracker,
    begin_harness_approval_action,
)
from qwenpaw.harnesses.events import HarnessEvent, HarnessEventKind
from qwenpaw.kernel import (
    ActionRetryDisposition,
    ActionStatus,
    InvocationScope,
    SideEffectStatus,
)
from qwenpaw.runtime.actions import RuntimeActionRecorder, lite_action_store
from qwenpaw.runtime.environments import FilesystemEnvironmentStore
from qwenpaw.runtime.harness_environments import (
    RuntimeHarnessEnvironmentManager,
)


@pytest.mark.asyncio
async def test_approval_creates_action_before_provider_completion(
    tmp_path: Path,
) -> None:
    invocation_id = uuid4()
    approval_id = uuid4()
    scope = InvocationScope(
        invocation_id=invocation_id,
        agent_id="default",
        conversation_id="chat-spec-1",
        session_id="chat-1",
        root_agent_id="default",
        root_session_id="chat-1",
        workspace_dir=str(tmp_path),
        registry_generation=7,
    )
    store = lite_action_store(tmp_path)
    tracker = HarnessActionTracker(
        backend="codex",
        scope=scope,
        recorder=RuntimeActionRecorder(scope, store),
    )
    resolution = await RuntimeHarnessEnvironmentManager(
        FilesystemEnvironmentStore(tmp_path),
    ).resolve(
        "codex",
        tmp_path,
        {"sandbox": "workspace-write"},
        invocation_id=invocation_id,
        conversation_id="chat-spec-1",
    )
    tracker.bind_environment(resolution)
    request_context = {HARNESS_ACTION_TRACKER_KEY: tracker}

    await begin_harness_approval_action(
        request_context,
        backend="codex",
        item_id="item-approval",
        tool_name="shell",
        arguments={"command": "echo private-value"},
        provider_type="commandExecution",
        approval_id=approval_id,
    )
    [pending] = await store.list_for_conversation("chat-spec-1")

    assert pending.result is None
    assert pending.request.policy_decision == "approval_required"
    assert pending.request.approval_id is None
    assert pending.request.redacted_arguments == {
        "command": "[CONTENT OMITTED]",
    }
    assert pending.approval_links[0].approval_id == approval_id

    await tracker.complete_event(
        HarnessEvent(
            kind=HarnessEventKind.TOOL_COMPLETED,
            item_id="item-approval",
            tool_name="shell",
            data={
                "provider_type": "commandExecution",
                "exit_code": 0,
            },
        ),
    )
    [completed] = await store.list_for_conversation("chat-spec-1")

    assert completed.result is not None
    assert completed.result.status is ActionStatus.SUCCEEDED
    assert completed.result.approval_ids == (approval_id,)
    committed = tracker.committed_items["item-approval"]
    assert committed.action_id == completed.request.action_id
    assert committed.invocation_id == invocation_id
    assert committed.conversation_id == "chat-spec-1"
    assert committed.executor_item_id == "item-approval"
    assert committed.observation_digest == (
        completed.result.observation_digest
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "tool_name",
        "provider_type",
        "expected_retryable",
        "expected_disposition",
        "expected_side_effect",
    ),
    [
        (
            "read_file",
            "fileRead",
            True,
            ActionRetryDisposition.RETRY_FROM_NEW_ACTION,
            None,
        ),
        (
            "shell",
            "commandExecution",
            False,
            ActionRetryDisposition.RECONCILE_REQUIRED,
            SideEffectStatus.UNCERTAIN,
        ),
    ],
)
async def test_provider_retry_hint_is_constrained_by_host_policy(
    tmp_path: Path,
    tool_name: str,
    provider_type: str,
    expected_retryable: bool,
    expected_disposition: ActionRetryDisposition,
    expected_side_effect: SideEffectStatus | None,
) -> None:
    invocation_id = uuid4()
    conversation_id = f"chat-retry-{tool_name}"
    scope = InvocationScope(
        invocation_id=invocation_id,
        agent_id="default",
        conversation_id=conversation_id,
        session_id="chat-1",
        root_agent_id="default",
        root_session_id="chat-1",
        workspace_dir=str(tmp_path),
        registry_generation=7,
    )
    store = lite_action_store(tmp_path)
    tracker = HarnessActionTracker(
        backend="codex",
        scope=scope,
        recorder=RuntimeActionRecorder(scope, store),
    )
    resolution = await RuntimeHarnessEnvironmentManager(
        FilesystemEnvironmentStore(tmp_path),
    ).resolve(
        "codex",
        tmp_path,
        {"sandbox": "workspace-write"},
        invocation_id=invocation_id,
        conversation_id=conversation_id,
    )
    tracker.bind_environment(resolution)

    await tracker.begin_event(
        HarnessEvent(
            kind=HarnessEventKind.TOOL_STARTED,
            item_id=f"item-{tool_name}",
            tool_name=tool_name,
            data={"provider_type": provider_type},
        ),
    )
    await tracker.complete_event(
        HarnessEvent(
            kind=HarnessEventKind.TOOL_COMPLETED,
            item_id=f"item-{tool_name}",
            tool_name=tool_name,
            data={
                "provider_type": provider_type,
                "status": "failed",
                "retryable": True,
            },
        ),
    )

    [record] = await store.list_for_conversation(conversation_id)
    assert record.result is not None
    assert record.result.retryable is expected_retryable
    assert record.result.side_effect_status is expected_side_effect
    assert record.result.retry_decision is not None
    assert record.result.retry_decision.disposition is expected_disposition
