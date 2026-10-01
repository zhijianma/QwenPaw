# -*- coding: utf-8 -*-
"""Tests for workspace-owned live invocation control."""

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from qwenpaw.invocation_control import (
    ControlIdempotencyConflictError,
    InvocationControlService,
    QueueCommandConflictError,
    RuntimeInterruptSession,
    SQLiteInvocationControl,
)
from qwenpaw.kernel import (
    ControlCommand,
    ControlCommandKind,
    ControlCommandStatus,
    ControlHistoryPort,
    SteerSafePoint,
    SubmissionStatus,
    TurnSubmissionRequest,
)


def _steer(
    invocation_id: UUID,
    *,
    expected_revision: int = 1,
) -> ControlCommand:
    return ControlCommand(
        kind=ControlCommandKind.STEER,
        agent_id="default",
        conversation_id="chat-1",
        idempotency_key=str(uuid4()),
        expected_revision=expected_revision,
        target_invocation_id=invocation_id,
        instruction="Use the newly supplied constraint.",
    )


def _interrupt(
    invocation_id: UUID,
    *,
    expected_revision: int,
) -> ControlCommand:
    return ControlCommand(
        kind=ControlCommandKind.INTERRUPT_CURRENT,
        agent_id="default",
        conversation_id="chat-1",
        idempotency_key=str(uuid4()),
        expected_revision=expected_revision,
        target_invocation_id=invocation_id,
    )


async def _active_store(
    path: Path,
) -> tuple[SQLiteInvocationControl, UUID]:
    store = SQLiteInvocationControl(path)
    await store.submit(
        TurnSubmissionRequest(
            agent_id="default",
            conversation_id="chat-1",
            content="original request",
            idempotency_key="turn-1",
        ),
        expected_revision=0,
    )
    invocation_id = uuid4()
    claimed = await store.claim_next(
        agent_id="default",
        conversation_id="chat-1",
        invocation_id=invocation_id,
    )
    assert claimed is not None
    await store.transition_submission(
        claimed.submission_id,
        invocation_id=invocation_id,
        target=SubmissionStatus.RUNNING,
        expected_revision=claimed.revision,
    )
    return store, invocation_id


@pytest.mark.asyncio
async def test_session_acknowledges_only_after_context_injection() -> None:
    service = InvocationControlService()
    invocation_id = uuid4()
    session = await service.open_steering(invocation_id)
    result = await service.offer_steer(_steer(invocation_id))
    injected = []

    async def inject(delivery, safe_point):
        injected.append((delivery.instruction, safe_point))

    count = await session.apply_pending(
        SteerSafePoint.BEFORE_REASONING,
        inject,
    )

    assert count == 1
    assert injected == [
        (
            "Use the newly supplied constraint.",
            SteerSafePoint.BEFORE_REASONING,
        ),
    ]
    assert await result is SteerSafePoint.BEFORE_REASONING


@pytest.mark.asyncio
async def test_failed_injection_keeps_steer_pending() -> None:
    service = InvocationControlService()
    invocation_id = uuid4()
    session = await service.open_steering(invocation_id)
    result = await service.offer_steer(_steer(invocation_id))

    async def fail(_delivery, _safe_point):
        raise RuntimeError("context is unavailable")

    with pytest.raises(RuntimeError, match="context is unavailable"):
        await session.apply_pending(SteerSafePoint.AFTER_REASONING, fail)
    assert not result.done()

    async def inject(_delivery, _safe_point):
        return None

    count = await session.apply_pending(
        SteerSafePoint.AFTER_REASONING,
        inject,
    )
    assert count == 1
    assert await result is SteerSafePoint.AFTER_REASONING


@pytest.mark.asyncio
async def test_closing_session_rejects_unapplied_steer() -> None:
    service = InvocationControlService()
    invocation_id = uuid4()
    session = await service.open_steering(invocation_id)
    result = await service.offer_steer(_steer(invocation_id))

    await session.close(reason="runtime completed")

    with pytest.raises(RuntimeError, match="runtime completed"):
        await result


@pytest.mark.asyncio
async def test_durable_dispatch_resolves_only_after_safe_point(
    tmp_path: Path,
) -> None:
    store, invocation_id = await _active_store(tmp_path / "control.sqlite3")
    service = InvocationControlService(store=store)
    session = await service.open_steering(
        invocation_id,
        agent_id="default",
        conversation_id="chat-1",
    )
    command = _steer(invocation_id, expected_revision=3)

    accepted = await service.accept_control(command)
    history = await service.list_for_conversation(
        agent_id="default",
        conversation_id="chat-1",
    )
    pending = await store.list_accepted_commands(
        agent_id="default",
        conversation_id="chat-1",
    )
    assert accepted.status is ControlCommandStatus.ACCEPTED
    assert isinstance(service, ControlHistoryPort)
    assert len(history) == 1
    assert history[0].command == command
    assert history[0].receipt == accepted
    assert pending == (command,)

    async def inject(_delivery, _safe_point):
        return None

    await session.apply_pending(SteerSafePoint.BEFORE_REASONING, inject)
    await service.wait_dispatch(command.command_id)

    pending = await store.list_accepted_commands(
        agent_id="default",
        conversation_id="chat-1",
    )
    replay = await store.resolve_command(
        command.command_id,
        status=ControlCommandStatus.APPLIED,
        detail="steer applied at before_reasoning",
        safe_point=SteerSafePoint.BEFORE_REASONING,
    )
    assert pending == ()
    assert replay.status is ControlCommandStatus.APPLIED
    assert replay.applied_at_safe_point is SteerSafePoint.BEFORE_REASONING


@pytest.mark.asyncio
async def test_binding_recovers_accepted_steer(
    tmp_path: Path,
) -> None:
    store, invocation_id = await _active_store(tmp_path / "control.sqlite3")
    service = InvocationControlService(store=store)
    command = _steer(invocation_id, expected_revision=3)

    await service.accept_control(command)
    await service.wait_dispatch(command.command_id)
    assert await store.list_accepted_commands(
        agent_id="default",
        conversation_id="chat-1",
    ) == (command,)

    session = await service.open_steering(
        invocation_id,
        agent_id="default",
        conversation_id="chat-1",
    )

    async def inject(_delivery, _safe_point):
        return None

    await session.apply_pending(SteerSafePoint.AFTER_REASONING, inject)
    await service.wait_dispatch(command.command_id)

    assert (
        await store.list_accepted_commands(
            agent_id="default",
            conversation_id="chat-1",
        )
        == ()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status",
    [
        SubmissionStatus.SUCCEEDED,
        SubmissionStatus.FAILED,
        SubmissionStatus.INTERRUPTED,
    ],
)
async def test_runtime_lease_commits_terminal_status(
    tmp_path: Path,
    terminal_status: SubmissionStatus,
) -> None:
    store = SQLiteInvocationControl(tmp_path / "control.sqlite3")
    service = InvocationControlService(store=store)
    request = TurnSubmissionRequest(
        agent_id="default",
        conversation_id="chat-spec-1",
        content="run this turn",
        idempotency_key="message-1",
    )

    lease = await service.begin_turn(request, invocation_id=uuid4())
    terminal = await service.finish_turn(lease, terminal_status)

    assert terminal.status is terminal_status
    assert lease.submission == terminal
    projection = await store.read_queue(
        agent_id="default",
        conversation_id="chat-spec-1",
    )
    assert projection.active_submission_id is None


@pytest.mark.asyncio
async def test_interrupt_cancels_children_before_runtime_task(
    tmp_path: Path,
) -> None:
    service = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    invocation_id = uuid4()
    lease = await service.begin_turn(
        TurnSubmissionRequest(
            agent_id="default",
            conversation_id="chat-1",
            content="run this turn",
            idempotency_key="message-1",
        ),
        invocation_id=invocation_id,
    )
    events: list[str] = []
    binding_holder: dict[str, RuntimeInterruptSession] = {}

    async def runtime_task() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await binding_holder["binding"].close()
            await service.finish_turn(
                lease,
                SubmissionStatus.INTERRUPTED,
            )
            raise
        finally:
            events.append("runtime")

    async def cancel_children() -> int:
        events.append("children")
        return 2

    task = asyncio.create_task(runtime_task())
    binding = await service.bind_interrupt(
        invocation_id,
        task,
        lease=lease,
        agent_id="default",
        conversation_id="chat-1",
        cancel_children=cancel_children,
    )
    binding_holder["binding"] = binding
    receipt = await service.interrupt_current(
        agent_id="default",
        conversation_id="chat-1",
        idempotency_key="interrupt-1",
    )

    assert receipt is not None
    assert receipt.status is ControlCommandStatus.APPLIED
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == ["children", "runtime"]
    assert "foreground children cancelled=2" in receipt.detail

    assert lease.submission.status is SubmissionStatus.INTERRUPTED


@pytest.mark.asyncio
async def test_interrupt_current_returns_none_without_active_turn(
    tmp_path: Path,
) -> None:
    service = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )

    receipt = await service.interrupt_current(
        agent_id="default",
        conversation_id="chat-1",
        idempotency_key="interrupt-1",
    )

    assert receipt is None


@pytest.mark.asyncio
async def test_late_runtime_binding_recovers_accepted_interrupt(
    tmp_path: Path,
) -> None:
    service = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    invocation_id = uuid4()
    lease = await service.begin_turn(
        TurnSubmissionRequest(
            agent_id="default",
            conversation_id="chat-1",
            content="run this turn",
            idempotency_key="message-1",
        ),
        invocation_id=invocation_id,
    )
    projection = await service.read_queue(
        agent_id="default",
        conversation_id="chat-1",
    )
    command = _interrupt(
        invocation_id,
        expected_revision=projection.revision,
    )

    accepted = await service.accept_control(command)
    assert accepted.status is ControlCommandStatus.ACCEPTED

    async def runtime_task() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await service.finish_turn(
                lease,
                SubmissionStatus.INTERRUPTED,
            )
            raise

    task = asyncio.create_task(runtime_task())
    binding = await service.bind_interrupt(
        invocation_id,
        task,
        lease=lease,
        agent_id="default",
        conversation_id="chat-1",
    )

    with pytest.raises(asyncio.CancelledError):
        await task
    replay = await service.accept_control(command)
    assert replay.status is ControlCommandStatus.APPLIED
    assert lease.submission.status is SubmissionStatus.INTERRUPTED
    await binding.close()


@pytest.mark.asyncio
async def test_runtime_never_executes_new_input_as_older_queued_turn(
    tmp_path: Path,
) -> None:
    store = SQLiteInvocationControl(tmp_path / "control.sqlite3")
    service = InvocationControlService(store=store)
    await store.submit(
        TurnSubmissionRequest(
            agent_id="default",
            conversation_id="chat-spec-1",
            content="older queued input",
            idempotency_key="message-old",
        ),
        expected_revision=0,
    )
    current = TurnSubmissionRequest(
        agent_id="default",
        conversation_id="chat-spec-1",
        content="current HTTP input",
        idempotency_key="message-current",
    )

    with pytest.raises(
        QueueCommandConflictError,
        match="older queued turn",
    ):
        await service.begin_turn(current, invocation_id=uuid4())

    projection = await store.read_queue(
        agent_id="default",
        conversation_id="chat-spec-1",
    )
    assert projection.active_submission_id is None
    assert [item.content for item in projection.submissions] == [
        "older queued input",
        "current HTTP input",
    ]


@pytest.mark.asyncio
async def test_queue_mutation_helpers_share_durable_protocol(
    tmp_path: Path,
) -> None:
    store = SQLiteInvocationControl(tmp_path / "control.sqlite3")
    service = InvocationControlService(store=store)
    first = await store.submit(
        TurnSubmissionRequest(
            agent_id="default",
            conversation_id="chat-1",
            content="first",
            idempotency_key="turn-1",
        ),
        expected_revision=0,
    )
    second = await store.submit(
        TurnSubmissionRequest(
            agent_id="default",
            conversation_id="chat-1",
            content="second",
            idempotency_key="turn-2",
        ),
        expected_revision=1,
    )
    assert first.submission_id is not None
    assert second.submission_id is not None

    reordered = await service.reorder_queue(
        agent_id="default",
        conversation_id="chat-1",
        ordered_submission_ids=(
            second.submission_id,
            first.submission_id,
        ),
        idempotency_key="reorder-1",
    )
    cancelled = await service.cancel_queued(
        agent_id="default",
        conversation_id="chat-1",
        submission_id=first.submission_id,
        idempotency_key="cancel-1",
    )
    reordered_replay = await service.reorder_queue(
        agent_id="default",
        conversation_id="chat-1",
        ordered_submission_ids=(
            second.submission_id,
            first.submission_id,
        ),
        idempotency_key="reorder-1",
    )
    cancelled_replay = await service.cancel_queued(
        agent_id="default",
        conversation_id="chat-1",
        submission_id=first.submission_id,
        idempotency_key="cancel-1",
    )
    queue = await service.read_queue(
        agent_id="default",
        conversation_id="chat-1",
    )

    assert reordered.status is ControlCommandStatus.APPLIED
    assert cancelled.status is ControlCommandStatus.APPLIED
    assert reordered_replay == reordered
    assert cancelled_replay == cancelled
    assert [item.submission_id for item in queue.submissions] == [
        second.submission_id,
    ]

    with pytest.raises(ControlIdempotencyConflictError):
        await service.cancel_queued(
            agent_id="default",
            conversation_id="chat-1",
            submission_id=second.submission_id,
            idempotency_key="cancel-1",
        )


@pytest.mark.asyncio
async def test_stop_and_clear_cancels_queue_then_runtime(
    tmp_path: Path,
) -> None:
    store = SQLiteInvocationControl(tmp_path / "control.sqlite3")
    service = InvocationControlService(store=store)
    invocation_id = uuid4()
    lease = await service.begin_turn(
        TurnSubmissionRequest(
            agent_id="default",
            conversation_id="chat-1",
            content="active",
            idempotency_key="turn-active",
        ),
        invocation_id=invocation_id,
    )
    projection = await service.read_queue(
        agent_id="default",
        conversation_id="chat-1",
    )
    await store.submit(
        TurnSubmissionRequest(
            agent_id="default",
            conversation_id="chat-1",
            content="queued",
            idempotency_key="turn-queued",
        ),
        expected_revision=projection.revision,
    )
    binding_holder: dict[str, RuntimeInterruptSession] = {}

    async def runtime_task() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await binding_holder["binding"].close()
            await service.finish_turn(
                lease,
                SubmissionStatus.INTERRUPTED,
            )
            raise

    task = asyncio.create_task(runtime_task())
    binding = await service.bind_interrupt(
        invocation_id,
        task,
        lease=lease,
        agent_id="default",
        conversation_id="chat-1",
    )
    binding_holder["binding"] = binding

    receipt = await service.stop_and_clear(
        agent_id="default",
        conversation_id="chat-1",
        idempotency_key="stop-all-1",
    )
    queue = await service.read_queue(
        agent_id="default",
        conversation_id="chat-1",
    )

    assert receipt.status is ControlCommandStatus.APPLIED
    assert "queued submissions cancelled" in receipt.detail
    assert queue.submissions == ()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_late_binding_recovers_stop_and_clear_for_captured_run(
    tmp_path: Path,
) -> None:
    store = SQLiteInvocationControl(tmp_path / "control.sqlite3")
    service = InvocationControlService(store=store)
    invocation_id = uuid4()
    lease = await service.begin_turn(
        TurnSubmissionRequest(
            agent_id="default",
            conversation_id="chat-1",
            content="active",
            idempotency_key="turn-active",
        ),
        invocation_id=invocation_id,
    )
    projection = await service.read_queue(
        agent_id="default",
        conversation_id="chat-1",
    )
    await store.submit(
        TurnSubmissionRequest(
            agent_id="default",
            conversation_id="chat-1",
            content="queued",
            idempotency_key="turn-queued",
        ),
        expected_revision=projection.revision,
    )

    accepted = await service.stop_and_clear(
        agent_id="default",
        conversation_id="chat-1",
        idempotency_key="stop-all-late",
    )
    assert accepted.status is ControlCommandStatus.ACCEPTED

    async def runtime_task() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await service.finish_turn(
                lease,
                SubmissionStatus.INTERRUPTED,
            )
            raise

    task = asyncio.create_task(runtime_task())
    binding = await service.bind_interrupt(
        invocation_id,
        task,
        lease=lease,
        agent_id="default",
        conversation_id="chat-1",
    )

    with pytest.raises(asyncio.CancelledError):
        await task
    replay = await service.stop_and_clear(
        agent_id="default",
        conversation_id="chat-1",
        idempotency_key="stop-all-late",
    )
    assert replay.status is ControlCommandStatus.APPLIED
    assert lease.submission.status is SubmissionStatus.INTERRUPTED
    await binding.close()
