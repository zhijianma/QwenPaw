# -*- coding: utf-8 -*-
"""Per-workspace live invocation-control infrastructure."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from ..kernel import (
    ACTIVE_SUBMISSION_STATUSES,
    ControlCommand,
    ControlCommandKind,
    ControlCommandStatus,
    ControlReceipt,
    SteerSafePoint,
    QueueProjection,
    SubmissionStatus,
    TurnSubmission,
    TurnSubmissionRequest,
)
from .sqlite import (
    ControlIdempotencyConflictError,
    QueueCommandConflictError,
    QueueRevisionConflictError,
    QueueTargetNotFoundError,
    SQLiteInvocationControl,
)
from .steering import (
    SteerDelivery,
    SteerInvocationUnavailableError,
    SteeringMailbox,
)

SteerInjector = Callable[[SteerDelivery, SteerSafePoint], Awaitable[None]]
InterruptChildren = Callable[[], Awaitable[int | None]]


@dataclass(slots=True)
class RuntimeInvocationLease:
    """Durable submission ownership held by one live Runtime."""

    submission: TurnSubmission
    steering: "RuntimeSteeringSession"


class RuntimeInterruptSession:
    """Live cancellation tree root owned by one Runtime invocation."""

    def __init__(
        self,
        service: "InvocationControlService",
        invocation_id: UUID,
        task: asyncio.Task,
        lease: RuntimeInvocationLease,
        cancel_children: InterruptChildren | None,
    ) -> None:
        self._service = service
        self.invocation_id = invocation_id
        self.task = task
        self.lease = lease
        self.cancel_children = cancel_children
        self._closed = False

    async def close(self) -> None:
        """Remove this exact live binding without touching a replacement."""
        if self._closed:
            return
        self._closed = True
        await self._service.unbind_interrupt(self)


class RuntimeSteeringSession:
    """Invocation-scoped safe-point access to accepted steer commands."""

    def __init__(
        self,
        mailbox: SteeringMailbox,
        invocation_id: UUID,
    ) -> None:
        self._mailbox = mailbox
        self.invocation_id = invocation_id
        self._closed = False

    async def apply_pending(
        self,
        safe_point: SteerSafePoint,
        injector: SteerInjector,
    ) -> int:
        """Inject every currently pending steer and acknowledge afterward."""
        if self._closed:
            return 0
        applied = 0
        while True:
            delivery = await self._mailbox.claim(self.invocation_id)
            if delivery is None:
                return applied
            try:
                await injector(delivery, safe_point)
            except BaseException:
                await self._mailbox.release(delivery)
                raise
            await self._mailbox.acknowledge(delivery, safe_point)
            applied += 1

    async def close(self, *, reason: str = "invocation finished") -> None:
        """Close the live binding and fail commands not yet applied."""
        if self._closed:
            return
        self._closed = True
        await self._mailbox.unbind(self.invocation_id, reason=reason)


class InvocationControlService:  # pylint: disable=too-many-public-methods
    """Workspace-owned runtime control service shared by all adapters."""

    def __init__(
        self,
        database_path: Path | None = None,
        *,
        store: SQLiteInvocationControl | None = None,
    ) -> None:
        if database_path is not None and store is not None:
            raise ValueError("provide database_path or store, not both")
        self.store = store or (
            SQLiteInvocationControl(database_path)
            if database_path is not None
            else None
        )
        self._steering = SteeringMailbox()
        self._dispatch_lock = asyncio.Lock()
        self._dispatch_tasks: dict[UUID, asyncio.Task[None]] = {}
        self._dispatch_ready: dict[UUID, asyncio.Future[bool]] = {}
        self._interrupt_lock = asyncio.Lock()
        self._interrupt_sessions: dict[UUID, RuntimeInterruptSession] = {}
        self._interrupt_tasks: dict[
            UUID,
            asyncio.Task[ControlReceipt | None],
        ] = {}

    async def start(self) -> None:
        """Validate the durable adapter during workspace startup."""
        if self.store is not None:
            await self.store.initialize()

    async def close(self) -> None:
        """Stop dispatchers without resolving still-recoverable commands."""
        async with self._dispatch_lock:
            tasks = tuple(self._dispatch_tasks.values())
        async with self._interrupt_lock:
            interrupt_tasks = tuple(self._interrupt_tasks.values())
            self._interrupt_sessions.clear()
        all_tasks = (*tasks, *interrupt_tasks)
        for task in all_tasks:
            task.cancel()
        if all_tasks:
            await asyncio.gather(*all_tasks, return_exceptions=True)

    async def recover_orphaned_submissions(
        self,
        *,
        agent_id: str,
    ) -> tuple[TurnSubmission, ...]:
        """Interrupt turns owned by a Runtime generation that is gone."""
        if self.store is None:
            return ()
        return await self.store.recover_orphaned_submissions(
            agent_id=agent_id,
        )

    async def bind_interrupt(
        self,
        invocation_id: UUID,
        task: asyncio.Task,
        *,
        lease: RuntimeInvocationLease,
        agent_id: str,
        conversation_id: str,
        cancel_children: InterruptChildren | None = None,
    ) -> RuntimeInterruptSession:
        """Bind a live Runtime task and recover an accepted interrupt."""
        session = RuntimeInterruptSession(
            self,
            invocation_id,
            task,
            lease,
            cancel_children,
        )
        async with self._interrupt_lock:
            current = self._interrupt_sessions.get(invocation_id)
            if current is not None and not current.task.done():
                raise RuntimeError(
                    "invocation interrupt binding already exists",
                )
            self._interrupt_sessions[invocation_id] = session
        if self.store is not None:
            accepted = await self.store.list_accepted_commands(
                agent_id=agent_id,
                conversation_id=conversation_id,
            )
            for command in accepted:
                if (
                    command.kind
                    in {
                        ControlCommandKind.INTERRUPT_CURRENT,
                        ControlCommandKind.STOP_AND_CLEAR,
                    }
                    and command.target_invocation_id == invocation_id
                ):
                    await self._schedule_interrupt(command)
        return session

    async def unbind_interrupt(
        self,
        session: RuntimeInterruptSession,
    ) -> None:
        """Remove a live Runtime only when the binding still matches."""
        async with self._interrupt_lock:
            if self._interrupt_sessions.get(session.invocation_id) is session:
                self._interrupt_sessions.pop(session.invocation_id, None)

    async def open_steering(
        self,
        invocation_id: UUID,
        *,
        agent_id: str | None = None,
        conversation_id: str | None = None,
    ) -> RuntimeSteeringSession:
        """Bind one live invocation to its steering mailbox."""
        await self._steering.bind(invocation_id)
        if self.store is not None and agent_id and conversation_id:
            accepted = await self.store.list_accepted_commands(
                agent_id=agent_id,
                conversation_id=conversation_id,
            )
            for command in accepted:
                if (
                    command.kind is ControlCommandKind.STEER
                    and command.target_invocation_id == invocation_id
                ):
                    await self._schedule_steer(command)
        return RuntimeSteeringSession(self._steering, invocation_id)

    async def offer_steer(self, command: ControlCommand):
        """Offer a steer directly for isolated runtime-level tests."""
        return await self._steering.offer(command)

    async def accept_control(
        self,
        command: ControlCommand,
    ) -> ControlReceipt:
        """Persist a command before scheduling any live delivery."""
        if self.store is None:
            raise RuntimeError("durable invocation-control store is disabled")
        receipt = await self.store.control(command)
        if (
            receipt.status is ControlCommandStatus.ACCEPTED
            and command.kind is ControlCommandKind.STEER
        ):
            await self._schedule_steer(command)
        if (
            receipt.status is ControlCommandStatus.ACCEPTED
            and command.kind
            in {
                ControlCommandKind.INTERRUPT_CURRENT,
                ControlCommandKind.STOP_AND_CLEAR,
            }
        ):
            resolved = await self._schedule_interrupt(command)
            if resolved is not None:
                return resolved
        return receipt

    async def acknowledge_interrupt(
        self,
        command_id: UUID,
        *,
        applied: bool,
        detail: str,
    ) -> ControlReceipt:
        """Resolve an accepted interrupt applied by a compatibility adapter."""
        if self.store is None:
            raise RuntimeError("durable invocation-control store is disabled")
        return await self.store.resolve_command(
            command_id,
            status=(
                ControlCommandStatus.APPLIED
                if applied
                else ControlCommandStatus.CONFLICT
            ),
            detail=detail,
        )

    async def read_queue(
        self,
        *,
        agent_id: str,
        conversation_id: str,
    ) -> QueueProjection:
        """Return the authoritative queue for one ChatSpec identity."""
        if self.store is None:
            raise RuntimeError("durable invocation-control store is disabled")
        return await self.store.read_queue(
            agent_id=agent_id,
            conversation_id=conversation_id,
        )

    async def get_submission(
        self,
        submission_id: UUID,
    ) -> TurnSubmission | None:
        """Return one durable submission including terminal records."""
        if self.store is None:
            raise RuntimeError("durable invocation-control store is disabled")
        return await self.store.get_submission(submission_id)

    async def list_dispatchable(
        self,
        *,
        agent_id: str,
    ) -> tuple[TurnSubmission, ...]:
        """Return one next queued turn for each idle conversation."""
        if self.store is None:
            raise RuntimeError("durable invocation-control store is disabled")
        return await self.store.list_dispatchable(agent_id=agent_id)

    async def enqueue_turn(
        self,
        request: TurnSubmissionRequest,
        *,
        expected_revision: int | None = None,
    ) -> ControlReceipt:
        """Persist one complete turn before any dispatcher admits it."""
        if self.store is None:
            raise RuntimeError("durable invocation-control store is disabled")
        return await self.store.submit(
            request,
            expected_revision=expected_revision,
        )

    async def _replay_control(
        self,
        *,
        kind: ControlCommandKind,
        agent_id: str,
        conversation_id: str,
        idempotency_key: str,
        target_submission_id: UUID | None = None,
        instruction: str | None = None,
        ordered_submission_ids: tuple[UUID, ...] | None = None,
    ) -> ControlReceipt | None:
        """Replay an existing service command after validating its intent."""
        if self.store is None:
            raise RuntimeError("durable invocation-control store is disabled")
        existing = await self.store.lookup_control(
            agent_id=agent_id,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
        )
        if existing is None:
            return None
        command, receipt = existing
        conflict = command.kind is not kind
        if target_submission_id is not None:
            conflict = conflict or (
                command.target_submission_id != target_submission_id
            )
        if instruction is not None:
            conflict = conflict or command.instruction != instruction
        if ordered_submission_ids is not None:
            conflict = conflict or (
                command.ordered_submission_ids != ordered_submission_ids
            )
        if conflict:
            raise ControlIdempotencyConflictError(
                "control idempotency key has conflicting intent",
            )
        return receipt

    async def steer_current(
        self,
        *,
        agent_id: str,
        conversation_id: str,
        instruction: str,
        idempotency_key: str,
        expected_revision: int | None = None,
    ) -> ControlReceipt:
        """Persist a steer for the invocation active in one conversation."""
        replay = await self._replay_control(
            kind=ControlCommandKind.STEER,
            agent_id=agent_id,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
            instruction=instruction,
        )
        if replay is not None:
            return replay
        projection = await self.read_queue(
            agent_id=agent_id,
            conversation_id=conversation_id,
        )
        active = next(
            (
                item
                for item in projection.submissions
                if item.status in ACTIVE_SUBMISSION_STATUSES
            ),
            None,
        )
        if active is None or active.invocation_id is None:
            raise QueueCommandConflictError(
                "conversation has no active invocation to steer",
            )
        return await self.accept_control(
            ControlCommand(
                kind=ControlCommandKind.STEER,
                agent_id=agent_id,
                conversation_id=conversation_id,
                idempotency_key=idempotency_key,
                expected_revision=(
                    projection.revision
                    if expected_revision is None
                    else expected_revision
                ),
                target_invocation_id=active.invocation_id,
                instruction=instruction,
            ),
        )

    async def cancel_queued(
        self,
        *,
        agent_id: str,
        conversation_id: str,
        submission_id: UUID,
        idempotency_key: str,
        expected_revision: int | None = None,
    ) -> ControlReceipt:
        """Cancel one queued turn through the durable control protocol."""
        replay = await self._replay_control(
            kind=ControlCommandKind.CANCEL_QUEUED,
            agent_id=agent_id,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
            target_submission_id=submission_id,
        )
        if replay is not None:
            return replay
        projection = await self.read_queue(
            agent_id=agent_id,
            conversation_id=conversation_id,
        )
        return await self.accept_control(
            ControlCommand(
                kind=ControlCommandKind.CANCEL_QUEUED,
                agent_id=agent_id,
                conversation_id=conversation_id,
                idempotency_key=idempotency_key,
                expected_revision=(
                    projection.revision
                    if expected_revision is None
                    else expected_revision
                ),
                target_submission_id=submission_id,
            ),
        )

    async def reorder_queue(
        self,
        *,
        agent_id: str,
        conversation_id: str,
        ordered_submission_ids: tuple[UUID, ...],
        idempotency_key: str,
        expected_revision: int | None = None,
    ) -> ControlReceipt:
        """Replace the complete queued-turn order atomically."""
        replay = await self._replay_control(
            kind=ControlCommandKind.REORDER,
            agent_id=agent_id,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
            ordered_submission_ids=ordered_submission_ids,
        )
        if replay is not None:
            return replay
        projection = await self.read_queue(
            agent_id=agent_id,
            conversation_id=conversation_id,
        )
        return await self.accept_control(
            ControlCommand(
                kind=ControlCommandKind.REORDER,
                agent_id=agent_id,
                conversation_id=conversation_id,
                idempotency_key=idempotency_key,
                expected_revision=(
                    projection.revision
                    if expected_revision is None
                    else expected_revision
                ),
                ordered_submission_ids=ordered_submission_ids,
            ),
        )

    async def stop_and_clear(
        self,
        *,
        agent_id: str,
        conversation_id: str,
        idempotency_key: str,
        expected_revision: int | None = None,
    ) -> ControlReceipt:
        """Cancel queued turns and interrupt the currently captured run."""
        replay = await self._replay_control(
            kind=ControlCommandKind.STOP_AND_CLEAR,
            agent_id=agent_id,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
        )
        if replay is not None:
            return replay
        projection = await self.read_queue(
            agent_id=agent_id,
            conversation_id=conversation_id,
        )
        active = next(
            (
                item
                for item in projection.submissions
                if item.status in ACTIVE_SUBMISSION_STATUSES
            ),
            None,
        )
        target_invocation_id = (
            active.invocation_id if active is not None else None
        )
        return await self.accept_control(
            ControlCommand(
                kind=ControlCommandKind.STOP_AND_CLEAR,
                agent_id=agent_id,
                conversation_id=conversation_id,
                idempotency_key=idempotency_key,
                expected_revision=(
                    projection.revision
                    if expected_revision is None
                    else expected_revision
                ),
                target_invocation_id=target_invocation_id,
            ),
        )

    async def interrupt_current(
        self,
        *,
        agent_id: str,
        conversation_id: str,
        idempotency_key: str,
        expected_revision: int | None = None,
    ) -> ControlReceipt | None:
        """Interrupt the active invocation for one ChatSpec, if present."""
        replay = await self._replay_control(
            kind=ControlCommandKind.INTERRUPT_CURRENT,
            agent_id=agent_id,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
        )
        if replay is not None:
            return replay
        projection = await self.read_queue(
            agent_id=agent_id,
            conversation_id=conversation_id,
        )
        active = next(
            (
                item
                for item in projection.submissions
                if item.status in ACTIVE_SUBMISSION_STATUSES
            ),
            None,
        )
        if active is None or active.invocation_id is None:
            return None
        return await self.accept_control(
            ControlCommand(
                kind=ControlCommandKind.INTERRUPT_CURRENT,
                agent_id=agent_id,
                conversation_id=conversation_id,
                idempotency_key=idempotency_key,
                expected_revision=(
                    projection.revision
                    if expected_revision is None
                    else expected_revision
                ),
                target_invocation_id=active.invocation_id,
            ),
        )

    async def begin_turn(
        self,
        request: TurnSubmissionRequest,
        *,
        invocation_id: UUID,
    ) -> RuntimeInvocationLease:
        """Admit one exact Chat turn and bind its live steering session."""
        submitted = await self.enqueue_turn(request)
        if submitted.submission_id is None:
            raise RuntimeError("enqueue receipt is missing submission_id")
        return await self.begin_submitted_turn(
            submitted.submission_id,
            invocation_id=invocation_id,
            agent_id=request.agent_id,
            conversation_id=request.conversation_id,
        )

    async def begin_submitted_turn(
        self,
        submission_id: UUID,
        *,
        invocation_id: UUID,
        agent_id: str,
        conversation_id: str,
    ) -> RuntimeInvocationLease:
        """Adopt one exact queued submission into a live Runtime lease."""
        if self.store is None:
            raise RuntimeError("durable invocation-control store is disabled")
        submitted = await self.store.get_submission(submission_id)
        if submitted is None:
            raise QueueTargetNotFoundError("submission was not found")
        if (
            submitted.agent_id != agent_id
            or submitted.conversation_id != conversation_id
        ):
            raise QueueTargetNotFoundError(
                "submission does not belong to this conversation",
            )
        claimed = await self.store.claim_next(
            agent_id=agent_id,
            conversation_id=conversation_id,
            invocation_id=invocation_id,
        )
        if claimed is None:
            raise QueueCommandConflictError(
                "conversation already has an active invocation",
            )
        if claimed.submission_id != submission_id:
            await self.store.transition_submission(
                claimed.submission_id,
                invocation_id=invocation_id,
                target=SubmissionStatus.QUEUED,
                expected_revision=claimed.revision,
            )
            raise QueueCommandConflictError(
                "an older queued turn must be dispatched first",
            )
        running = await self.store.transition_submission(
            claimed.submission_id,
            invocation_id=invocation_id,
            target=SubmissionStatus.RUNNING,
            expected_revision=claimed.revision,
        )
        try:
            steering = await self.open_steering(
                invocation_id,
                agent_id=agent_id,
                conversation_id=conversation_id,
            )
        except BaseException:
            await self.store.transition_submission(
                running.submission_id,
                invocation_id=invocation_id,
                target=SubmissionStatus.FAILED,
                expected_revision=running.revision,
            )
            raise
        return RuntimeInvocationLease(
            submission=running,
            steering=steering,
        )

    async def finish_turn(
        self,
        lease: RuntimeInvocationLease,
        status: SubmissionStatus,
    ) -> TurnSubmission:
        """Commit exactly one terminal status for a live Runtime turn."""
        if self.store is None:
            raise RuntimeError("durable invocation-control store is disabled")
        if status not in {
            SubmissionStatus.SUCCEEDED,
            SubmissionStatus.FAILED,
            SubmissionStatus.INTERRUPTED,
        }:
            raise ValueError("finish_turn requires a runtime terminal status")
        await lease.steering.close(
            reason=f"invocation finished as {status.value}",
        )
        current = lease.submission
        if (
            status is SubmissionStatus.INTERRUPTED
            and current.status is SubmissionStatus.RUNNING
        ):
            current = await self.store.transition_submission(
                current.submission_id,
                invocation_id=lease.steering.invocation_id,
                target=SubmissionStatus.INTERRUPTING,
                expected_revision=current.revision,
            )
        terminal = await self.store.transition_submission(
            current.submission_id,
            invocation_id=lease.steering.invocation_id,
            target=status,
            expected_revision=current.revision,
        )
        lease.submission = terminal
        return terminal

    async def wait_dispatch(self, command_id: UUID) -> None:
        """Wait for a currently scheduled dispatcher, if one exists."""
        async with self._dispatch_lock:
            task = self._dispatch_tasks.get(command_id)
        if task is not None:
            await asyncio.shield(task)

    async def _schedule_steer(self, command: ControlCommand) -> bool:
        """Ensure one dispatcher exists and wait for its mailbox attempt."""
        async with self._dispatch_lock:
            current = self._dispatch_tasks.get(command.command_id)
            if current is not None and not current.done():
                ready = self._dispatch_ready[command.command_id]
            else:
                ready = asyncio.get_running_loop().create_future()
                self._dispatch_ready[command.command_id] = ready
                self._dispatch_tasks[command.command_id] = asyncio.create_task(
                    self._dispatch_steer(command, ready),
                    name=("steer-dispatch-" f"{str(command.command_id)[:8]}"),
                )
        return await asyncio.shield(ready)

    async def _schedule_interrupt(
        self,
        command: ControlCommand,
    ) -> ControlReceipt | None:
        """Apply one accepted interrupt through its live cancellation tree."""
        target_invocation_id = command.target_invocation_id
        if target_invocation_id is None:
            return None
        async with self._interrupt_lock:
            current = self._interrupt_tasks.get(command.command_id)
            if current is None or current.done():
                session = self._interrupt_sessions.get(
                    target_invocation_id,
                )
                if session is None or session.task.done():
                    return None
                current = asyncio.create_task(
                    self._dispatch_interrupt(command, session),
                    name=(
                        "interrupt-dispatch-" f"{str(command.command_id)[:8]}"
                    ),
                )
                self._interrupt_tasks[command.command_id] = current
        return await asyncio.shield(current)

    async def _dispatch_interrupt(
        self,
        command: ControlCommand,
        session: RuntimeInterruptSession,
    ) -> ControlReceipt | None:
        """Cancel child work before the owning Runtime task."""
        child_count = 0
        child_error = ""
        try:
            if self.store is None:
                return None
            if session.task.done():
                return await self._resolve_interrupt(
                    command,
                    status=ControlCommandStatus.CONFLICT,
                    detail="runtime finished before interrupt was applied",
                )
            try:
                session.lease.submission = (
                    await self.store.transition_submission(
                        session.lease.submission.submission_id,
                        invocation_id=session.invocation_id,
                        target=SubmissionStatus.INTERRUPTING,
                        expected_revision=session.lease.submission.revision,
                    )
                )
            except (
                QueueCommandConflictError,
                QueueRevisionConflictError,
            ) as error:
                return await self._resolve_interrupt(
                    command,
                    status=ControlCommandStatus.CONFLICT,
                    detail=str(error),
                )
            if session.cancel_children is not None:
                try:
                    child_count = int(await session.cancel_children() or 0)
                except Exception as error:  # pylint: disable=broad-except
                    child_error = f"; child cancellation failed: {error}"
            session.task.cancel()
            try:
                await asyncio.shield(session.task)
            except asyncio.CancelledError:
                pass
            terminal = session.lease.submission.status
            if terminal is not SubmissionStatus.INTERRUPTED:
                return await self._resolve_interrupt(
                    command,
                    status=ControlCommandStatus.CONFLICT,
                    detail=(
                        "runtime ended without interrupted terminal state: "
                        f"{terminal.value}"
                    ),
                )
            return await self._resolve_interrupt(
                command,
                status=ControlCommandStatus.APPLIED,
                detail=(
                    (
                        "queued submissions cancelled; "
                        if command.kind is ControlCommandKind.STOP_AND_CLEAR
                        else ""
                    )
                    + "runtime cancellation requested; "
                    f"foreground children cancelled={child_count}"
                    f"{child_error}"
                ),
            )
        finally:
            current_task = asyncio.current_task()
            async with self._interrupt_lock:
                if (
                    self._interrupt_tasks.get(command.command_id)
                    is current_task
                ):
                    self._interrupt_tasks.pop(command.command_id, None)

    async def _resolve_interrupt(
        self,
        command: ControlCommand,
        *,
        status: ControlCommandStatus,
        detail: str,
    ) -> ControlReceipt | None:
        """Persist an interrupt result when durable control is enabled."""
        if self.store is None:
            return None
        return await self.store.resolve_command(
            command.command_id,
            status=status,
            detail=detail,
        )

    async def _dispatch_steer(
        self,
        command: ControlCommand,
        ready: asyncio.Future[bool],
    ) -> None:
        """Bridge one accepted steer to a live safe point and receipt."""
        try:
            try:
                applied = await self._steering.offer(command)
            except SteerInvocationUnavailableError:
                # Runtime may not be bound yet. The accepted SQLite record is
                # recovered by ``open_steering`` when that invocation opens.
                if not ready.done():
                    ready.set_result(False)
                return
            if not ready.done():
                ready.set_result(True)
            try:
                safe_point = await applied
            except SteerInvocationUnavailableError as error:
                await self._resolve_steer(
                    command,
                    status=ControlCommandStatus.CONFLICT,
                    detail=str(error),
                )
                return
            await self._resolve_steer(
                command,
                status=ControlCommandStatus.APPLIED,
                detail=f"steer applied at {safe_point.value}",
                safe_point=safe_point,
            )
        finally:
            if not ready.done():
                ready.set_result(False)
            current_task = asyncio.current_task()
            async with self._dispatch_lock:
                if (
                    self._dispatch_tasks.get(command.command_id)
                    is current_task
                ):
                    self._dispatch_tasks.pop(command.command_id, None)
                    self._dispatch_ready.pop(command.command_id, None)

    async def _resolve_steer(
        self,
        command: ControlCommand,
        *,
        status: ControlCommandStatus,
        detail: str,
        safe_point: SteerSafePoint | None = None,
    ) -> None:
        """Persist a delivery result when a durable store is configured."""
        if self.store is None:
            return
        await self.store.resolve_command(
            command.command_id,
            status=status,
            detail=detail,
            safe_point=safe_point,
        )


__all__ = [
    "InterruptChildren",
    "InvocationControlService",
    "RuntimeInterruptSession",
    "RuntimeInvocationLease",
    "RuntimeSteeringSession",
    "SteerInjector",
]
