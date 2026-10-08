# -*- coding: utf-8 -*-
"""Console Chat adapter for the generic durable submission dispatcher."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from ...invocation_control import (
    InvocationControlService,
    SubmissionDispatcher,
)
from ...kernel import (
    ConversationContinuation,
    ControlCommandKind,
    ControlReceipt,
    InteractionStatus,
    ModelResourceWait,
    ModelStepContinuation,
    ModelStepContinuationStatus,
    ResourceWaitStatus,
    SubmissionInputEnvelope,
    TurnSubmission,
    TurnSubmissionRequest,
)
from .input_context import persist_pending_project_dirs

CONSOLE_SUBMISSION_ENVELOPE = "chat.console.native.v1"
CONSOLE_INTERACTION_CONTINUATION_ENVELOPE = (
    "chat.console.interaction-continuation.v1"
)
CONSOLE_MODEL_RECOVERY_ENVELOPE = "chat.console.model-recovery.v1"
CONSOLE_MODEL_STEP_CONTINUATION_ENVELOPE = (
    "chat.console.model-step-continuation.v1"
)
logger = logging.getLogger(__name__)


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
        self._interactions = getattr(workspace, "interaction_service", None)
        self._resource_waits = getattr(
            workspace,
            "model_resource_wait_service",
            None,
        )
        self._continuation_event = asyncio.Event()
        self._continuation_task: asyncio.Task[None] | None = None
        self._resource_wait_task: asyncio.Task[None] | None = None
        self._dispatcher = SubmissionDispatcher(
            agent_id=workspace.agent_id,
            control=control,
            execute=self._execute,
        )

    async def start(self) -> None:
        """Recover queued Console turns after workspace services start."""
        if self._resource_waits is not None:
            await self._dispatch_ready_resource_waits()
            await self._dispatch_ready_model_steps()
        await self._dispatcher.start()
        if self._interactions is not None and self._continuation_task is None:
            self._continuation_task = asyncio.create_task(
                self._run_continuations(),
                name=(f"interaction-continuations:{self._workspace.agent_id}"),
            )
            self.wake_continuations()
        if (
            self._resource_waits is not None
            and self._resource_wait_task is None
        ):
            self._resource_wait_task = asyncio.create_task(
                self._run_resource_waits(),
                name=(f"model-resource-waits:{self._workspace.agent_id}"),
            )
            self.wake_resource_waits()

    async def stop(self) -> None:
        """Release this workspace generation's dispatch ownership."""
        task = self._continuation_task
        self._continuation_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        resource_task = self._resource_wait_task
        self._resource_wait_task = None
        if resource_task is not None:
            resource_task.cancel()
            await asyncio.gather(resource_task, return_exceptions=True)
        await self._dispatcher.stop()

    def wake_continuations(self) -> None:
        """Wake the durable continuation outbox worker."""
        self._continuation_event.set()

    def wake_resource_waits(self) -> None:
        """Wake model-resource continuations after an external release."""
        if self._resource_waits is not None:
            self._resource_waits.notify_change()

    async def _run_resource_waits(self) -> None:
        """Dispatch matured waits without depending on an HTTP request."""
        while True:
            self._resource_waits.clear_change()
            try:
                await self._dispatch_ready_resource_waits()
                await self._dispatch_ready_model_steps()
            except Exception:  # pylint: disable=broad-except
                logger.exception("Failed to dispatch model resource wait")
                delay = 1.0
            else:
                next_delay = (
                    await self._resource_waits.seconds_until_next_timer()
                )
                delay = 60.0 if next_delay is None else min(
                    max(next_delay, 0.05),
                    60.0,
                )
            await self._resource_waits.wait_for_change(delay)

    async def _dispatch_ready_resource_waits(self) -> None:
        waits = await self._resource_waits.list_ready()
        first_error: Exception | None = None
        for wait in waits:
            try:
                await self._dispatch_resource_wait(wait)
            except Exception as exc:  # pylint: disable=broad-except
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    async def _dispatch_resource_wait(
        self,
        wait: ModelResourceWait,
    ) -> None:
        """Create one idempotent continuation for a ready resource wait."""
        if wait.status is not ResourceWaitStatus.READY:
            raise RuntimeError("model resource wait is not ready")

        dispatched = await self._resource_waits.dispatch_ready(
            wait.wait_id,
            self._enqueue_resource_wait,
        )
        if dispatched.status is ResourceWaitStatus.DISPATCHED:
            self._dispatcher.wake()

    async def _dispatch_ready_model_steps(self) -> None:
        """Dispatch partial-stream continuations without polling Chat."""
        continuations = (
            await self._resource_waits.list_recoverable_model_steps()
        )
        first_error: Exception | None = None
        for continuation in continuations:
            try:
                await self._dispatch_model_step(continuation)
            except Exception as exc:  # pylint: disable=broad-except
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    async def _dispatch_model_step(
        self,
        continuation: ModelStepContinuation,
    ) -> None:
        """Continue only from a complete, causally current context."""
        workspace_dir = getattr(self._workspace, "workspace_dir", None)
        if workspace_dir is None:
            raise RuntimeError(
                "workspace path is required for action reconciliation",
            )
        from ...runtime.actions import (
            assess_model_step_reconciliation,
            lite_action_store,
            model_step_committed_action_items,
            model_step_action_evidence_digest,
        )
        from ...runtime.model_step_contexts import (
            ModelStepContextConflictError,
            ModelStepContextNotFoundError,
            context_has_committed_action_items,
            lite_model_step_context_store,
        )

        action_store = lite_action_store(Path(workspace_dir))
        actions = await action_store.scan_for_conversation(
            continuation.conversation_id,
        )
        reconciliation = assess_model_step_reconciliation(
            actions,
            continuation.invocation_id,
        )
        if reconciliation is not None:
            committed_items = model_step_committed_action_items(
                actions,
                continuation.invocation_id,
            )
            evidence_digest = model_step_action_evidence_digest(
                actions,
                continuation.invocation_id,
            )
            checkpoint = continuation.context_checkpoint
            checkpoint_id = (
                checkpoint.checkpoint_id
                if checkpoint is not None
                else uuid5(
                    continuation.continuation_id,
                    "private-agent-context",
                )
            )
            try:
                stored_checkpoint, agent_state = (
                    await lite_model_step_context_store(
                        Path(workspace_dir),
                    ).load(checkpoint_id)
                )
            except (
                ModelStepContextConflictError,
                ModelStepContextNotFoundError,
            ):
                stored_checkpoint = None
                agent_state = None
            context_ready = (
                committed_items is not None
                and evidence_digest is not None
                and stored_checkpoint is not None
                and agent_state is not None
                and stored_checkpoint.continuation_id
                == continuation.continuation_id
                and stored_checkpoint.invocation_id
                == continuation.invocation_id
                and stored_checkpoint.conversation_id
                == continuation.conversation_id
                and stored_checkpoint.action_count
                == reconciliation.action_count
                and stored_checkpoint.action_evidence_digest
                == evidence_digest
                and context_has_committed_action_items(
                    agent_state,
                    committed_items,
                )
            )
            if not context_ready:
                await self._resource_waits.require_action_reconciliation(
                    continuation.continuation_id,
                    reconciliation,
                )
                return
            continuation = (
                await self._resource_waits.attach_model_step_context(
                    continuation.continuation_id,
                    stored_checkpoint,
                )
            )
            submissions = (
                await self._control.scan_submissions_for_conversation(
                    agent_id=continuation.agent_id,
                    conversation_id=continuation.conversation_id,
                )
            )
            source = next(
                (
                    item
                    for item in submissions
                    if item.submission_id
                    == stored_checkpoint.source_submission_id
                ),
                None,
            )
            if source is None or any(
                item.sequence > source.sequence for item in submissions
            ):
                await self._resource_waits.cancel_model_step(
                    continuation.continuation_id,
                )
                return
        dispatched = await self._resource_waits.dispatch_model_step(
            continuation.continuation_id,
            self._enqueue_model_step,
        )
        if dispatched.status is ModelStepContinuationStatus.DISPATCHED:
            self._dispatcher.wake()

    async def _enqueue_model_step(
        self,
        continuation: ModelStepContinuation,
    ) -> UUID | None:
        """Create one fenced Submission for a partial model step."""
        projection = await self._control.read_queue(
            agent_id=continuation.agent_id,
            conversation_id=continuation.conversation_id,
        )
        records = await self._control.scan_for_conversation(
            agent_id=continuation.agent_id,
            conversation_id=continuation.conversation_id,
        )
        if any(
            (
                record.command.kind
                in {
                    ControlCommandKind.INTERRUPT_CURRENT,
                    ControlCommandKind.STOP_AND_CLEAR,
                }
                and record.command.target_invocation_id
                == continuation.invocation_id
            )
            or (
                record.command.kind is ControlCommandKind.STOP_AND_CLEAR
                and record.command.requested_at
                >= continuation.created_at
            )
            for record in records
        ):
            return None
        request = TurnSubmissionRequest(
            agent_id=continuation.agent_id,
            conversation_id=continuation.conversation_id,
            content="[model step continuation]",
            request_context={
                "channel": "console",
                "model_step_continuation_id": str(
                    continuation.continuation_id,
                ),
            },
            input_envelope=SubmissionInputEnvelope(
                kind=CONSOLE_MODEL_STEP_CONTINUATION_ENVELOPE,
                payload={
                    "continuation_id": str(
                        continuation.continuation_id,
                    ),
                },
            ),
            idempotency_key=(
                "model-step-continuation:"
                f"{continuation.continuation_id}"
            ),
            correlation_id=continuation.correlation_id,
        )
        receipt = await self._control.enqueue_turn(
            request,
            expected_revision=projection.revision,
        )
        if receipt.submission_id is None:
            raise RuntimeError(
                "model-step enqueue returned no submission identity",
            )
        return receipt.submission_id

    async def _enqueue_resource_wait(
        self,
        wait: ModelResourceWait,
    ) -> UUID | None:
        """Enqueue unless an authoritative stop command fences the wait."""
        projection = await self._control.read_queue(
            agent_id=wait.agent_id,
            conversation_id=wait.conversation_id,
        )
        records = await self._control.scan_for_conversation(
            agent_id=wait.agent_id,
            conversation_id=wait.conversation_id,
        )
        if any(
            (
                record.command.kind
                in {
                    ControlCommandKind.INTERRUPT_CURRENT,
                    ControlCommandKind.STOP_AND_CLEAR,
                }
                and record.command.target_invocation_id
                == wait.invocation_id
            )
            or (
                record.command.kind is ControlCommandKind.STOP_AND_CLEAR
                and record.command.requested_at >= wait.created_at
            )
            for record in records
        ):
            return None
        request = TurnSubmissionRequest(
            agent_id=wait.agent_id,
            conversation_id=wait.conversation_id,
            content="[model resource continuation]",
            request_context={
                "channel": "console",
                "model_resource_wait_id": str(wait.wait_id),
            },
            input_envelope=SubmissionInputEnvelope(
                kind=CONSOLE_MODEL_RECOVERY_ENVELOPE,
                payload={"wait_id": str(wait.wait_id)},
            ),
            idempotency_key=f"model-resource-continuation:{wait.wait_id}",
            correlation_id=wait.correlation_id,
        )
        receipt = await self._control.enqueue_turn(
            request,
            expected_revision=projection.revision,
        )
        if receipt.submission_id is None:
            raise RuntimeError(
                "model recovery enqueue returned no submission identity",
            )
        return receipt.submission_id

    async def _run_continuations(self) -> None:
        """Retry ready outbox entries without coupling them to HTTP."""
        retry_delay = 1.0
        while True:
            await self._continuation_event.wait()
            self._continuation_event.clear()
            try:
                await self._dispatch_ready_continuations()
            except Exception:  # pylint: disable=broad-except
                logger.exception(
                    "Failed to dispatch interaction continuation",
                )
                try:
                    await asyncio.wait_for(
                        self._continuation_event.wait(),
                        timeout=retry_delay,
                    )
                except TimeoutError:
                    pass
                retry_delay = min(retry_delay * 2, 60.0)
                self._continuation_event.set()
            else:
                retry_delay = 1.0

    async def _dispatch_ready_continuations(self) -> None:
        """Create one idempotent Submission per resolved interaction."""
        continuations = await self._interactions.list_ready_continuations(
            agent_id=self._workspace.agent_id,
        )
        first_error: Exception | None = None
        for continuation in continuations:
            try:
                await self._dispatch_continuation(continuation)
            except Exception as exc:  # pylint: disable=broad-except
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    async def _dispatch_continuation(
        self,
        continuation: ConversationContinuation,
    ) -> None:
        """Idempotently bind one outbox entry to one Submission."""
        request = await self._interactions.get_request(
            continuation.interaction_id,
        )
        if request is None:
            raise RuntimeError(
                "continuation interaction request is unavailable",
            )
        if (
            request.agent_id != continuation.agent_id
            or request.conversation_id != continuation.conversation_id
        ):
            raise RuntimeError(
                "continuation interaction owner mismatch",
            )
        submission = TurnSubmissionRequest(
            agent_id=continuation.agent_id,
            conversation_id=continuation.conversation_id,
            content="[interaction continuation]",
            request_context={
                "channel": "console",
                "interaction_id": str(continuation.interaction_id),
            },
            input_envelope=SubmissionInputEnvelope(
                kind=CONSOLE_INTERACTION_CONTINUATION_ENVELOPE,
                payload={
                    "interaction_id": str(continuation.interaction_id),
                    "response_revision": continuation.response_revision,
                },
            ),
            idempotency_key=(
                "interaction-continuation:"
                f"{continuation.interaction_id}:"
                f"{continuation.response_revision}"
            ),
            correlation_id=request.correlation_id,
        )
        receipt = await self.enqueue(submission)
        if receipt.submission_id is None:
            raise RuntimeError(
                "continuation enqueue returned no submission identity",
            )
        await self._interactions.mark_continuation_dispatched(
            continuation.interaction_id,
            receipt.submission_id,
        )

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
        if envelope.kind not in {
            CONSOLE_SUBMISSION_ENVELOPE,
            CONSOLE_INTERACTION_CONTINUATION_ENVELOPE,
            CONSOLE_MODEL_RECOVERY_ENVELOPE,
            CONSOLE_MODEL_STEP_CONTINUATION_ENVELOPE,
        }:
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

        if envelope.kind == CONSOLE_INTERACTION_CONTINUATION_ENVELOPE:
            payload = await self._materialize_continuation_payload(
                envelope,
                chat,
                submission,
            )
        elif envelope.kind == CONSOLE_MODEL_RECOVERY_ENVELOPE:
            payload = await self._materialize_model_recovery_payload(
                envelope,
                chat,
                submission,
            )
        elif envelope.kind == CONSOLE_MODEL_STEP_CONTINUATION_ENVELOPE:
            payload = await self._materialize_model_step_payload(
                envelope,
                chat,
                submission,
            )
        else:
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

    async def _materialize_model_recovery_payload(
        self,
        envelope: SubmissionInputEnvelope,
        chat: Any,
        submission: TurnSubmission,
    ) -> dict[str, Any]:
        """Resolve a resource pointer into a bounded continuation input."""
        if self._resource_waits is None:
            raise RuntimeError("Model resource wait service is unavailable")
        try:
            wait_id = UUID(str(envelope.payload["wait_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid model recovery envelope") from exc
        wait = await self._resource_waits.get(wait_id)
        if wait is None:
            raise ValueError("model resource wait is unavailable")
        if (
            wait.agent_id != submission.agent_id
            or wait.conversation_id != submission.conversation_id
            or wait.conversation_id != chat.id
        ):
            raise ValueError(
                "model resource wait does not belong to its ChatSpec",
            )
        if (
            wait.status is not ResourceWaitStatus.DISPATCHED
            or wait.submission_id != submission.submission_id
        ):
            raise ValueError(
                "model resource wait is not bound to this Submission",
            )
        return {
            "channel_id": chat.channel,
            "sender_id": chat.user_id,
            "content_parts": [
                {
                    "type": "text",
                    "text": (
                        "QwenPaw runtime resource recovery: continue the "
                        "interrupted task from the durable conversation "
                        "state. Re-check external side effects before "
                        "repeating any action."
                    ),
                },
            ],
            "message_metadata": {
                "qwenpaw_client_message_id": submission.idempotency_key,
                "qwenpaw_model_resource_wait": str(wait.wait_id),
            },
            "message_id": submission.idempotency_key,
            "meta": {
                "session_id": chat.session_id,
                "user_id": chat.user_id,
                "request_context": {
                    "model_resource_wait_id": str(wait.wait_id),
                    "recovered_attempt_id": str(wait.attempt_id),
                },
            },
        }

    async def _materialize_model_step_payload(
        self,
        envelope: SubmissionInputEnvelope,
        chat: Any,
        submission: TurnSubmission,
    ) -> dict[str, Any]:
        """Resolve a partial-stream pointer without replaying its text."""
        if self._resource_waits is None:
            raise RuntimeError("Model recovery service is unavailable")
        try:
            continuation_id = UUID(
                str(envelope.payload["continuation_id"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid model-step envelope") from exc
        continuation = await self._resource_waits.get_model_step(
            continuation_id,
        )
        if continuation is None:
            raise ValueError("model-step continuation is unavailable")
        if (
            continuation.agent_id != submission.agent_id
            or continuation.conversation_id
            != submission.conversation_id
            or continuation.conversation_id != chat.id
        ):
            raise ValueError(
                "model-step continuation does not belong to its ChatSpec",
            )
        if (
            continuation.status
            is not ModelStepContinuationStatus.DISPATCHED
            or continuation.submission_id != submission.submission_id
        ):
            raise ValueError(
                "model-step continuation is not bound to this Submission",
            )
        checkpoint = continuation.context_checkpoint
        if checkpoint is not None:
            from ...runtime.model_step_contexts import (
                lite_model_step_context_store,
            )

            stored_checkpoint, _ = await lite_model_step_context_store(
                Path(self._workspace.workspace_dir),
            ).load(checkpoint.checkpoint_id)
            if stored_checkpoint != checkpoint:
                raise ValueError(
                    "model-step context checkpoint does not match recovery",
                )
        return {
            "channel_id": chat.channel,
            "sender_id": chat.user_id,
            "content_parts": [
                {
                    "type": "text",
                    "text": (
                        "QwenPaw runtime model-step recovery: the prior "
                        "model stream ended before a terminal response. "
                        "Continue the original task from durable "
                        "conversation state. Do not treat partial output "
                        "as committed and do not repeat external actions."
                    ),
                },
            ],
            "message_metadata": {
                "qwenpaw_client_message_id": submission.idempotency_key,
                "qwenpaw_model_step_continuation": str(continuation_id),
            },
            "message_id": submission.idempotency_key,
            "meta": {
                "session_id": chat.session_id,
                "user_id": chat.user_id,
                "request_context": {
                    "model_step_continuation_id": str(continuation_id),
                    "recovered_attempt_id": str(
                        continuation.attempt_id,
                    ),
                    **(
                        {
                            "model_step_context_checkpoint_id": str(
                                checkpoint.checkpoint_id,
                            ),
                        }
                        if checkpoint is not None
                        else {}
                    ),
                },
            },
        }

    async def _materialize_continuation_payload(
        self,
        envelope: SubmissionInputEnvelope,
        chat: Any,
        submission: TurnSubmission,
    ) -> dict[str, Any]:
        """Resolve an outbox pointer into one Console user message."""
        if self._interactions is None:
            raise RuntimeError("Interaction service is unavailable")
        try:
            interaction_id = UUID(str(envelope.payload["interaction_id"]))
            response_revision = int(
                envelope.payload["response_revision"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "invalid interaction continuation envelope",
            ) from exc
        request = await self._interactions.get_request(interaction_id)
        resolution = await self._interactions.get_resolution(interaction_id)
        if request is None or resolution is None:
            raise ValueError("interaction continuation source is unavailable")
        if (
            request.agent_id != submission.agent_id
            or request.conversation_id != submission.conversation_id
            or request.conversation_id != chat.id
        ):
            raise ValueError(
                "interaction continuation does not belong to its ChatSpec",
            )
        if (
            resolution.status is not InteractionStatus.RESOLVED
            or resolution.response is None
            or resolution.revision != response_revision
        ):
            raise ValueError("interaction continuation is not resolved")
        answer = self._format_interaction_answer(
            request,
            resolution.response,
        )
        return {
            "channel_id": chat.channel,
            "sender_id": chat.user_id,
            "content_parts": [{"type": "text", "text": answer}],
            "message_metadata": {
                "qwenpaw_client_message_id": submission.idempotency_key,
                "qwenpaw_interaction_continuation": str(interaction_id),
            },
            "message_id": submission.idempotency_key,
            "meta": {
                "session_id": chat.session_id,
                "user_id": chat.user_id,
                "request_context": {
                    "interaction_id": str(interaction_id),
                    "interaction_response_revision": response_revision,
                },
            },
        }

    @staticmethod
    def _format_interaction_answer(request: Any, response: Any) -> str:
        """Render one structured answer for the next model step."""
        if response.text:
            answer = response.text
        elif response.selected_option_ids:
            labels = {
                option.option_id: option.label for option in request.options
            }
            answer = ", ".join(
                labels.get(option_id, option_id)
                for option_id in response.selected_option_ids
            )
        else:
            answer = json.dumps(
                response.values,
                ensure_ascii=False,
                sort_keys=True,
            )
        return (
            "User response to QwenPaw interaction "
            f"{request.interaction_id}: {answer}"
        )

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
                "os_correlation_id": str(submission.correlation_id),
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
    "CONSOLE_INTERACTION_CONTINUATION_ENVELOPE",
    "CONSOLE_MODEL_RECOVERY_ENVELOPE",
    "CONSOLE_SUBMISSION_ENVELOPE",
    "WorkspaceChatSubmissionDispatcher",
]
