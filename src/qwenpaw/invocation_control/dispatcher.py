# -*- coding: utf-8 -*-
"""Workspace-owned consumer for durable conversation submissions."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from functools import partial
from uuid import uuid4

from ..kernel import (
    SubmissionInputEnvelope,
    SubmissionStatus,
    TurnSubmission,
)
from .service import InvocationControlService
from .sqlite import QueueCommandConflictError, QueueTargetNotFoundError

logger = logging.getLogger(__name__)

SubmissionExecutor = Callable[
    [TurnSubmission, SubmissionInputEnvelope],
    Awaitable[None],
]


class SubmissionDispatcher:
    """Consume one queued turn per conversation independently of clients."""

    def __init__(
        self,
        *,
        agent_id: str,
        control: InvocationControlService,
        execute: SubmissionExecutor,
        poll_interval: float = 0.25,
    ) -> None:
        self._agent_id = agent_id
        self._control = control
        self._execute = execute
        self._poll_interval = poll_interval
        self._wake = asyncio.Event()
        self._supervisor: asyncio.Task[None] | None = None
        self._workers: dict[str, asyncio.Task[None]] = {}

    async def start(self) -> tuple[TurnSubmission, ...]:
        """Start recovery scanning once for this workspace generation."""
        if self._supervisor is not None and not self._supervisor.done():
            return ()
        recovered = await self._control.recover_orphaned_submissions(
            agent_id=self._agent_id,
        )
        if recovered:
            logger.warning(
                "submission dispatcher interrupted orphaned runtimes "
                "agent=%s count=%s",
                self._agent_id,
                len(recovered),
            )
        self._supervisor = asyncio.create_task(
            self._run(),
            name=f"submission-dispatch-{self._agent_id}",
        )
        self.wake()
        return tuple(recovered)

    async def stop(self) -> None:
        """Stop dispatch ownership without fabricating terminal results."""
        supervisor = self._supervisor
        self._supervisor = None
        if supervisor is not None:
            supervisor.cancel()
        workers = tuple(self._workers.values())
        for worker in workers:
            worker.cancel()
        await asyncio.gather(
            *(item for item in (supervisor, *workers) if item is not None),
            return_exceptions=True,
        )
        self._workers.clear()

    def wake(self) -> None:
        """Notify the dispatcher that durable queue state may have changed."""
        self._wake.set()

    async def _run(self) -> None:
        while True:
            await self._start_available()
            self._wake.clear()
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=self._poll_interval,
                )
            except asyncio.TimeoutError:
                pass

    async def _start_available(self) -> None:
        submissions = await self._control.list_dispatchable(
            agent_id=self._agent_id,
        )
        for submission in submissions:
            conversation_id = submission.conversation_id
            worker = self._workers.get(conversation_id)
            if worker is not None and not worker.done():
                continue
            worker = asyncio.create_task(
                self._run_one(submission),
                name=(
                    "submission-run-"
                    f"{conversation_id[:16]}-"
                    f"{str(submission.submission_id)[:8]}"
                ),
            )
            self._workers[conversation_id] = worker
            worker.add_done_callback(
                partial(self._worker_done, conversation_id),
            )

    def _worker_done(
        self,
        conversation_id: str,
        worker: asyncio.Task[None],
    ) -> None:
        if self._workers.get(conversation_id) is worker:
            self._workers.pop(conversation_id, None)
        if not worker.cancelled():
            error = worker.exception()
            if error is not None:
                logger.error(
                    "submission dispatcher worker failed conversation=%s",
                    conversation_id,
                    exc_info=error,
                )
        self.wake()

    async def _run_one(self, submission: TurnSubmission) -> None:
        try:
            envelope = submission.input_envelope
            if envelope is None:
                raise ValueError("submission input envelope is missing")
            await self._execute(submission, envelope)
            current = await self._control.get_submission(
                submission.submission_id,
            )
            if (
                current is not None
                and current.status is SubmissionStatus.QUEUED
            ):
                raise RuntimeError(
                    "submission executor returned before Runtime admission",
                )
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception(
                "submission execution failed submission=%s",
                submission.submission_id,
            )
            await self._fail_if_queued(submission, str(exc))

    async def _fail_if_queued(
        self,
        submission: TurnSubmission,
        detail: str,
    ) -> None:
        current = await self._control.get_submission(
            submission.submission_id,
        )
        if current is None or current.status is not SubmissionStatus.QUEUED:
            return
        invocation_id = uuid4()
        try:
            lease = await self._control.begin_submitted_turn(
                submission.submission_id,
                invocation_id=invocation_id,
                agent_id=submission.agent_id,
                conversation_id=submission.conversation_id,
            )
            await self._control.finish_turn(
                lease,
                SubmissionStatus.FAILED,
            )
            logger.error(
                "submission failed closed submission=%s detail=%s",
                submission.submission_id,
                detail,
            )
        except (QueueCommandConflictError, QueueTargetNotFoundError):
            logger.info(
                "submission changed before fail-close submission=%s",
                submission.submission_id,
            )


__all__ = ["SubmissionDispatcher", "SubmissionExecutor"]
