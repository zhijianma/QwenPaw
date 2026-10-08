# -*- coding: utf-8 -*-
"""Content-safe admission tests for Harness recovery contexts."""

import stat
from uuid import uuid4

import pytest

from qwenpaw.kernel import CommittedActionItem
from qwenpaw.runtime.harness_recovery import (
    HarnessRecoveryContextConflictError,
    build_harness_recovery_checkpoint,
    lite_harness_recovery_context_store,
)


def _item() -> CommittedActionItem:
    return CommittedActionItem(
        action_id=uuid4(),
        invocation_id=uuid4(),
        conversation_id="chat-1",
        executor_item_id="tool-1",
        observation_digest=f"sha256:{'a' * 64}",
    )


def test_checkpoint_requires_provider_and_session_agreement() -> None:
    item = _item()
    source_submission_id = uuid4()
    checkpoint = build_harness_recovery_checkpoint(
        invocation_id=item.invocation_id,
        conversation_id="chat-1",
        source_submission_id=source_submission_id,
        backend="codex",
        provider_context_id="private-thread-id",
        provider_item_ids={"tool-1"},
        action_evidence_digest=f"sha256:{'b' * 64}",
        expected_items=(item,),
        session_items=(item,),
    )

    assert checkpoint is not None
    serialized = checkpoint.model_dump_json()
    assert "private-thread-id" not in serialized
    assert checkpoint.action_count == 1
    another_submission = build_harness_recovery_checkpoint(
        invocation_id=item.invocation_id,
        conversation_id="chat-1",
        source_submission_id=uuid4(),
        backend="codex",
        provider_context_id="private-thread-id",
        provider_item_ids={"tool-1"},
        action_evidence_digest=f"sha256:{'b' * 64}",
        expected_items=(item,),
        session_items=(item,),
    )
    assert another_submission is not None
    assert another_submission.provider_context_digest != (
        checkpoint.provider_context_digest
    )
    assert build_harness_recovery_checkpoint(
        invocation_id=item.invocation_id,
        conversation_id="chat-1",
        source_submission_id=uuid4(),
        backend="codex",
        provider_context_id="private-thread-id",
        provider_item_ids=set(),
        action_evidence_digest=f"sha256:{'b' * 64}",
        expected_items=(item,),
        session_items=(item,),
    ) is None
    assert build_harness_recovery_checkpoint(
        invocation_id=item.invocation_id,
        conversation_id="chat-1",
        source_submission_id=uuid4(),
        backend="codex",
        provider_context_id="private-thread-id",
        provider_item_ids={"tool-1"},
        action_evidence_digest=f"sha256:{'b' * 64}",
        expected_items=(item,),
        session_items=(),
    ) is None


@pytest.mark.asyncio
async def test_checkpoint_store_is_private_and_immutable(tmp_path) -> None:
    item = _item()
    checkpoint = build_harness_recovery_checkpoint(
        invocation_id=item.invocation_id,
        conversation_id="chat-1",
        source_submission_id=uuid4(),
        backend="qoder",
        provider_context_id="private-session-id",
        provider_item_ids={"tool-1"},
        action_evidence_digest=f"sha256:{'b' * 64}",
        expected_items=(item,),
        session_items=(item,),
    )
    assert checkpoint is not None
    store = lite_harness_recovery_context_store(tmp_path)

    saved = await store.save(checkpoint)
    loaded = await store.load(checkpoint.checkpoint_id)

    assert saved == checkpoint
    assert loaded == checkpoint
    path = next(
        tmp_path.glob(".qwenpaw/lite/harness-recovery-contexts/*.json"),
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "private-session-id" not in path.read_text(encoding="utf-8")
    with pytest.raises(HarnessRecoveryContextConflictError):
        await store.save(
            checkpoint.model_copy(
                update={"provider_item_digest": f"sha256:{'c' * 64}"},
            ),
        )
