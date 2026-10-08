# -*- coding: utf-8 -*-
"""Tests for immutable private model-step context snapshots."""

import stat
from uuid import uuid4

import pytest

from qwenpaw.kernel import ModelStepContextCheckpoint
from qwenpaw.runtime.model_step_contexts import (
    ModelStepContextConflictError,
    context_has_terminal_tool_results,
    lite_model_step_context_store,
)


def _checkpoint() -> ModelStepContextCheckpoint:
    invocation_id = uuid4()
    return ModelStepContextCheckpoint(
        checkpoint_id=uuid4(),
        continuation_id=uuid4(),
        invocation_id=invocation_id,
        conversation_id="chat-1",
        source_submission_id=uuid4(),
        action_evidence_digest=f"sha256:{'a' * 64}",
        action_count=1,
    )


def _state(
    call_id: str = "call-1",
    action_id: str = "action-1",
) -> dict:
    return {
        "state": {
            "context": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_result",
                            "id": call_id,
                            "name": "write_file",
                            "state": "success",
                            "output": "private result",
                            "metadata": {
                                "qwenpaw_action_id": action_id,
                            },
                        },
                    ],
                },
            ],
        },
    }


@pytest.mark.asyncio
async def test_context_snapshot_is_immutable_private_and_round_trips(
    tmp_path,
) -> None:
    store = lite_model_step_context_store(tmp_path)
    checkpoint = _checkpoint()
    state = _state()

    persisted = await store.save(checkpoint, state)
    replayed = await store.save(
        checkpoint.model_copy(
            update={"created_at": checkpoint.created_at},
        ),
        state,
    )
    loaded_checkpoint, loaded_state = await store.load(
        checkpoint.checkpoint_id,
    )

    assert persisted == checkpoint
    assert replayed == checkpoint
    assert loaded_checkpoint == checkpoint
    assert loaded_state == state
    assert context_has_terminal_tool_results(
        state,
        (("call-1", "action-1"),),
    )
    assert not context_has_terminal_tool_results(
        state,
        (("call-1", "different-action"),),
    )
    path = next(
        tmp_path.glob(".qwenpaw/lite/model-step-contexts/*.json"),
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_context_snapshot_rejects_conflicting_state(tmp_path) -> None:
    store = lite_model_step_context_store(tmp_path)
    checkpoint = _checkpoint()
    await store.save(checkpoint, _state())

    with pytest.raises(ModelStepContextConflictError):
        await store.save(checkpoint, _state("call-2"))
