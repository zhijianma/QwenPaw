# -*- coding: utf-8 -*-
"""Console Chat adapter for the generic durable submission dispatcher."""

from __future__ import annotations

import asyncio
import copy
from typing import Any

from ...invocation_control import (
    InvocationControlService,
    SubmissionDispatcher,
)
from ...kernel import (
    ControlReceipt,
    SubmissionInputEnvelope,
    TurnSubmission,
    TurnSubmissionRequest,
)
from .input_context import persist_pending_project_dirs

CONSOLE_SUBMISSION_ENVELOPE = "chat.console.native.v1"


class WorkspaceChatSubmissionDispatcher:
    """Execute durable Console turns independently of HTTP subscribers."""

    def __init__(
        self,
        *,
        workspace: Any,
        control: InvocationControlService,
    ) -> None:
        self._workspace = workspace
        self._control = control
        self._dispatcher = SubmissionDispatcher(
            agent_id=workspace.agent_id,
            control=control,
            execute=self._execute,
        )

    async def start(self) -> None:
        """Recover queued Console turns after workspace services start."""
        await self._dispatcher.start()

    async def stop(self) -> None:
        """Release this workspace generation's dispatch ownership."""
        await self._dispatcher.stop()

    async def enqueue(
        self,
        request: TurnSubmissionRequest,
        *,
        expected_revision: int | None = None,
    ) -> ControlReceipt:
        """Persist one complete input and wake the independent consumer."""
        receipt = await self._control.enqueue_turn(
            request,
            expected_revision=expected_revision,
        )
        self._dispatcher.wake()
        return receipt

    async def _execute(
        self,
        submission: TurnSubmission,
        envelope: SubmissionInputEnvelope,
    ) -> None:
        if envelope.kind != CONSOLE_SUBMISSION_ENVELOPE:
            raise ValueError(
                f"unsupported submission input envelope: {envelope.kind}",
            )
        chat = await self._workspace.chat_manager.get_chat(
            submission.conversation_id,
        )
        if chat is None:
            raise ValueError("submission conversation does not exist")
        channel = await self._workspace.channel_manager.get_channel("console")
        if channel is None:
            raise RuntimeError("Console channel is unavailable")

        payload = copy.deepcopy(envelope.payload)
        self._bind_runtime_identity(payload, submission)
        self._validate_chat_ownership(payload, chat)
        chat = await persist_pending_project_dirs(
            self._workspace,
            chat,
            payload,
        )
        tracker = self._workspace.task_tracker
        while await tracker.get_status(chat.id) == "running":
            await asyncio.sleep(0.1)
        queue, is_new_run = await tracker.attach_or_start(
            chat.id,
            payload,
            channel.stream_one,
            owner=self._workspace,
            on_finished=self._workspace.chat_manager.mark_chat_finished,
        )
        if not is_new_run:
            await tracker.detach_subscriber(chat.id, queue)
            raise RuntimeError("conversation gained another active runtime")
        async for _ in tracker.stream_from_queue(queue, chat.id):
            pass

    @staticmethod
    def _bind_runtime_identity(
        payload: dict[str, Any],
        submission: TurnSubmission,
    ) -> None:
        meta = payload.setdefault("meta", {})
        if not isinstance(meta, dict):
            raise ValueError("submission meta must be an object")
        raw_context = meta.get("request_context")
        request_context = (
            dict(raw_context) if isinstance(raw_context, dict) else {}
        )
        request_context.update(
            {
                "os_conversation_id": submission.conversation_id,
                "os_submission_idempotency_key": (submission.idempotency_key),
                "os_submission_id": str(submission.submission_id),
                "os_submission_priority": submission.priority,
            },
        )
        meta["request_context"] = request_context

    @staticmethod
    def _validate_chat_ownership(payload: dict[str, Any], chat: Any) -> None:
        meta = payload.get("meta")
        if not isinstance(meta, dict):
            raise ValueError("submission meta must be an object")
        expected = {
            "sender_id": chat.user_id,
            "channel_id": chat.channel,
            "session_id": chat.session_id,
        }
        actual = {
            "sender_id": payload.get("sender_id"),
            "channel_id": payload.get("channel_id"),
            "session_id": meta.get("session_id"),
        }
        if actual != expected:
            raise ValueError(
                "submission input does not belong to its ChatSpec",
            )


__all__ = [
    "CONSOLE_SUBMISSION_ENVELOPE",
    "WorkspaceChatSubmissionDispatcher",
]
