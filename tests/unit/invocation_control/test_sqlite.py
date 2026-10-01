# -*- coding: utf-8 -*-
"""Tests for the SQLite Lite invocation-control adapter."""

import asyncio
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from qwenpaw.invocation_control import (
    ControlIdempotencyConflictError,
    QueueCommandConflictError,
    QueueRevisionConflictError,
    SQLiteInvocationControl,
)
from qwenpaw.kernel import (
    ControlCommand,
    ControlCommandKind,
    ControlCommandStatus,
    InvalidSubmissionTransition,
    SubmissionStatus,
    TurnSubmissionRequest,
)


def _request(
    key: str,
    *,
    content: str = "hello",
) -> TurnSubmissionRequest:
    return TurnSubmissionRequest(
        agent_id="default",
        conversation_id="chat-1",
        content=content,
        idempotency_key=key,
    )


@pytest.mark.asyncio
async def test_submit_assigns_server_sequence_and_revision(
    tmp_path: Path,
) -> None:
    control = SQLiteInvocationControl(tmp_path / "control.sqlite3")

    first = await control.submit(_request("one"), expected_revision=0)
    second = await control.submit(_request("two"), expected_revision=1)
    queue = await control.read_queue(
        agent_id="default",
        conversation_id="chat-1",
    )

    assert first.revision == 1
    assert second.revision == 2
    assert [item.sequence for item in queue.submissions] == [1, 2]
    assert [item.queue_position for item in queue.submissions] == [1, 2]
    assert queue.revision == 2


@pytest.mark.asyncio
async def test_concurrent_submit_without_revision_is_server_sequenced(
    tmp_path: Path,
) -> None:
    control = SQLiteInvocationControl(tmp_path / "control.sqlite3")

    first, second = await asyncio.gather(
        control.submit(_request("tab-a")),
        control.submit(_request("tab-b")),
    )
    queue = await control.read_queue(
        agent_id="default",
        conversation_id="chat-1",
    )

    assert {first.revision, second.revision} == {1, 2}
    assert [item.sequence for item in queue.submissions] == [1, 2]
    assert {item.idempotency_key for item in queue.submissions} == {
        "tab-a",
        "tab-b",
    }


@pytest.mark.asyncio
async def test_submit_replays_same_idempotent_receipt(
    tmp_path: Path,
) -> None:
    control = SQLiteInvocationControl(tmp_path / "control.sqlite3")
    first = await control.submit(_request("same"), expected_revision=0)
    replay = await control.submit(_request("same"), expected_revision=999)

    assert replay == first
    queue = await control.read_queue(
        agent_id="default",
        conversation_id="chat-1",
    )
    assert len(queue.submissions) == 1


@pytest.mark.asyncio
async def test_schema_v4_migrates_identity_and_queue_position(
    tmp_path: Path,
) -> None:
    path = tmp_path / "control.sqlite3"
    control = SQLiteInvocationControl(path)
    await control.submit(_request("one"), expected_revision=0)

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT submission_id, request_json, model_json "
            "FROM turn_submissions",
        ).fetchone()
        assert row is not None
        request_payload = json.loads(row[1])
        model_payload = json.loads(row[2])
        request_payload["session_id"] = "console:user-1"
        model_payload["session_id"] = "console:user-1"
        connection.execute(
            "UPDATE turn_submissions SET request_json = ?, model_json = ? "
            "WHERE submission_id = ?",
            (
                json.dumps(request_payload),
                json.dumps(model_payload),
                row[0],
            ),
        )
        connection.execute("PRAGMA user_version = 2")

    migrated = SQLiteInvocationControl(path)
    queue = await migrated.read_queue(
        agent_id="default",
        conversation_id="chat-1",
    )

    assert len(queue.submissions) == 1
    assert "session_id" not in queue.submissions[0].model_dump()
    assert queue.submissions[0].queue_position == 1
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT request_json, model_json FROM turn_submissions",
        ).fetchone()
    assert row is not None
    assert "session_id" not in json.loads(row[0])
    assert "session_id" not in json.loads(row[1])


@pytest.mark.asyncio
async def test_submit_rejects_idempotency_content_conflict(
    tmp_path: Path,
) -> None:
    control = SQLiteInvocationControl(tmp_path / "control.sqlite3")
    await control.submit(_request("same"), expected_revision=0)

    with pytest.raises(ControlIdempotencyConflictError):
        await control.submit(
            _request("same", content="different"),
            expected_revision=1,
        )


@pytest.mark.asyncio
async def test_submit_rejects_stale_queue_revision(tmp_path: Path) -> None:
    control = SQLiteInvocationControl(tmp_path / "control.sqlite3")
    await control.submit(_request("one"), expected_revision=0)

    with pytest.raises(QueueRevisionConflictError) as captured:
        await control.submit(_request("two"), expected_revision=0)

    assert captured.value.actual == 1


@pytest.mark.asyncio
async def test_control_command_is_durable_and_replay_safe(
    tmp_path: Path,
) -> None:
    path = tmp_path / "control.sqlite3"
    control = SQLiteInvocationControl(path)
    submitted = await control.submit(
        _request("one"),
        expected_revision=0,
    )
    command = ControlCommand(
        kind=ControlCommandKind.CANCEL_QUEUED,
        agent_id="default",
        conversation_id="chat-1",
        idempotency_key="cancel-one",
        expected_revision=1,
        target_submission_id=submitted.submission_id,
    )

    receipt = await control.control(command)
    restarted = SQLiteInvocationControl(path)
    replay = await restarted.control(
        command.model_copy(
            update={
                "command_id": "4e5ccf0f-201f-4ac0-bc3c-3184dfe7d47a",
            },
        ),
    )
    accepted = await restarted.list_accepted_commands(
        agent_id="default",
        conversation_id="chat-1",
    )
    history = await restarted.list_for_conversation(
        agent_id="default",
        conversation_id="chat-1",
    )

    assert receipt.status is ControlCommandStatus.APPLIED
    assert receipt.revision == 2
    assert replay == receipt
    assert accepted == ()
    assert len(history) == 1
    assert history[0].command == command
    assert history[0].receipt == receipt
    queue = await restarted.read_queue(
        agent_id="default",
        conversation_id="chat-1",
    )
    assert queue.submissions == ()


@pytest.mark.asyncio
async def test_interrupt_rejects_missing_active_invocation(
    tmp_path: Path,
) -> None:
    control = SQLiteInvocationControl(tmp_path / "control.sqlite3")
    command = ControlCommand(
        kind=ControlCommandKind.INTERRUPT_CURRENT,
        agent_id="default",
        conversation_id="chat-1",
        idempotency_key="interrupt-one",
        expected_revision=0,
        target_invocation_id="34fb55b2-6280-4aba-b4b0-05c43b34f6fb",
    )

    with pytest.raises(QueueCommandConflictError, match="no active turn"):
        await control.control(command)


@pytest.mark.asyncio
async def test_claim_next_uses_priority_then_prevents_second_active(
    tmp_path: Path,
) -> None:
    control = SQLiteInvocationControl(tmp_path / "control.sqlite3")
    await control.submit(_request("normal"), expected_revision=0)
    urgent = TurnSubmissionRequest(
        agent_id="default",
        conversation_id="chat-1",
        priority=0,
        content="urgent",
        idempotency_key="urgent",
    )
    await control.submit(urgent, expected_revision=1)
    invocation_id = uuid4()

    claimed = await control.claim_next(
        agent_id="default",
        conversation_id="chat-1",
        invocation_id=invocation_id,
    )
    blocked = await control.claim_next(
        agent_id="default",
        conversation_id="chat-1",
        invocation_id=uuid4(),
    )

    assert claimed is not None
    assert claimed.content == "urgent"
    assert claimed.status is SubmissionStatus.ADMITTED
    assert claimed.invocation_id == invocation_id
    assert blocked is None


@pytest.mark.asyncio
async def test_runtime_transitions_are_durable_and_revision_guarded(
    tmp_path: Path,
) -> None:
    path = tmp_path / "control.sqlite3"
    control = SQLiteInvocationControl(path)
    await control.submit(_request("one"), expected_revision=0)
    invocation_id = uuid4()
    claimed = await control.claim_next(
        agent_id="default",
        conversation_id="chat-1",
        invocation_id=invocation_id,
    )
    assert claimed is not None

    running = await control.transition_submission(
        claimed.submission_id,
        invocation_id=invocation_id,
        target=SubmissionStatus.RUNNING,
        expected_revision=claimed.revision,
    )
    interrupted = await control.transition_submission(
        running.submission_id,
        invocation_id=invocation_id,
        target=SubmissionStatus.INTERRUPTING,
        expected_revision=running.revision,
    )
    terminal = await control.transition_submission(
        interrupted.submission_id,
        invocation_id=invocation_id,
        target=SubmissionStatus.INTERRUPTED,
        expected_revision=interrupted.revision,
    )
    queue = await SQLiteInvocationControl(path).read_queue(
        agent_id="default",
        conversation_id="chat-1",
    )

    assert terminal.status is SubmissionStatus.INTERRUPTED
    assert queue.active_submission_id is None
    assert queue.submissions == ()

    with pytest.raises(InvalidSubmissionTransition):
        await control.transition_submission(
            terminal.submission_id,
            invocation_id=invocation_id,
            target=SubmissionStatus.RUNNING,
            expected_revision=terminal.revision,
        )


@pytest.mark.asyncio
async def test_recovery_interrupts_orphan_and_resolves_pending_controls(
    tmp_path: Path,
) -> None:
    control = SQLiteInvocationControl(tmp_path / "control.sqlite3")
    await control.submit(_request("active"), expected_revision=0)
    queued = await control.submit(_request("next"), expected_revision=1)
    invocation_id = uuid4()
    admitted = await control.claim_next(
        agent_id="default",
        conversation_id="chat-1",
        invocation_id=invocation_id,
    )
    assert admitted is not None
    running = await control.transition_submission(
        admitted.submission_id,
        invocation_id=invocation_id,
        target=SubmissionStatus.RUNNING,
        expected_revision=admitted.revision,
    )
    steer = ControlCommand(
        kind=ControlCommandKind.STEER,
        agent_id="default",
        conversation_id="chat-1",
        idempotency_key="steer-orphan",
        expected_revision=4,
        target_invocation_id=invocation_id,
        instruction="use the recovered path",
    )
    interrupt = ControlCommand(
        kind=ControlCommandKind.INTERRUPT_CURRENT,
        agent_id="default",
        conversation_id="chat-1",
        idempotency_key="interrupt-orphan",
        expected_revision=5,
        target_invocation_id=invocation_id,
    )
    await control.control(steer)
    await control.control(interrupt)

    recovered = await control.recover_orphaned_submissions(
        agent_id="default",
    )
    replay = await control.recover_orphaned_submissions(
        agent_id="default",
    )
    current = await control.get_submission(running.submission_id)
    dispatchable = await control.list_dispatchable(agent_id="default")
    steer_result = await control.lookup_control(
        agent_id="default",
        conversation_id="chat-1",
        idempotency_key="steer-orphan",
    )
    interrupt_result = await control.lookup_control(
        agent_id="default",
        conversation_id="chat-1",
        idempotency_key="interrupt-orphan",
    )

    assert len(recovered) == 1
    assert replay == ()
    assert current is not None
    assert current.status is SubmissionStatus.INTERRUPTED
    assert [item.submission_id for item in dispatchable] == [
        queued.submission_id,
    ]
    assert steer_result is not None
    assert steer_result[1].status is ControlCommandStatus.REJECTED
    assert "restart" in steer_result[1].detail
    assert interrupt_result is not None
    assert interrupt_result[1].status is ControlCommandStatus.APPLIED
    assert "recovery" in interrupt_result[1].detail
    assert (
        await control.list_accepted_commands(
            agent_id="default",
            conversation_id="chat-1",
        )
        == ()
    )


@pytest.mark.asyncio
async def test_runtime_resolves_control_command_once(tmp_path: Path) -> None:
    control = SQLiteInvocationControl(tmp_path / "control.sqlite3")
    await control.submit(_request("active"), expected_revision=0)
    invocation_id = uuid4()
    claimed = await control.claim_next(
        agent_id="default",
        conversation_id="chat-1",
        invocation_id=invocation_id,
    )
    assert claimed is not None
    running = await control.transition_submission(
        claimed.submission_id,
        invocation_id=invocation_id,
        target=SubmissionStatus.RUNNING,
        expected_revision=claimed.revision,
    )
    command = ControlCommand(
        kind=ControlCommandKind.STOP_AND_CLEAR,
        agent_id="default",
        conversation_id="chat-1",
        idempotency_key="stop-all",
        expected_revision=3,
        target_invocation_id=running.invocation_id,
    )
    accepted = await control.control(command)

    applied = await control.resolve_command(
        command.command_id,
        status=ControlCommandStatus.APPLIED,
        detail="cancelled before admission",
    )
    replay = await control.resolve_command(
        command.command_id,
        status=ControlCommandStatus.APPLIED,
        detail="cancelled before admission",
    )
    pending = await control.list_accepted_commands(
        agent_id="default",
        conversation_id="chat-1",
    )

    assert applied.status is ControlCommandStatus.APPLIED
    assert applied.revision == accepted.revision + 1
    assert replay == applied
    assert pending == ()


@pytest.mark.asyncio
async def test_stop_and_clear_cancels_queue_without_active_turn(
    tmp_path: Path,
) -> None:
    control = SQLiteInvocationControl(tmp_path / "control.sqlite3")
    await control.submit(_request("one"), expected_revision=0)
    await control.submit(_request("two"), expected_revision=1)

    receipt = await control.control(
        ControlCommand(
            kind=ControlCommandKind.STOP_AND_CLEAR,
            agent_id="default",
            conversation_id="chat-1",
            idempotency_key="stop-all",
            expected_revision=2,
        ),
    )
    queue = await control.read_queue(
        agent_id="default",
        conversation_id="chat-1",
    )

    assert receipt.status is ControlCommandStatus.APPLIED
    assert "cancelled=2" in receipt.detail
    assert queue.submissions == ()


@pytest.mark.asyncio
async def test_stop_and_clear_requires_exact_active_invocation(
    tmp_path: Path,
) -> None:
    control = SQLiteInvocationControl(tmp_path / "control.sqlite3")
    await control.submit(_request("one"), expected_revision=0)
    invocation_id = uuid4()
    claimed = await control.claim_next(
        agent_id="default",
        conversation_id="chat-1",
        invocation_id=invocation_id,
    )
    assert claimed is not None

    with pytest.raises(
        QueueCommandConflictError,
        match="capture the active invocation",
    ):
        await control.control(
            ControlCommand(
                kind=ControlCommandKind.STOP_AND_CLEAR,
                agent_id="default",
                conversation_id="chat-1",
                idempotency_key="stop-all",
                expected_revision=2,
            ),
        )


@pytest.mark.asyncio
async def test_reorder_changes_dispatch_order_not_audit_sequence(
    tmp_path: Path,
) -> None:
    control = SQLiteInvocationControl(tmp_path / "control.sqlite3")
    first_receipt = await control.submit(
        _request("one", content="first"),
        expected_revision=0,
    )
    second_receipt = await control.submit(
        _request("two", content="second"),
        expected_revision=1,
    )
    assert first_receipt.submission_id is not None
    assert second_receipt.submission_id is not None
    command = ControlCommand(
        kind=ControlCommandKind.REORDER,
        agent_id="default",
        conversation_id="chat-1",
        idempotency_key="reverse-queue",
        expected_revision=2,
        ordered_submission_ids=(
            second_receipt.submission_id,
            first_receipt.submission_id,
        ),
    )

    receipt = await control.control(command)
    queue = await control.read_queue(
        agent_id="default",
        conversation_id="chat-1",
    )
    claimed = await control.claim_next(
        agent_id="default",
        conversation_id="chat-1",
        invocation_id=uuid4(),
    )

    assert receipt.status is ControlCommandStatus.APPLIED
    assert [item.content for item in queue.submissions] == ["second", "first"]
    assert [item.sequence for item in queue.submissions] == [2, 1]
    assert [item.queue_position for item in queue.submissions] == [1, 2]
    assert claimed is not None
    assert claimed.content == "second"
