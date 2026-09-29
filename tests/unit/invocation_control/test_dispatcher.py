# -*- coding: utf-8 -*-
"""Tests for durable workspace-owned submission dispatch."""

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from qwenpaw.invocation_control import (
    InvocationControlService,
    SQLiteInvocationControl,
    SubmissionDispatcher,
)
from qwenpaw.kernel import (
    SubmissionInputEnvelope,
    SubmissionStatus,
    TurnSubmissionRequest,
)


def _request(
    conversation_id: str,
    idempotency_key: str,
    *,
    with_envelope: bool = True,
) -> TurnSubmissionRequest:
    return TurnSubmissionRequest(
        agent_id="default",
        conversation_id=conversation_id,
        content=idempotency_key,
        input_envelope=(
            SubmissionInputEnvelope(
                kind="chat.native.v1",
                payload={"text": idempotency_key},
            )
            if with_envelope
            else None
        ),
        idempotency_key=idempotency_key,
    )


async def _wait_terminal(
    service: InvocationControlService,
    submission_ids: tuple,
) -> None:
    async with asyncio.timeout(2):
        while True:
            records = [
                await service.get_submission(submission_id)
                for submission_id in submission_ids
            ]
            if all(
                item is not None
                and item.status
                in {
                    SubmissionStatus.SUCCEEDED,
                    SubmissionStatus.FAILED,
                }
                for item in records
            ):
                return
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_dispatcher_recovers_and_preserves_conversation_fifo(
    tmp_path: Path,
) -> None:
    service = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    first = await service.enqueue_turn(_request("chat-1", "turn-1"))
    second = await service.enqueue_turn(_request("chat-1", "turn-2"))
    other = await service.enqueue_turn(_request("chat-2", "turn-other"))
    assert first.submission_id is not None
    assert second.submission_id is not None
    assert other.submission_id is not None
    observed: list[str] = []

    async def execute(submission, envelope) -> None:
        observed.append(str(envelope.payload["text"]))
        lease = await service.begin_submitted_turn(
            submission.submission_id,
            invocation_id=uuid4(),
            agent_id=submission.agent_id,
            conversation_id=submission.conversation_id,
        )
        await asyncio.sleep(0)
        await service.finish_turn(lease, SubmissionStatus.SUCCEEDED)

    dispatcher = SubmissionDispatcher(
        agent_id="default",
        control=service,
        execute=execute,
        poll_interval=0.01,
    )
    await dispatcher.start()
    await _wait_terminal(
        service,
        (first.submission_id, second.submission_id, other.submission_id),
    )

    assert observed.index("turn-1") < observed.index("turn-2")
    assert set(observed) == {"turn-1", "turn-2", "turn-other"}
    await dispatcher.stop()
    await service.close()


@pytest.mark.asyncio
async def test_dispatcher_fails_closed_when_envelope_is_missing(
    tmp_path: Path,
) -> None:
    service = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    receipt = await service.enqueue_turn(
        _request("chat-1", "missing-input", with_envelope=False),
    )
    assert receipt.submission_id is not None
    called = False

    async def execute(_submission, _envelope) -> None:
        nonlocal called
        called = True

    dispatcher = SubmissionDispatcher(
        agent_id="default",
        control=service,
        execute=execute,
        poll_interval=0.01,
    )
    await dispatcher.start()
    await _wait_terminal(service, (receipt.submission_id,))
    failed = await service.get_submission(receipt.submission_id)

    assert called is False
    assert failed is not None
    assert failed.status is SubmissionStatus.FAILED
    await dispatcher.stop()
    await service.close()


@pytest.mark.asyncio
async def test_dispatcher_interrupts_orphan_before_running_next_turn(
    tmp_path: Path,
) -> None:
    service = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    orphan = await service.enqueue_turn(_request("chat-1", "orphan"))
    next_turn = await service.enqueue_turn(_request("chat-1", "next"))
    assert orphan.submission_id is not None
    assert next_turn.submission_id is not None
    lease = await service.begin_submitted_turn(
        orphan.submission_id,
        invocation_id=uuid4(),
        agent_id="default",
        conversation_id="chat-1",
    )
    observed: list[str] = []

    async def execute(submission, envelope) -> None:
        observed.append(str(envelope.payload["text"]))
        current = await service.begin_submitted_turn(
            submission.submission_id,
            invocation_id=uuid4(),
            agent_id=submission.agent_id,
            conversation_id=submission.conversation_id,
        )
        await service.finish_turn(current, SubmissionStatus.SUCCEEDED)

    dispatcher = SubmissionDispatcher(
        agent_id="default",
        control=service,
        execute=execute,
        poll_interval=0.01,
    )
    await dispatcher.start()
    await _wait_terminal(service, (next_turn.submission_id,))
    orphaned = await service.get_submission(orphan.submission_id)

    assert lease.submission.status is SubmissionStatus.RUNNING
    assert orphaned is not None
    assert orphaned.status is SubmissionStatus.INTERRUPTED
    assert observed == ["next"]
    await dispatcher.stop()
    await service.close()
