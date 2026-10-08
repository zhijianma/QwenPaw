# -*- coding: utf-8 -*-
"""Console Chat adapter for the generic durable submission dispatcher."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from ...invocation_control import (
    InvocationControlService,
    SubmissionDispatcher,
)
from ...kernel import (
    TERMINAL_SUBMISSION_STATUSES,
    ActionRecord,
    BackgroundActionContextCheckpoint,
    BackgroundActionContinuation,
    BackgroundActionContinuationStatus,
    ContinuationMode,
    ConversationContinuation,
    ControlCommandKind,
    ControlReceipt,
    InteractionKind,
    InteractionMode,
    InteractionOption,
    InteractionRequest,
    InteractionStatus,
    HarnessStepContinuation,
    HarnessStepContinuationStatus,
    ModelResourceWait,
    ModelStepContinuation,
    ModelStepContinuationStatus,
    ModelStepReconciliation,
    ModelStepReconciliationReason,
    ModelStepRetryAuthorization,
    ResourceWaitStatus,
    SubmissionInputEnvelope,
    SubmissionStatus,
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
CONSOLE_HARNESS_STEP_CONTINUATION_ENVELOPE = (
    "chat.console.harness-step-continuation.v1"
)
CONSOLE_BACKGROUND_ACTION_CONTINUATION_ENVELOPE = (
    "chat.console.background-action-continuation.v1"
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
        from ...runtime.harness_recovery import (
            lite_harness_step_continuation_store,
        )
        from ...runtime.background_actions import (
            lite_background_action_context_store,
            lite_background_action_continuation_store,
        )
        from ...runtime.action_retries import (
            lite_action_retry_continuation_store,
            lite_action_retry_input_store,
        )
        from ...runtime.actions import lite_action_store

        workspace_dir = getattr(workspace, "workspace_dir", None)
        self._harness_steps = (
            lite_harness_step_continuation_store(Path(workspace_dir))
            if workspace_dir is not None
            else None
        )
        self._background_action_contexts = (
            lite_background_action_context_store(Path(workspace_dir))
            if workspace_dir is not None
            else None
        )
        self._background_actions = (
            lite_background_action_continuation_store(Path(workspace_dir))
            if workspace_dir is not None
            else None
        )
        self._action_retry_inputs = (
            lite_action_retry_input_store(Path(workspace_dir))
            if workspace_dir is not None
            else None
        )
        self._action_retries = (
            lite_action_retry_continuation_store(Path(workspace_dir))
            if workspace_dir is not None
            else None
        )
        self._actions = (
            lite_action_store(Path(workspace_dir))
            if workspace_dir is not None
            else None
        )
        self._recovery_event = asyncio.Event()
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
        if (
            self._action_retries is not None
            and self._action_retry_inputs is not None
            and self._actions is not None
        ):
            await self._action_retries.repair(
                agent_id=self._workspace.agent_id,
                input_store=self._action_retry_inputs,
                action_store=self._actions,
            )
        if self._resource_waits is not None:
            await self._dispatch_ready_resource_waits()
            await self._dispatch_ready_model_steps()
        if self._harness_steps is not None:
            await self._dispatch_ready_harness_steps()
        if self._background_actions is not None:
            await self._dispatch_ready_background_actions()
        await self._dispatcher.start()
        if self._interactions is not None and self._continuation_task is None:
            self._continuation_task = asyncio.create_task(
                self._run_continuations(),
                name=(f"interaction-continuations:{self._workspace.agent_id}"),
            )
            self.wake_continuations()
        if (
            self._resource_waits is not None
            or self._harness_steps is not None
            or self._background_actions is not None
        ) and self._resource_wait_task is None:
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
        self._recovery_event.set()
        if self._resource_waits is not None:
            self._resource_waits.notify_change()

    async def _run_resource_waits(self) -> None:
        """Dispatch matured waits without depending on an HTTP request."""
        while True:
            self._recovery_event.clear()
            if self._resource_waits is not None:
                self._resource_waits.clear_change()
            try:
                if self._resource_waits is not None:
                    await self._dispatch_ready_resource_waits()
                    await self._dispatch_ready_model_steps()
                if self._harness_steps is not None:
                    await self._dispatch_ready_harness_steps()
                if self._background_actions is not None:
                    await self._dispatch_ready_background_actions()
            except Exception:  # pylint: disable=broad-except
                logger.exception("Failed to dispatch model resource wait")
                delay = 1.0
            else:
                next_delay = None
                if self._resource_waits is not None:
                    next_delay = (
                        await self._resource_waits.seconds_until_next_timer()
                    )
                delay = 60.0 if next_delay is None else min(
                    max(next_delay, 0.05),
                    60.0,
                )
            if self._resource_waits is not None:
                await self._resource_waits.wait_for_change(delay)
            else:
                try:
                    await asyncio.wait_for(
                        self._recovery_event.wait(),
                        timeout=delay,
                    )
                except TimeoutError:
                    pass

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
            model_step_reconciliation_evidence_digest,
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
            if (
                reconciliation.reason
                is ModelStepReconciliationReason.UNCERTAIN_SIDE_EFFECT
            ):
                evidence_digest = model_step_reconciliation_evidence_digest(
                    actions,
                    continuation.invocation_id,
                )
                if evidence_digest is None:
                    await self._resource_waits.require_action_reconciliation(
                        continuation.continuation_id,
                        reconciliation,
                    )
                    return
                continuation = (
                    await self._resolve_uncertain_action_reconciliation(
                        continuation,
                        reconciliation,
                        evidence_digest,
                    )
                )
                if continuation is None:
                    return
            else:
                continuation = await self._attach_model_step_context(
                    continuation,
                    reconciliation,
                    actions,
                )
                if continuation is None:
                    return
        dispatched = await self._resource_waits.dispatch_model_step(
            continuation.continuation_id,
            self._enqueue_model_step,
        )
        if dispatched.status is ModelStepContinuationStatus.DISPATCHED:
            self._dispatcher.wake()

    # pylint: disable-next=too-many-locals
    async def _attach_model_step_context(
        self,
        continuation: ModelStepContinuation,
        reconciliation: ModelStepReconciliation,
        actions: Sequence[ActionRecord],
    ) -> ModelStepContinuation | None:
        """Attach exact committed Action context or leave the step blocked."""
        from ...runtime.actions import (
            model_step_action_evidence_digest,
            model_step_committed_action_items,
        )
        from ...runtime.model_step_contexts import (
            ModelStepContextConflictError,
            ModelStepContextNotFoundError,
            context_has_committed_action_items,
            lite_model_step_context_store,
        )

        workspace_dir = Path(self._workspace.workspace_dir)
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
            (
                stored_checkpoint,
                agent_state,
            ) = await lite_model_step_context_store(workspace_dir).load(
                checkpoint_id,
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
            and stored_checkpoint.invocation_id == continuation.invocation_id
            and stored_checkpoint.conversation_id
            == continuation.conversation_id
            and stored_checkpoint.action_count == reconciliation.action_count
            and stored_checkpoint.action_evidence_digest == evidence_digest
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
            return None
        continuation = await self._resource_waits.attach_model_step_context(
            continuation.continuation_id,
            stored_checkpoint,
        )
        submissions = await self._control.scan_submissions_for_conversation(
            agent_id=continuation.agent_id,
            conversation_id=continuation.conversation_id,
        )
        source = next(
            (
                item
                for item in submissions
                if item.submission_id == stored_checkpoint.source_submission_id
            ),
            None,
        )
        if source is None or any(
            item.sequence > source.sequence for item in submissions
        ):
            await self._resource_waits.cancel_model_step(
                continuation.continuation_id,
            )
            return None
        return continuation

    async def _resolve_uncertain_action_reconciliation(
        self,
        continuation: ModelStepContinuation,
        reconciliation: ModelStepReconciliation,
        evidence_digest: str,
    ) -> ModelStepContinuation | None:
        """Require an exact Chat approval before one uncertain retry."""
        if self._interactions is None:
            await self._resource_waits.require_action_reconciliation(
                continuation.continuation_id,
                reconciliation,
            )
            return None
        interaction_id = uuid5(
            continuation.continuation_id,
            "uncertain-action-retry",
        )
        interaction = await self._interactions.get_request(interaction_id)
        if interaction is None:
            interaction = InteractionRequest(
                interaction_id=interaction_id,
                kind=InteractionKind.APPROVAL,
                mode=InteractionMode.BLOCKING,
                agent_id=continuation.agent_id,
                conversation_id=continuation.conversation_id,
                invocation_id=continuation.invocation_id,
                correlation_id=continuation.correlation_id,
                source_id=continuation.continuation_id,
                continuation_mode=ContinuationMode.CHECKPOINT,
                continuation_checkpoint_id=continuation.continuation_id,
                title="Reconcile uncertain action",
                prompt=(
                    "An external action may already have taken effect. "
                    "Retry only after verifying that repeating it is safe."
                ),
                options=(
                    InteractionOption(
                        option_id="retry_once",
                        label="Verified safe; retry once",
                    ),
                    InteractionOption(
                        option_id="stop",
                        label="Stop recovery",
                    ),
                ),
                metadata={
                    "purpose": "model_step_action_reconciliation",
                    "action_count": reconciliation.action_count,
                    "action_evidence_digest": evidence_digest,
                },
            )
            await self._interactions.open(interaction)
        actual_binding = (
            interaction.kind,
            interaction.mode,
            interaction.agent_id,
            interaction.conversation_id,
            interaction.invocation_id,
            interaction.correlation_id,
            interaction.source_id,
            interaction.continuation_mode,
            interaction.continuation_checkpoint_id,
        )
        expected_binding = (
            InteractionKind.APPROVAL,
            InteractionMode.BLOCKING,
            continuation.agent_id,
            continuation.conversation_id,
            continuation.invocation_id,
            continuation.correlation_id,
            continuation.continuation_id,
            ContinuationMode.CHECKPOINT,
            continuation.continuation_id,
        )
        if (
            actual_binding != expected_binding
            or interaction.metadata.get("action_count")
            != reconciliation.action_count
            or interaction.metadata.get("action_evidence_digest")
            != evidence_digest
        ):
            raise RuntimeError(
                "uncertain Action interaction binding mismatch",
            )
        resolution = await self._interactions.get_resolution(interaction_id)
        if resolution is None:
            await self._resource_waits.require_action_reconciliation(
                continuation.continuation_id,
                reconciliation,
            )
            return None
        if (
            resolution.status is not InteractionStatus.RESOLVED
            or resolution.response is None
            or len(resolution.response.selected_option_ids) != 1
        ):
            await self._resource_waits.cancel_model_step(
                continuation.continuation_id,
            )
            return None
        decision = resolution.response.selected_option_ids[0]
        if decision == "stop":
            await self._resource_waits.cancel_model_step(
                continuation.continuation_id,
            )
            return None
        if decision != "retry_once":
            raise RuntimeError("unsupported uncertain Action decision")
        return await self._resource_waits.authorize_uncertain_action_retry(
            continuation.continuation_id,
            ModelStepRetryAuthorization(
                interaction_id=interaction_id,
                response_revision=resolution.revision,
                continuation_id=continuation.continuation_id,
                invocation_id=continuation.invocation_id,
                conversation_id=continuation.conversation_id,
                action_evidence_digest=evidence_digest,
                action_count=reconciliation.action_count,
                authorized_at=resolution.resolved_at,
            ),
        )

    async def _dispatch_ready_harness_steps(self) -> None:
        """Dispatch admitted Harness continuations through the Chat queue."""
        if self._harness_steps is None:
            return
        continuations = await self._harness_steps.list_ready(
            agent_id=self._workspace.agent_id,
        )
        first_error: Exception | None = None
        for continuation in continuations:
            try:
                dispatched = await self._harness_steps.dispatch(
                    continuation.continuation_id,
                    self._enqueue_harness_step,
                )
                if (
                    dispatched.status
                    is HarnessStepContinuationStatus.DISPATCHED
                ):
                    self._dispatcher.wake()
            except Exception as exc:  # pylint: disable=broad-except
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    async def _enqueue_harness_step(
        self,
        continuation: HarnessStepContinuation,
    ) -> UUID | None:
        """Create one fenced Submission for an admitted Harness context."""
        checkpoint = continuation.checkpoint
        projection = await self._control.read_queue(
            agent_id=continuation.agent_id,
            conversation_id=checkpoint.conversation_id,
        )
        controls = await self._control.scan_for_conversation(
            agent_id=continuation.agent_id,
            conversation_id=checkpoint.conversation_id,
        )
        if any(
            (
                record.command.kind
                in {
                    ControlCommandKind.INTERRUPT_CURRENT,
                    ControlCommandKind.STOP_AND_CLEAR,
                }
                and record.command.target_invocation_id
                == checkpoint.invocation_id
            )
            or (
                record.command.kind is ControlCommandKind.STOP_AND_CLEAR
                and record.command.requested_at >= continuation.created_at
            )
            for record in controls
        ):
            return None
        submissions = await self._control.scan_submissions_for_conversation(
            agent_id=continuation.agent_id,
            conversation_id=checkpoint.conversation_id,
        )
        source = next(
            (
                item
                for item in submissions
                if item.submission_id == checkpoint.source_submission_id
            ),
            None,
        )
        idempotency_key = (
            "harness-step-continuation:"
            f"{continuation.continuation_id}"
        )
        if source is None or any(
            item.sequence > source.sequence
            and item.idempotency_key != idempotency_key
            for item in submissions
        ):
            return None
        request = TurnSubmissionRequest(
            agent_id=continuation.agent_id,
            conversation_id=checkpoint.conversation_id,
            content="[Harness step continuation]",
            request_context={
                "channel": "console",
                "harness_backend": checkpoint.backend,
                "harness_step_continuation_id": str(
                    continuation.continuation_id,
                ),
            },
            input_envelope=SubmissionInputEnvelope(
                kind=CONSOLE_HARNESS_STEP_CONTINUATION_ENVELOPE,
                payload={
                    "continuation_id": str(
                        continuation.continuation_id,
                    ),
                },
            ),
            idempotency_key=idempotency_key,
            correlation_id=continuation.correlation_id,
        )
        receipt = await self._control.enqueue_turn(
            request,
            expected_revision=projection.revision,
        )
        if receipt.submission_id is None:
            raise RuntimeError(
                "Harness continuation enqueue returned no submission",
            )
        return receipt.submission_id

    async def _dispatch_ready_background_actions(self) -> None:
        """Repair, reconcile, and dispatch completed background Actions."""
        if (
            self._background_actions is None
            or self._background_action_contexts is None
        ):
            return
        checkpoints = await self._background_action_contexts.list_checkpoints(
            agent_id=self._workspace.agent_id,
        )
        for checkpoint in checkpoints:
            if await self._background_action_is_committed(checkpoint):
                await self._background_actions.defer(checkpoint)
        continuations = await self._background_actions.list_pending(
            agent_id=self._workspace.agent_id,
        )
        first_error: Exception | None = None
        for continuation in continuations:
            try:
                await self._dispatch_background_action(continuation)
            except Exception as exc:  # pylint: disable=broad-except
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    # pylint: disable-next=too-many-return-statements
    async def _dispatch_background_action(
        self,
        continuation: BackgroundActionContinuation,
    ) -> None:
        """Continue only after source completion and context validation."""
        checkpoint = continuation.checkpoint
        if (
            continuation.status
            is BackgroundActionContinuationStatus.WAITING_SOURCE
        ):
            source = await self._control.get_submission(
                checkpoint.source_submission_id,
            )
            if source is None:
                await self._background_actions.transition(
                    continuation.continuation_id,
                    BackgroundActionContinuationStatus.CANCELLED,
                )
                return
            if source.status not in TERMINAL_SUBMISSION_STATUSES:
                return
            if source.status is not SubmissionStatus.SUCCEEDED:
                await self._background_actions.transition(
                    continuation.continuation_id,
                    BackgroundActionContinuationStatus.CANCELLED,
                )
                return
            if await self._background_action_is_superseded(continuation):
                await self._background_actions.transition(
                    continuation.continuation_id,
                    BackgroundActionContinuationStatus.CANCELLED,
                )
                return
            chat = await self._workspace.chat_manager.get_chat(
                checkpoint.committed_item.conversation_id,
            )
            if chat is None:
                await self._background_actions.transition(
                    continuation.continuation_id,
                    BackgroundActionContinuationStatus.CANCELLED,
                )
                return
            session_state = (
                await self._workspace.session.get_session_state_dict(
                    session_id=chat.session_id,
                    user_id=chat.user_id,
                    channel=chat.channel,
                )
            )
            from ...runtime.model_step_contexts import (
                context_has_committed_action_items,
            )

            persisted_agent_state = session_state.get("agent")
            if isinstance(
                persisted_agent_state,
                dict,
            ) and context_has_committed_action_items(
                persisted_agent_state,
                (checkpoint.committed_item,),
            ):
                await self._background_actions.transition(
                    continuation.continuation_id,
                    BackgroundActionContinuationStatus.CANCELLED,
                )
                return
            continuation = await self._background_actions.transition(
                continuation.continuation_id,
                BackgroundActionContinuationStatus.READY,
            )
        if continuation.status is not BackgroundActionContinuationStatus.READY:
            return
        dispatched = await self._background_actions.dispatch(
            continuation.continuation_id,
            self._enqueue_background_action,
        )
        if dispatched.status is BackgroundActionContinuationStatus.DISPATCHED:
            self._dispatcher.wake()

    async def _background_action_is_superseded(
        self,
        continuation: BackgroundActionContinuation,
    ) -> bool:
        checkpoint = continuation.checkpoint
        conversation_id = checkpoint.committed_item.conversation_id
        controls = await self._control.scan_for_conversation(
            agent_id=checkpoint.agent_id,
            conversation_id=conversation_id,
        )
        if any(
            (
                record.command.kind
                in {
                    ControlCommandKind.INTERRUPT_CURRENT,
                    ControlCommandKind.STOP_AND_CLEAR,
                }
                and record.command.target_invocation_id
                == checkpoint.committed_item.invocation_id
            )
            or (
                record.command.kind is ControlCommandKind.STOP_AND_CLEAR
                and record.command.requested_at >= continuation.created_at
            )
            for record in controls
        ):
            return True
        submissions = await self._control.scan_submissions_for_conversation(
            agent_id=checkpoint.agent_id,
            conversation_id=conversation_id,
        )
        source = next(
            (
                item
                for item in submissions
                if item.submission_id == checkpoint.source_submission_id
            ),
            None,
        )
        idempotency_key = (
            "background-action-continuation:"
            f"{continuation.continuation_id}"
        )
        return source is None or any(
            item.sequence > source.sequence
            and item.idempotency_key != idempotency_key
            and not item.idempotency_key.startswith(
                "background-action-continuation:",
            )
            for item in submissions
        )

    async def _background_action_is_committed(
        self,
        checkpoint: BackgroundActionContextCheckpoint,
    ) -> bool:
        """Require ActionStore to confirm the snapshot's exact digest."""
        from ...runtime.actions import lite_action_store

        records = await lite_action_store(
            Path(self._workspace.workspace_dir),
        ).scan_for_conversation(
            checkpoint.committed_item.conversation_id,
        )
        return any(
            record.request.action_id == checkpoint.committed_item.action_id
            and record.result is not None
            and record.result.invocation_id
            == checkpoint.committed_item.invocation_id
            and record.result.observation_digest
            == checkpoint.committed_item.observation_digest
            for record in records
        )

    async def _enqueue_background_action(
        self,
        continuation: BackgroundActionContinuation,
    ) -> UUID | None:
        """Create one fenced Submission for a committed background result."""
        if await self._background_action_is_superseded(continuation):
            return None
        checkpoint = continuation.checkpoint
        conversation_id = checkpoint.committed_item.conversation_id
        projection = await self._control.read_queue(
            agent_id=checkpoint.agent_id,
            conversation_id=conversation_id,
        )
        idempotency_key = (
            "background-action-continuation:"
            f"{continuation.continuation_id}"
        )
        request = TurnSubmissionRequest(
            agent_id=checkpoint.agent_id,
            conversation_id=conversation_id,
            content="[background Action continuation]",
            request_context={
                "channel": "console",
                "background_action_continuation_id": str(
                    continuation.continuation_id,
                ),
            },
            input_envelope=SubmissionInputEnvelope(
                kind=CONSOLE_BACKGROUND_ACTION_CONTINUATION_ENVELOPE,
                payload={
                    "continuation_id": str(
                        continuation.continuation_id,
                    ),
                },
            ),
            idempotency_key=idempotency_key,
            correlation_id=checkpoint.correlation_id,
        )
        receipt = await self._control.enqueue_turn(
            request,
            expected_revision=projection.revision,
        )
        if receipt.submission_id is None:
            raise RuntimeError(
                "background Action enqueue returned no submission",
            )
        return receipt.submission_id

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
        idempotency_key = (
            "model-step-continuation:"
            f"{continuation.continuation_id}"
        )
        if continuation.retry_authorization is not None:
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
                    if item.invocation_id == continuation.invocation_id
                ),
                None,
            )
            if source is None or any(
                item.sequence > source.sequence
                and item.idempotency_key != idempotency_key
                for item in submissions
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
            idempotency_key=idempotency_key,
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
            CONSOLE_BACKGROUND_ACTION_CONTINUATION_ENVELOPE,
            CONSOLE_SUBMISSION_ENVELOPE,
            CONSOLE_INTERACTION_CONTINUATION_ENVELOPE,
            CONSOLE_HARNESS_STEP_CONTINUATION_ENVELOPE,
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
        elif envelope.kind == CONSOLE_BACKGROUND_ACTION_CONTINUATION_ENVELOPE:
            payload = await self._materialize_background_action_payload(
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
        elif envelope.kind == CONSOLE_HARNESS_STEP_CONTINUATION_ENVELOPE:
            payload = await self._materialize_harness_step_payload(
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

    async def _materialize_harness_step_payload(
        self,
        envelope: SubmissionInputEnvelope,
        chat: Any,
        submission: TurnSubmission,
    ) -> dict[str, Any]:
        """Resolve a Harness outbox pointer into a fenced continuation."""
        if self._harness_steps is None:
            raise RuntimeError("Harness recovery store is unavailable")
        try:
            continuation_id = UUID(
                str(envelope.payload["continuation_id"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid Harness continuation envelope") from exc
        continuation = await self._harness_steps.get(continuation_id)
        if continuation is None:
            raise ValueError("Harness continuation is unavailable")
        checkpoint = continuation.checkpoint
        if (
            continuation.agent_id != submission.agent_id
            or checkpoint.conversation_id != submission.conversation_id
            or checkpoint.conversation_id != chat.id
        ):
            raise ValueError(
                "Harness continuation does not belong to its ChatSpec",
            )
        if (
            continuation.status
            is not HarnessStepContinuationStatus.DISPATCHED
            or continuation.submission_id != submission.submission_id
        ):
            raise ValueError(
                "Harness continuation is not bound to this Submission",
            )
        from ...runtime.harness_recovery import (
            lite_harness_recovery_context_store,
        )

        stored = await lite_harness_recovery_context_store(
            Path(self._workspace.workspace_dir),
        ).load(checkpoint.checkpoint_id)
        if stored != checkpoint:
            raise ValueError(
                "Harness recovery checkpoint does not match continuation",
            )
        return {
            "channel_id": chat.channel,
            "sender_id": chat.user_id,
            "content_parts": [
                {
                    "type": "text",
                    "text": (
                        "QwenPaw Harness recovery: continue the interrupted "
                        "task from the existing provider context. Do not "
                        "repeat completed external actions."
                    ),
                },
            ],
            "message_metadata": {
                "qwenpaw_client_message_id": submission.idempotency_key,
                "qwenpaw_harness_step_continuation": str(continuation_id),
            },
            "message_id": submission.idempotency_key,
            "meta": {
                "session_id": chat.session_id,
                "user_id": chat.user_id,
                "request_context": {
                    "harness_backend": checkpoint.backend,
                    "harness_recovery_checkpoint_id": str(
                        checkpoint.checkpoint_id,
                    ),
                    "harness_step_continuation_id": str(continuation_id),
                    "harness_recovery_cycle": continuation.recovery_cycle,
                },
            },
        }

    async def _materialize_background_action_payload(
        self,
        envelope: SubmissionInputEnvelope,
        chat: Any,
        submission: TurnSubmission,
    ) -> dict[str, Any]:
        """Resolve one background Action pointer into a verified input."""
        if (
            self._background_actions is None
            or self._background_action_contexts is None
        ):
            raise RuntimeError("background Action recovery is unavailable")
        try:
            continuation_id = UUID(
                str(envelope.payload["continuation_id"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "invalid background Action continuation envelope",
            ) from exc
        continuation = await self._background_actions.get(continuation_id)
        if continuation is None:
            raise ValueError("background Action continuation is unavailable")
        checkpoint = continuation.checkpoint
        if (
            checkpoint.agent_id != submission.agent_id
            or checkpoint.committed_item.conversation_id
            != submission.conversation_id
            or checkpoint.committed_item.conversation_id != chat.id
        ):
            raise ValueError(
                "background Action continuation does not belong to ChatSpec",
            )
        if (
            continuation.status
            is not BackgroundActionContinuationStatus.DISPATCHED
            or continuation.submission_id != submission.submission_id
        ):
            raise ValueError(
                "background Action is not bound to this Submission",
            )
        stored_checkpoint, agent_state = (
            await self._background_action_contexts.load(
                checkpoint.checkpoint_id,
            )
        )
        if stored_checkpoint != checkpoint:
            raise ValueError("background Action checkpoint does not match")
        if not await self._background_action_is_committed(checkpoint):
            raise ValueError(
                "background Action result is not durably committed",
            )
        from ...runtime.model_step_contexts import (
            context_has_committed_action_items,
        )

        if not context_has_committed_action_items(
            agent_state,
            (checkpoint.committed_item,),
        ):
            raise ValueError(
                "background Action context does not prove its result",
            )
        return {
            "channel_id": chat.channel,
            "sender_id": chat.user_id,
            "content_parts": [
                {
                    "type": "text",
                    "text": (
                        "QwenPaw background Action recovery: continue from "
                        "the committed result in the bound private context."
                    ),
                },
            ],
            "message_metadata": {
                "qwenpaw_client_message_id": submission.idempotency_key,
                "qwenpaw_background_action_continuation": str(
                    continuation_id,
                ),
            },
            "message_id": submission.idempotency_key,
            "meta": {
                "session_id": chat.session_id,
                "user_id": chat.user_id,
                "request_context": {
                    "background_action_checkpoint_id": str(
                        checkpoint.checkpoint_id,
                    ),
                    "background_action_continuation_id": str(
                        continuation_id,
                    ),
                    "background_action_recovery_cycle": (
                        checkpoint.recovery_cycle
                    ),
                },
            },
        }

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
        authorization = continuation.retry_authorization
        if authorization is not None:
            if self._interactions is None:
                raise RuntimeError(
                    "model-step retry authorization is unavailable",
                )
            resolution = await self._interactions.get_resolution(
                authorization.interaction_id,
            )
            if (
                resolution is None
                or resolution.status is not InteractionStatus.RESOLVED
                or resolution.revision != authorization.response_revision
                or resolution.response is None
                or resolution.response.selected_option_ids != ("retry_once",)
            ):
                raise ValueError(
                    "model-step retry authorization is not valid",
                )
            from ...runtime.actions import (
                lite_action_store,
                model_step_reconciliation_evidence_digest,
            )

            records = await lite_action_store(
                Path(self._workspace.workspace_dir),
            ).scan_for_conversation(continuation.conversation_id)
            actual_digest = model_step_reconciliation_evidence_digest(
                records,
                continuation.invocation_id,
            )
            if actual_digest != authorization.action_evidence_digest:
                raise ValueError(
                    "model-step retry Action evidence changed",
                )
        recovery_instruction = (
            "QwenPaw runtime model-step recovery: the prior model stream "
            "ended before a terminal response. Continue the original task "
            "from durable conversation state. Do not treat partial output "
            "as committed and do not repeat external actions."
        )
        if authorization is not None:
            recovery_instruction = (
                "QwenPaw runtime model-step recovery: the prior model "
                "stream ended after an uncertain external action. The user "
                "verified that one retry from the safe conversation "
                "boundary is allowed. Re-evaluate the task and do not "
                "assume the prior partial output was committed."
            )
        return {
            "channel_id": chat.channel,
            "sender_id": chat.user_id,
            "content_parts": [
                {
                    "type": "text",
                    "text": recovery_instruction,
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
                    **(
                        {
                            "model_step_retry_interaction_id": str(
                                authorization.interaction_id,
                            ),
                            "model_step_retry_evidence_digest": (
                                authorization.action_evidence_digest
                            ),
                        }
                        if authorization is not None
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
    "CONSOLE_BACKGROUND_ACTION_CONTINUATION_ENVELOPE",
    "CONSOLE_INTERACTION_CONTINUATION_ENVELOPE",
    "CONSOLE_HARNESS_STEP_CONTINUATION_ENVELOPE",
    "CONSOLE_MODEL_RECOVERY_ENVELOPE",
    "CONSOLE_SUBMISSION_ENVELOPE",
    "WorkspaceChatSubmissionDispatcher",
]
