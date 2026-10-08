# -*- coding: utf-8 -*-
"""Tests for the workspace Console submission adapter."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4, uuid5

import pytest
from agentscope.message import Msg, TextBlock

from qwenpaw.app.chats.manager import ChatManager
from qwenpaw.app.chats.models import ChatSpec
from qwenpaw.app.chats.repo import JsonChatRepository
from qwenpaw.app.chats.submission_dispatcher import (
    CONSOLE_ACTION_RETRY_ENVELOPE,
    CONSOLE_BACKGROUND_ACTION_CONTINUATION_ENVELOPE,
    CONSOLE_HARNESS_STEP_CONTINUATION_ENVELOPE,
    CONSOLE_INTERACTION_CONTINUATION_ENVELOPE,
    CONSOLE_MODEL_RECOVERY_ENVELOPE,
    CONSOLE_MODEL_STEP_CONTINUATION_ENVELOPE,
    CONSOLE_SUBMISSION_ENVELOPE,
    WorkspaceChatSubmissionDispatcher,
)
from qwenpaw.app.task_tracker import TaskTracker
from qwenpaw.app.chats.session import SafeJSONSession
from qwenpaw.invocation_control import (
    InvocationControlService,
    QueueRevisionConflictError,
    SQLiteInvocationControl,
)
from qwenpaw.interactions import InteractionService
from qwenpaw.recovery import ModelResourceWaitService
from qwenpaw.kernel import (
    COMMITTED_ACTION_ITEM_METADATA_KEY,
    TERMINAL_SUBMISSION_STATUSES,
    BackgroundActionContinuationStatus,
    ActionKind,
    ActionRecord,
    ActionRequest,
    ActionResult,
    ActionRetryDecision,
    ActionRetryDisposition,
    ActionRetryReason,
    ActionRetryContinuationStatus,
    ActionStatus,
    ActorRef,
    ActorType,
    CommittedActionItem,
    ContinuationDispatchStatus,
    ContinuationMode,
    InteractionKind,
    InteractionMode,
    InteractionOption,
    InteractionRequest,
    UserInputReason,
    InteractionResponse,
    HarnessStepContinuationStatus,
    ModelCallAttempt,
    ModelCallResult,
    ModelCallStatus,
    ModelFailureClass,
    ModelOutputBoundary,
    ModelRecoveryDisposition,
    ModelStepReconciliationReason,
    ModelStepContextCheckpoint,
    ModelStepContinuationStatus,
    ResourceWaitStatus,
    SideEffectStatus,
    SubmissionInputEnvelope,
    SubmissionStatus,
    TurnSubmission,
    TurnSubmissionRequest,
    ToolEffect,
    ToolSelection,
)
from qwenpaw.runtime.action_retries import (
    lite_action_retry_continuation_store,
    lite_action_retry_input_store,
)
from qwenpaw.runtime.actions import (
    lite_action_store,
    model_step_action_evidence_digest,
    model_step_committed_action_items,
)
from qwenpaw.runtime.background_actions import (
    build_background_action_checkpoint,
    build_background_action_snapshot,
    lite_background_action_context_store,
    lite_background_action_continuation_store,
)
from qwenpaw.runtime.harness_recovery import (
    build_harness_recovery_checkpoint,
    lite_harness_recovery_context_store,
    lite_harness_step_continuation_store,
)
from qwenpaw.runtime.model_step_contexts import (
    lite_model_step_context_store,
)
from qwenpaw.runtime.action_execution import RuntimeActionRetryRunner
from qwenpaw.runtime.builder import AgentBuilder


@pytest.mark.asyncio
async def test_new_user_submission_cancels_pending_action_retries(
    tmp_path: Path,
) -> None:
    control = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    workspace = SimpleNamespace(agent_id="default")
    dispatcher = WorkspaceChatSubmissionDispatcher(
        workspace=workspace,
        control=control,
    )
    cancelled = []

    class RetryStore:
        async def cancel_for_conversation(
            self,
            *,
            agent_id,
            conversation_id,
        ):
            cancelled.append((agent_id, conversation_id))
            return ()

    # Isolate enqueue ordering: durable submission first, retry cancel second.
    # pylint: disable=protected-access
    dispatcher._action_retries = RetryStore()
    # pylint: enable=protected-access
    receipt = await dispatcher.enqueue(
        TurnSubmissionRequest(
            agent_id="default",
            conversation_id="chat-new-intent",
            content="new user intent",
            input_envelope=SubmissionInputEnvelope(
                kind=CONSOLE_SUBMISSION_ENVELOPE,
                payload={"content_parts": []},
            ),
            idempotency_key="new-user-intent",
        ),
        expected_revision=0,
    )

    assert receipt.submission_id is not None
    assert cancelled == [("default", "chat-new-intent")]
    await control.close()


@pytest.mark.asyncio
async def test_workspace_dispatcher_repairs_and_dispatches_action_retry(
    tmp_path: Path,
) -> None:
    request = ActionRequest(
        invocation_id=uuid4(),
        correlation_id=uuid4(),
        agent_id="default",
        conversation_id="chat-retry-repair",
        registry_generation=3,
        capability_id="example.retry-tool",
        tool_selection=ToolSelection(active_modes=("coding",)),
        provider_execution_digest=f"sha256:{'c' * 64}",
        kind=ActionKind.TOOL,
        action_name="retry_tool",
        arguments={"value": "private"},
        redacted_arguments={"value": "[REDACTED]"},
        arguments_hash=f"sha256:{'a' * 64}",
        effect=ToolEffect.NONE,
        idempotency_key="retry-repair-key",
    )
    decision = ActionRetryDecision(
        disposition=ActionRetryDisposition.RETRY_FROM_NEW_ACTION,
        reason=ActionRetryReason.TRANSIENT_FAILURE,
        provider_retryable=True,
        max_attempts=2,
        next_attempt=2,
        retry_after_seconds=0,
    )
    inputs = lite_action_retry_input_store(tmp_path)
    checkpoint = await inputs.save(request, decision)
    result = ActionResult(
        action_id=request.action_id,
        invocation_id=request.invocation_id,
        conversation_id=request.conversation_id,
        status=ActionStatus.FAILED,
        observation_digest=f"sha256:{'b' * 64}",
        retryable=True,
        retry_decision=decision.model_copy(
            update={"input_checkpoint_id": checkpoint.checkpoint_id},
        ),
    )
    actions = lite_action_store(tmp_path)
    await actions.begin(request)
    await actions.complete(result)
    outbox = lite_action_retry_continuation_store(tmp_path)
    assert await outbox.list_pending(agent_id="default") == ()

    control = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
    )
    dispatcher = WorkspaceChatSubmissionDispatcher(
        workspace=workspace,
        control=control,
    )

    await outbox.repair(
        agent_id="default",
        input_store=inputs,
        action_store=actions,
    )
    # pylint: disable=protected-access
    await dispatcher._cancel_superseded_action_retries()
    await dispatcher._dispatch_ready_action_retries()
    # pylint: enable=protected-access
    [continuation] = await outbox.list_dispatched(agent_id="default")
    assert continuation.checkpoint == checkpoint
    projection = await control.read_queue(
        agent_id="default",
        conversation_id=request.conversation_id,
    )
    [submission] = projection.submissions
    assert submission.submission_id == continuation.dispatch_id
    assert submission.content == "[Action retry]"
    assert submission.correlation_id == request.correlation_id
    assert submission.input_envelope == SubmissionInputEnvelope(
        kind=CONSOLE_ACTION_RETRY_ENVELOPE,
        payload={
            "continuation_id": str(continuation.continuation_id),
        },
    )
    # Repeated scheduling must reuse the one durable dispatch binding.
    # pylint: disable=protected-access
    await dispatcher._dispatch_ready_action_retries()
    # pylint: enable=protected-access
    repeated = await control.read_queue(
        agent_id="default",
        conversation_id=request.conversation_id,
    )
    assert repeated.submissions == projection.submissions
    stopped = await control.stop_and_clear(
        agent_id="default",
        conversation_id=request.conversation_id,
        idempotency_key="stop-retry-repair",
        expected_revision=projection.revision,
    )
    assert stopped.status.value == "applied"
    terminal = await control.get_submission(submission.submission_id)
    assert terminal is not None
    assert terminal.status is SubmissionStatus.CANCELLED
    # DISPATCHED is immutable audit linkage; Submission owns execution state.
    bound = await outbox.get(continuation.continuation_id)
    assert bound is not None
    assert bound.status is ActionRetryContinuationStatus.DISPATCHED
    await control.close()


@pytest.mark.asyncio
async def test_workspace_dispatcher_runs_pinned_action_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = ActionRequest(
        invocation_id=uuid4(),
        correlation_id=uuid4(),
        agent_id="default",
        conversation_id="chat-retry-run",
        registry_generation=7,
        capability_id="example.retry-tool",
        tool_selection=ToolSelection(active_modes=("coding",)),
        provider_execution_digest=f"sha256:{'c' * 64}",
        kind=ActionKind.TOOL,
        action_name="retry_tool",
        arguments={"value": "private"},
        redacted_arguments={"value": "[REDACTED]"},
        arguments_hash=f"sha256:{'a' * 64}",
        effect=ToolEffect.NONE,
        idempotency_key="retry-run-key",
    )
    decision = ActionRetryDecision(
        disposition=ActionRetryDisposition.RETRY_FROM_NEW_ACTION,
        reason=ActionRetryReason.TRANSIENT_FAILURE,
        provider_retryable=True,
        max_attempts=2,
        next_attempt=2,
        retry_after_seconds=0,
    )
    inputs = lite_action_retry_input_store(tmp_path)
    checkpoint = await inputs.save(request, decision)
    result = ActionResult(
        action_id=request.action_id,
        invocation_id=request.invocation_id,
        conversation_id=request.conversation_id,
        status=ActionStatus.FAILED,
        observation_digest=f"sha256:{'b' * 64}",
        retryable=True,
        retry_decision=decision.model_copy(
            update={"input_checkpoint_id": checkpoint.checkpoint_id},
        ),
    )
    actions = lite_action_store(tmp_path)
    await actions.begin(request)
    await actions.complete(result)
    outbox = lite_action_retry_continuation_store(tmp_path)
    await outbox.defer(checkpoint, result)
    control = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        config=SimpleNamespace(project_dir=None),
        app_services=SimpleNamespace(tool_coordinator=object()),
    )
    dispatcher = WorkspaceChatSubmissionDispatcher(
        workspace=workspace,
        control=control,
    )

    class Governor:
        stopped = False

        def stop(self) -> None:
            self.stopped = True

    governor = Governor()
    monkeypatch.setattr(
        AgentBuilder,
        "build_governor",
        staticmethod(lambda *_args, **_kwargs: governor),
    )
    observed: dict[str, Any] = {}

    async def run_retry(_runner, **kwargs):
        observed.update(kwargs)
        return object()

    monkeypatch.setattr(RuntimeActionRetryRunner, "run", run_retry)

    await dispatcher.start()
    terminal = None
    for _ in range(100):
        dispatched = await outbox.list_dispatched(agent_id=request.agent_id)
        if dispatched and dispatched[0].dispatch_id is not None:
            terminal = await control.get_submission(
                dispatched[0].dispatch_id,
            )
        if (
            terminal is not None
            and terminal.status in TERMINAL_SUBMISSION_STATUSES
        ):
            break
        await asyncio.sleep(0.01)

    assert terminal is not None
    assert terminal.status is SubmissionStatus.SUCCEEDED
    assert terminal.invocation_id == observed["plan"].invocation_id
    assert observed["prepared"].arguments == {"value": "private"}
    assert governor.stopped
    await dispatcher.stop()
    await control.close()


@pytest.mark.asyncio
async def test_interrupted_action_retry_without_result_is_requeued(
    tmp_path: Path,
) -> None:
    request = ActionRequest(
        invocation_id=uuid4(),
        correlation_id=uuid4(),
        agent_id="default",
        conversation_id="chat-retry-interrupted",
        registry_generation=3,
        capability_id="example.retry-tool",
        kind=ActionKind.TOOL,
        action_name="retry_tool",
        arguments={"value": "private"},
        redacted_arguments={"value": "[REDACTED]"},
        arguments_hash=f"sha256:{'a' * 64}",
        effect=ToolEffect.NONE,
        idempotency_key="retry-interrupted-key",
    )
    decision = ActionRetryDecision(
        disposition=ActionRetryDisposition.RETRY_FROM_NEW_ACTION,
        reason=ActionRetryReason.TRANSIENT_FAILURE,
        provider_retryable=True,
        max_attempts=2,
        next_attempt=2,
        retry_after_seconds=0,
    )
    inputs = lite_action_retry_input_store(tmp_path)
    checkpoint = await inputs.save(request, decision)
    result = ActionResult(
        action_id=request.action_id,
        invocation_id=request.invocation_id,
        conversation_id=request.conversation_id,
        status=ActionStatus.FAILED,
        observation_digest=f"sha256:{'b' * 64}",
        retryable=True,
        retry_decision=decision.model_copy(
            update={"input_checkpoint_id": checkpoint.checkpoint_id},
        ),
    )
    actions = lite_action_store(tmp_path)
    await actions.begin(request)
    await actions.complete(result)
    outbox = lite_action_retry_continuation_store(tmp_path)
    ready = await outbox.defer(checkpoint, result)
    submission_id = uuid4()

    async def bind_dispatch(_continuation):
        return submission_id

    await outbox.dispatch(ready.continuation_id, bind_dispatch)
    invocation_id = uuid4()
    interrupted = TurnSubmission(
        submission_id=submission_id,
        sequence=1,
        invocation_id=invocation_id,
        status=SubmissionStatus.INTERRUPTED,
        agent_id="default",
        conversation_id=request.conversation_id,
        content="[Action retry]",
        input_envelope=SubmissionInputEnvelope(
            kind=CONSOLE_ACTION_RETRY_ENVELOPE,
            payload={"continuation_id": str(ready.continuation_id)},
        ),
        idempotency_key=f"action-retry:{ready.continuation_id}",
        correlation_id=request.correlation_id,
    )
    control = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    dispatcher = WorkspaceChatSubmissionDispatcher(
        workspace=SimpleNamespace(
            agent_id="default",
            workspace_dir=tmp_path,
        ),
        control=control,
    )

    # pylint: disable=protected-access
    await dispatcher._recover_interrupted_action_retries((interrupted,))
    # pylint: enable=protected-access

    recovered = await outbox.get(ready.continuation_id)
    assert recovered is not None
    assert recovered.status is ActionRetryContinuationStatus.READY
    assert recovered.recovered_dispatch_id == submission_id

    completed_submission_id = uuid4()

    async def bind_completed_dispatch(_continuation):
        return completed_submission_id

    await outbox.dispatch(
        ready.continuation_id,
        bind_completed_dispatch,
    )
    completed_invocation_id = uuid4()
    completed_request = request.model_copy(
        update={
            "action_id": uuid4(),
            "invocation_id": completed_invocation_id,
            "retry_root_action_id": request.action_id,
            "retry_of_action_id": request.action_id,
            "attempt": 2,
        },
    )
    dispatched = await outbox.get(ready.continuation_id)
    assert dispatched is not None
    # pylint: disable=protected-access
    with pytest.raises(RuntimeError, match="attempt identity mismatch"):
        dispatcher._validate_recovered_action_attempt(
            dispatched,
            ActionRecord(
                request=completed_request.model_copy(
                    update={"capability_id": "example.drifted-tool"},
                ),
            ),
        )
    # pylint: enable=protected-access
    await actions.begin(completed_request)
    await actions.complete(
        ActionResult(
            action_id=completed_request.action_id,
            invocation_id=completed_invocation_id,
            conversation_id=request.conversation_id,
            status=ActionStatus.SUCCEEDED,
            observation_digest=f"sha256:{'c' * 64}",
        ),
    )
    completed_interrupted = interrupted.model_copy(
        update={
            "submission_id": completed_submission_id,
            "invocation_id": completed_invocation_id,
            "revision": interrupted.revision + 1,
        },
    )

    # pylint: disable=protected-access
    await dispatcher._recover_interrupted_action_retries(
        (completed_interrupted,),
    )
    # pylint: enable=protected-access

    terminal = await outbox.get(ready.continuation_id)
    assert terminal is not None
    assert terminal.status is ActionRetryContinuationStatus.DISPATCHED
    assert terminal.dispatch_id == completed_submission_id
    await control.close()


@pytest.mark.asyncio
async def test_workspace_dispatcher_executes_without_http_subscriber(
    tmp_path: Path,
) -> None:
    manager = ChatManager(
        repo=JsonChatRepository(tmp_path / "chats.json"),
    )
    chat = await manager.create_chat(
        ChatSpec(
            id="chat-1",
            session_id="console:chat-1",
            user_id="local-user",
            channel="console",
        ),
    )
    control = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    observed: list[dict] = []
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    class ConsoleChannel:
        async def stream_one(self, payload):
            observed.append(payload)
            context = payload["meta"]["request_context"]
            lease = await control.begin_submitted_turn(
                UUID(context["os_submission_id"]),
                invocation_id=uuid4(),
                agent_id="default",
                conversation_id=context["os_conversation_id"],
            )
            yield 'data: {"status":"running"}\n\n'
            await control.finish_turn(lease, SubmissionStatus.SUCCEEDED)

    class ChannelManager:
        @staticmethod
        async def get_channel(name: str):
            return ConsoleChannel() if name == "console" else None

    workspace = SimpleNamespace(
        agent_id="default",
        chat_manager=manager,
        channel_manager=ChannelManager(),
        task_tracker=TaskTracker(),
    )
    dispatcher = WorkspaceChatSubmissionDispatcher(
        workspace=workspace,
        control=control,
    )
    request = TurnSubmissionRequest(
        agent_id="default",
        conversation_id=chat.id,
        content="run independently",
        input_envelope=SubmissionInputEnvelope(
            kind=CONSOLE_SUBMISSION_ENVELOPE,
            payload={
                "channel_id": "console",
                "sender_id": "local-user",
                "content_parts": [
                    {"type": "text", "text": "run independently"},
                ],
                "message_id": "client-message-1",
                "message_metadata": {},
                "meta": {
                    "session_id": "console:chat-1",
                    "user_id": "local-user",
                    "request_context": {
                        "session_project_dirs": [
                            {
                                "path": str(project_dir),
                                "label": "Project",
                            },
                        ],
                    },
                },
            },
        ),
        idempotency_key="client-message-1",
    )

    await dispatcher.start()
    receipt = await dispatcher.enqueue(request, expected_revision=0)
    assert receipt.submission_id is not None
    async with asyncio.timeout(2):
        while True:
            record = await control.get_submission(receipt.submission_id)
            if (
                record is not None
                and record.status is SubmissionStatus.SUCCEEDED
            ):
                break
            await asyncio.sleep(0.01)

    assert len(observed) == 1
    context = observed[0]["meta"]["request_context"]
    assert context["os_submission_id"] == str(receipt.submission_id)
    assert context["os_conversation_id"] == chat.id
    assert context["os_submission_idempotency_key"] == "client-message-1"
    assert "session_project_dirs" not in context
    stored_chat = await manager.get_chat(chat.id)
    assert stored_chat is not None
    assert stored_chat.meta["runtime_context"]["project_dirs"] == [
        {"path": str(project_dir), "label": "Project"},
    ]
    assert await workspace.task_tracker.get_status(chat.id) == "idle"
    await dispatcher.stop()
    await control.close()


@pytest.mark.asyncio
async def test_workspace_dispatcher_recovers_interaction_continuation(
    tmp_path: Path,
) -> None:
    """A committed answer survives a crash before Submission enqueue."""
    manager = ChatManager(
        repo=JsonChatRepository(tmp_path / "chats.json"),
    )
    chat = await manager.create_chat(
        ChatSpec(
            id="chat-1",
            session_id="console:chat-1",
            user_id="local-user",
            channel="console",
        ),
    )
    control = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    interaction_path = tmp_path / "interactions.sqlite3"
    interactions = InteractionService(interaction_path)
    correlation_id = uuid4()
    request = InteractionRequest(
        kind=InteractionKind.USER_INPUT,
        mode=InteractionMode.BLOCKING,
        agent_id="default",
        conversation_id=chat.id,
        invocation_id=uuid4(),
        correlation_id=correlation_id,
        user_input_reason=UserInputReason.MATERIAL_PREFERENCE,
        continuation_mode=ContinuationMode.CONVERSATION_TURN,
        title="Choose output",
        prompt="Which format?",
        options=(
            InteractionOption(option_id="md", label="Markdown"),
            InteractionOption(option_id="html", label="HTML"),
        ),
    )
    await interactions.open(request)
    await interactions.resolve(
        InteractionResponse(
            interaction_id=request.interaction_id,
            idempotency_key="answer-1",
            expected_revision=1,
            actor=ActorRef(type=ActorType.USER, id="local-user"),
            selected_option_ids=("html",),
        ),
    )
    await interactions.close()

    observed: list[dict] = []

    class ConsoleChannel:
        async def stream_one(self, payload):
            observed.append(payload)
            context = payload["meta"]["request_context"]
            lease = await control.begin_submitted_turn(
                UUID(context["os_submission_id"]),
                invocation_id=uuid4(),
                agent_id="default",
                conversation_id=context["os_conversation_id"],
            )
            yield 'data: {"status":"running"}\n\n'
            await control.finish_turn(lease, SubmissionStatus.SUCCEEDED)

    class ChannelManager:
        @staticmethod
        async def get_channel(name: str):
            return ConsoleChannel() if name == "console" else None

    restarted = InteractionService(interaction_path)
    workspace = SimpleNamespace(
        agent_id="default",
        chat_manager=manager,
        channel_manager=ChannelManager(),
        task_tracker=TaskTracker(),
        interaction_service=restarted,
    )
    dispatcher = WorkspaceChatSubmissionDispatcher(
        workspace=workspace,
        control=control,
    )

    original_mark = restarted.mark_continuation_dispatched

    async def fail_after_enqueue(*_args, **_kwargs):
        raise RuntimeError("simulated crash after enqueue")

    restarted.mark_continuation_dispatched = fail_after_enqueue
    # White-box drain isolates the crash window between two durable stores.
    # pylint: disable=protected-access
    with pytest.raises(RuntimeError, match="simulated crash"):
        await dispatcher._dispatch_ready_continuations()
    restarted.mark_continuation_dispatched = original_mark
    await dispatcher._dispatch_ready_continuations()
    # pylint: enable=protected-access

    await dispatcher.start()
    async with asyncio.timeout(2):
        while not observed:
            await asyncio.sleep(0.01)

    [payload] = observed
    context = payload["meta"]["request_context"]
    assert payload["content_parts"] == [
        {
            "type": "text",
            "text": (
                "User response to QwenPaw interaction "
                f"{request.interaction_id}: HTML"
            ),
        },
    ]
    assert context["interaction_id"] == str(request.interaction_id)
    assert context["os_correlation_id"] == str(correlation_id)
    submission = await control.get_submission(
        UUID(context["os_submission_id"]),
    )
    assert submission is not None
    assert submission.input_envelope is not None
    assert (
        submission.input_envelope.kind
        == CONSOLE_INTERACTION_CONTINUATION_ENVELOPE
    )
    assert submission.correlation_id == correlation_id
    continuations = await restarted.list_ready_continuations(
        agent_id="default",
    )
    assert not continuations
    dispatched = await restarted.mark_continuation_dispatched(
        request.interaction_id,
        submission.submission_id,
    )
    assert dispatched.status is ContinuationDispatchStatus.DISPATCHED

    dispatcher.wake_continuations()
    await asyncio.sleep(0.05)
    assert len(observed) == 1
    await dispatcher.stop()
    await control.close()


@pytest.mark.asyncio
async def test_workspace_dispatcher_recovers_released_model_resource_wait(
    tmp_path: Path,
) -> None:
    """A released quota wait creates one new correlated Invocation."""
    manager = ChatManager(
        repo=JsonChatRepository(tmp_path / "chats.json"),
    )
    chat = await manager.create_chat(
        ChatSpec(
            id="chat-1",
            session_id="console:chat-1",
            user_id="local-user",
            channel="console",
        ),
    )
    control = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    resource_waits = ModelResourceWaitService(
        tmp_path / "resource-waits.sqlite3",
        agent_id="default",
    )
    attempt = ModelCallAttempt(
        attempt_id=uuid4(),
        route_decision_id=uuid4(),
        invocation_id=uuid4(),
        correlation_id=uuid4(),
        conversation_id=chat.id,
        registry_generation=1,
        context_manifest_id=uuid4(),
        model_call_index=1,
        attempt_index=1,
        provider_id="provider-a",
        model_id="model-a",
    )
    wait = await resource_waits.defer(
        attempt,
        ModelCallResult(
            attempt_id=attempt.attempt_id,
            invocation_id=attempt.invocation_id,
            conversation_id=chat.id,
            status=ModelCallStatus.FAILED,
            failure_class=ModelFailureClass.QUOTA_EXHAUSTED,
            recovery_disposition=ModelRecoveryDisposition.WAIT_RESOURCE,
        ),
    )
    assert wait is not None
    await resource_waits.release(wait.wait_id)
    observed: list[dict] = []

    class ConsoleChannel:
        async def stream_one(self, payload):
            observed.append(payload)
            context = payload["meta"]["request_context"]
            lease = await control.begin_submitted_turn(
                UUID(context["os_submission_id"]),
                invocation_id=uuid4(),
                agent_id="default",
                conversation_id=context["os_conversation_id"],
            )
            yield 'data: {"status":"running"}\n\n'
            await control.finish_turn(lease, SubmissionStatus.SUCCEEDED)

    class ChannelManager:
        @staticmethod
        async def get_channel(name: str):
            return ConsoleChannel() if name == "console" else None

    workspace = SimpleNamespace(
        agent_id="default",
        chat_manager=manager,
        channel_manager=ChannelManager(),
        task_tracker=TaskTracker(),
        model_resource_wait_service=resource_waits,
    )
    dispatcher = WorkspaceChatSubmissionDispatcher(
        workspace=workspace,
        control=control,
    )

    original_dispatch = resource_waits.dispatch_ready

    async def fail_after_enqueue(wait_id, dispatch):
        current = await resource_waits.get(wait_id)
        assert current is not None
        await dispatch(current)
        raise RuntimeError("simulated resource wait crash after enqueue")

    resource_waits.dispatch_ready = fail_after_enqueue
    # The second drain must obtain the same idempotent Submission.
    # pylint: disable=protected-access
    with pytest.raises(RuntimeError, match="simulated resource wait crash"):
        await dispatcher._dispatch_ready_resource_waits()
    resource_waits.dispatch_ready = original_dispatch
    await dispatcher._dispatch_ready_resource_waits()
    # pylint: enable=protected-access

    await dispatcher.start()
    async with asyncio.timeout(2):
        while not observed:
            await asyncio.sleep(0.01)

    [payload] = observed
    context = payload["meta"]["request_context"]
    submission = await control.get_submission(
        UUID(context["os_submission_id"]),
    )
    assert submission is not None
    assert submission.correlation_id == attempt.correlation_id
    assert submission.input_envelope is not None
    assert submission.input_envelope.kind == CONSOLE_MODEL_RECOVERY_ENVELOPE
    assert context["model_resource_wait_id"] == str(wait.wait_id)
    assert context["recovered_attempt_id"] == str(attempt.attempt_id)

    dispatcher.wake_resource_waits()
    await asyncio.sleep(0.05)
    assert len(observed) == 1
    await dispatcher.stop()
    await control.close()


@pytest.mark.asyncio
async def test_model_recovery_honors_stop_during_enqueue_race(
    tmp_path: Path,
) -> None:
    manager = ChatManager(
        repo=JsonChatRepository(tmp_path / "chats.json"),
    )
    chat = await manager.create_chat(
        ChatSpec(
            id="chat-1",
            session_id="console:chat-1",
            user_id="local-user",
            channel="console",
        ),
    )
    control = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    resource_waits = ModelResourceWaitService(
        tmp_path / "resource-waits.sqlite3",
        agent_id="default",
    )
    attempt = ModelCallAttempt(
        attempt_id=uuid4(),
        route_decision_id=uuid4(),
        invocation_id=uuid4(),
        correlation_id=uuid4(),
        conversation_id=chat.id,
        registry_generation=1,
        context_manifest_id=uuid4(),
        model_call_index=1,
        attempt_index=1,
        provider_id="provider-a",
        model_id="model-a",
    )
    wait = await resource_waits.defer(
        attempt,
        ModelCallResult(
            attempt_id=attempt.attempt_id,
            invocation_id=attempt.invocation_id,
            conversation_id=chat.id,
            status=ModelCallStatus.FAILED,
            failure_class=ModelFailureClass.QUOTA_EXHAUSTED,
            recovery_disposition=ModelRecoveryDisposition.WAIT_RESOURCE,
        ),
    )
    assert wait is not None
    await resource_waits.release(wait.wait_id)
    workspace = SimpleNamespace(
        agent_id="default",
        chat_manager=manager,
        model_resource_wait_service=resource_waits,
    )
    dispatcher = WorkspaceChatSubmissionDispatcher(
        workspace=workspace,
        control=control,
    )
    original_scan = control.scan_for_conversation

    async def stop_after_scan(**kwargs):
        records = await original_scan(**kwargs)
        await control.stop_and_clear(
            agent_id="default",
            conversation_id=chat.id,
            idempotency_key="stop-during-recovery-enqueue",
            expected_revision=0,
        )
        return records

    control.scan_for_conversation = stop_after_scan

    # pylint: disable=protected-access
    with pytest.raises(QueueRevisionConflictError):
        await dispatcher._dispatch_ready_resource_waits()
    control.scan_for_conversation = original_scan
    await dispatcher._dispatch_ready_resource_waits()
    # pylint: enable=protected-access

    cancelled = await resource_waits.get(wait.wait_id)
    queue = await control.read_queue(
        agent_id="default",
        conversation_id=chat.id,
    )
    assert cancelled is not None
    assert cancelled.status is ResourceWaitStatus.CANCELLED
    assert queue.submissions == ()
    await control.close()


@pytest.mark.asyncio
async def test_partial_model_step_recovers_one_correlated_submission(
    tmp_path: Path,
) -> None:
    manager = ChatManager(
        repo=JsonChatRepository(tmp_path / "chats.json"),
    )
    chat = await manager.create_chat(
        ChatSpec(
            id="chat-1",
            session_id="console:chat-1",
            user_id="local-user",
            channel="console",
        ),
    )
    control = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    recovery = ModelResourceWaitService(
        tmp_path / "resource-waits.sqlite3",
        agent_id="default",
    )
    attempt = ModelCallAttempt(
        attempt_id=uuid4(),
        route_decision_id=uuid4(),
        invocation_id=uuid4(),
        correlation_id=uuid4(),
        conversation_id=chat.id,
        registry_generation=1,
        context_manifest_id=uuid4(),
        model_call_index=1,
        attempt_index=1,
        provider_id="provider-a",
        model_id="model-a",
    )
    continuation = await recovery.defer_model_step(
        attempt,
        ModelCallResult(
            attempt_id=attempt.attempt_id,
            invocation_id=attempt.invocation_id,
            conversation_id=chat.id,
            status=ModelCallStatus.FAILED,
            emitted_content=True,
            output_boundary=ModelOutputBoundary.PARTIAL_STREAM,
            failure_class=ModelFailureClass.STREAM_INTERRUPTED,
            recovery_disposition=(
                ModelRecoveryDisposition.CONTINUE_MODEL_STEP
            ),
        ),
    )
    assert continuation is not None
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        chat_manager=manager,
        model_resource_wait_service=recovery,
    )
    dispatcher = WorkspaceChatSubmissionDispatcher(
        workspace=workspace,
        control=control,
    )
    original_dispatch = recovery.dispatch_model_step

    async def fail_after_enqueue(continuation_id, dispatch):
        current = await recovery.get_model_step(continuation_id)
        assert current is not None
        await dispatch(current)
        raise RuntimeError("simulated model-step crash after enqueue")

    recovery.dispatch_model_step = fail_after_enqueue
    # pylint: disable=protected-access
    with pytest.raises(RuntimeError, match="model-step crash"):
        await dispatcher._dispatch_ready_model_steps()
    recovery.dispatch_model_step = original_dispatch
    await dispatcher._dispatch_ready_model_steps()
    # pylint: enable=protected-access

    dispatched = await recovery.get_model_step(
        continuation.continuation_id,
    )
    assert dispatched is not None
    assert dispatched.status is ModelStepContinuationStatus.DISPATCHED
    assert dispatched.submission_id is not None
    submission = await control.get_submission(dispatched.submission_id)
    assert submission is not None
    assert submission.correlation_id == attempt.correlation_id
    assert submission.input_envelope is not None
    assert submission.input_envelope.kind == (
        CONSOLE_MODEL_STEP_CONTINUATION_ENVELOPE
    )
    # pylint: disable=protected-access
    payload = await dispatcher._materialize_model_step_payload(
        submission.input_envelope,
        chat,
        submission,
    )
    # pylint: enable=protected-access
    text = payload["content_parts"][0]["text"]
    assert "partial output" in text
    assert "model_step_continuation_id" in (
        payload["meta"]["request_context"]
    )
    queue = await control.read_queue(
        agent_id="default",
        conversation_id=chat.id,
    )
    assert len(queue.submissions) == 1
    await control.close()


@pytest.mark.asyncio
async def test_partial_model_step_stops_for_action_reconciliation(
    tmp_path: Path,
) -> None:
    manager = ChatManager(
        repo=JsonChatRepository(tmp_path / "chats.json"),
    )
    chat = await manager.create_chat(
        ChatSpec(
            id="chat-1",
            session_id="console:chat-1",
            user_id="local-user",
            channel="console",
        ),
    )
    control = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    recovery = ModelResourceWaitService(
        tmp_path / "resource-waits.sqlite3",
        agent_id="default",
    )
    attempt = ModelCallAttempt(
        attempt_id=uuid4(),
        route_decision_id=uuid4(),
        invocation_id=uuid4(),
        correlation_id=uuid4(),
        conversation_id=chat.id,
        registry_generation=1,
        context_manifest_id=uuid4(),
        model_call_index=1,
        attempt_index=1,
        provider_id="provider-a",
        model_id="model-a",
    )
    continuation = await recovery.defer_model_step(
        attempt,
        ModelCallResult(
            attempt_id=attempt.attempt_id,
            invocation_id=attempt.invocation_id,
            conversation_id=chat.id,
            status=ModelCallStatus.FAILED,
            emitted_content=True,
            output_boundary=ModelOutputBoundary.PARTIAL_STREAM,
            failure_class=ModelFailureClass.STREAM_INTERRUPTED,
            recovery_disposition=(
                ModelRecoveryDisposition.CONTINUE_MODEL_STEP
            ),
        ),
    )
    assert continuation is not None
    await lite_action_store(tmp_path).begin(
        ActionRequest(
            invocation_id=attempt.invocation_id,
            correlation_id=attempt.correlation_id,
            conversation_id=chat.id,
            registry_generation=1,
            capability_id="qwenpaw.system.test-tool",
            kind=ActionKind.TOOL,
            action_name="write_file",
            arguments={},
            redacted_arguments={},
            arguments_hash=f"sha256:{'a' * 64}",
            idempotency_key="action-before-partial-recovery",
        ),
    )
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        chat_manager=manager,
        model_resource_wait_service=recovery,
    )
    dispatcher = WorkspaceChatSubmissionDispatcher(
        workspace=workspace,
        control=control,
    )

    # pylint: disable=protected-access
    await dispatcher._dispatch_ready_model_steps()
    # pylint: enable=protected-access

    blocked = await recovery.get_model_step(
        continuation.continuation_id,
    )
    assert blocked is not None
    assert blocked.status is (
        ModelStepContinuationStatus.ACTION_RECONCILIATION_REQUIRED
    )
    assert blocked.reconciliation is not None
    assert blocked.reconciliation.reason is (
        ModelStepReconciliationReason.PENDING_ACTION_RESULT
    )
    assert blocked.reconciliation.action_count == 1
    assert blocked.reconciliation.pending_result_count == 1
    queue = await control.read_queue(
        agent_id="default",
        conversation_id=chat.id,
    )
    assert queue.submissions == ()
    await control.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "superseded", "expected_status"),
    [
        ("retry_once", False, ModelStepContinuationStatus.DISPATCHED),
        ("retry_once", True, ModelStepContinuationStatus.CANCELLED),
        ("stop", False, ModelStepContinuationStatus.CANCELLED),
    ],
)
async def test_uncertain_action_requires_exact_chat_decision(
    tmp_path: Path,
    decision: str,
    superseded: bool,
    expected_status: ModelStepContinuationStatus,
) -> None:
    manager = ChatManager(
        repo=JsonChatRepository(tmp_path / "chats.json"),
    )
    chat = await manager.create_chat(
        ChatSpec(
            id="chat-1",
            session_id="console:chat-1",
            user_id="local-user",
            channel="console",
        ),
    )
    control = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    recovery_path = tmp_path / "resource-waits.sqlite3"
    interaction_path = tmp_path / "interactions.sqlite3"
    recovery = ModelResourceWaitService(
        recovery_path,
        agent_id="default",
    )
    interactions = InteractionService(interaction_path)
    attempt = ModelCallAttempt(
        attempt_id=uuid4(),
        route_decision_id=uuid4(),
        invocation_id=uuid4(),
        correlation_id=uuid4(),
        conversation_id=chat.id,
        registry_generation=1,
        context_manifest_id=uuid4(),
        model_call_index=1,
        attempt_index=1,
        provider_id="provider-a",
        model_id="model-a",
    )
    continuation = await recovery.defer_model_step(
        attempt,
        ModelCallResult(
            attempt_id=attempt.attempt_id,
            invocation_id=attempt.invocation_id,
            conversation_id=chat.id,
            status=ModelCallStatus.FAILED,
            emitted_content=True,
            output_boundary=ModelOutputBoundary.PARTIAL_STREAM,
            failure_class=ModelFailureClass.STREAM_INTERRUPTED,
            recovery_disposition=(
                ModelRecoveryDisposition.CONTINUE_MODEL_STEP
            ),
        ),
    )
    assert continuation is not None
    source_lease = await control.begin_turn(
        TurnSubmissionRequest(
            agent_id="default",
            conversation_id=chat.id,
            content="perform external write",
            input_envelope=SubmissionInputEnvelope(
                kind=CONSOLE_SUBMISSION_ENVELOPE,
                payload={},
            ),
            idempotency_key="uncertain-source-with-interaction",
            correlation_id=attempt.correlation_id,
        ),
        invocation_id=attempt.invocation_id,
    )
    await control.finish_turn(source_lease, SubmissionStatus.FAILED)
    action = ActionRequest(
        invocation_id=attempt.invocation_id,
        correlation_id=attempt.correlation_id,
        conversation_id=chat.id,
        registry_generation=1,
        capability_id="qwenpaw.system.test-tool",
        kind=ActionKind.TOOL,
        action_name="external_write",
        arguments={},
        redacted_arguments={},
        arguments_hash=f"sha256:{'a' * 64}",
        idempotency_key="uncertain-action",
    )
    action_store = lite_action_store(tmp_path)
    await action_store.begin(action)
    await action_store.complete(
        ActionResult(
            action_id=action.action_id,
            invocation_id=action.invocation_id,
            conversation_id=action.conversation_id,
            status=ActionStatus.UNKNOWN,
            observation_digest=f"sha256:{'b' * 64}",
            side_effect_status=SideEffectStatus.UNCERTAIN,
        ),
    )
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        chat_manager=manager,
        interaction_service=interactions,
        model_resource_wait_service=recovery,
    )
    dispatcher = WorkspaceChatSubmissionDispatcher(
        workspace=workspace,
        control=control,
    )

    # pylint: disable=protected-access
    await dispatcher._dispatch_ready_model_steps()
    [interaction] = await interactions.list_open(
        agent_id="default",
        conversation_id=chat.id,
    )
    assert interaction.kind is InteractionKind.APPROVAL
    assert interaction.continuation_mode is ContinuationMode.CHECKPOINT
    assert interaction.continuation_checkpoint_id == (
        continuation.continuation_id
    )
    await interactions.resolve(
        InteractionResponse(
            interaction_id=interaction.interaction_id,
            idempotency_key=f"decision:{decision}",
            expected_revision=1,
            actor=ActorRef(type=ActorType.USER, id="local-user"),
            selected_option_ids=(decision,),
        ),
    )
    if superseded:
        await control.enqueue_turn(
            TurnSubmissionRequest(
                agent_id="default",
                conversation_id=chat.id,
                content="newer user direction",
                input_envelope=SubmissionInputEnvelope(
                    kind=CONSOLE_SUBMISSION_ENVELOPE,
                    payload={},
                ),
                idempotency_key="newer-user-submission",
                correlation_id=uuid4(),
            ),
        )
    recovery = ModelResourceWaitService(
        recovery_path,
        agent_id="default",
    )
    interactions = InteractionService(interaction_path)
    workspace.model_resource_wait_service = recovery
    workspace.interaction_service = interactions
    dispatcher = WorkspaceChatSubmissionDispatcher(
        workspace=workspace,
        control=control,
    )
    await dispatcher._dispatch_ready_model_steps()
    # pylint: enable=protected-access

    final = await recovery.get_model_step(continuation.continuation_id)
    assert final is not None
    assert final.status is expected_status
    queue = await control.read_queue(
        agent_id="default",
        conversation_id=chat.id,
    )
    if decision == "retry_once" and not superseded:
        assert final.retry_authorization is not None
        assert len(queue.submissions) == 1
        submission = queue.submissions[0]
        assert submission.input_envelope is not None
        # pylint: disable=protected-access
        payload = await dispatcher._materialize_model_step_payload(
            submission.input_envelope,
            chat,
            submission,
        )
        # pylint: enable=protected-access
        assert "verified that one retry" in (
            payload["content_parts"][0]["text"]
        )
        assert payload["meta"]["request_context"][
            "model_step_retry_interaction_id"
        ] == str(interaction.interaction_id)
    else:
        assert not any(
            item.idempotency_key.startswith("model-step-continuation:")
            for item in queue.submissions
        )
    await control.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("superseded", [False, True])
@pytest.mark.parametrize("context_form", ["tool_result", "assistant_hint"])
async def test_terminal_action_continues_from_immutable_context(
    tmp_path: Path,
    superseded: bool,
    context_form: str,
) -> None:
    manager = ChatManager(
        repo=JsonChatRepository(tmp_path / "chats.json"),
    )
    chat = await manager.create_chat(
        ChatSpec(
            id="chat-1",
            session_id="console:chat-1",
            user_id="local-user",
            channel="console",
        ),
    )
    control = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    recovery = ModelResourceWaitService(
        tmp_path / "resource-waits.sqlite3",
        agent_id="default",
    )
    invocation_id = uuid4()
    correlation_id = uuid4()
    lease = await control.begin_turn(
        TurnSubmissionRequest(
            agent_id="default",
            conversation_id=chat.id,
            content="do work",
            input_envelope=SubmissionInputEnvelope(
                kind=CONSOLE_SUBMISSION_ENVELOPE,
                payload={},
            ),
            idempotency_key="source-submission",
            correlation_id=correlation_id,
        ),
        invocation_id=invocation_id,
    )
    source_submission_id = lease.submission.submission_id
    await control.finish_turn(lease, SubmissionStatus.FAILED)
    attempt = ModelCallAttempt(
        attempt_id=uuid4(),
        route_decision_id=uuid4(),
        invocation_id=invocation_id,
        correlation_id=correlation_id,
        conversation_id=chat.id,
        registry_generation=1,
        context_manifest_id=uuid4(),
        model_call_index=2,
        attempt_index=1,
        provider_id="provider-a",
        model_id="model-a",
    )
    continuation = await recovery.defer_model_step(
        attempt,
        ModelCallResult(
            attempt_id=attempt.attempt_id,
            invocation_id=invocation_id,
            conversation_id=chat.id,
            status=ModelCallStatus.FAILED,
            emitted_content=True,
            output_boundary=ModelOutputBoundary.PARTIAL_STREAM,
            failure_class=ModelFailureClass.STREAM_INTERRUPTED,
            recovery_disposition=(
                ModelRecoveryDisposition.CONTINUE_MODEL_STEP
            ),
        ),
    )
    assert continuation is not None
    action = ActionRequest(
        invocation_id=invocation_id,
        correlation_id=correlation_id,
        conversation_id=chat.id,
        registry_generation=1,
        capability_id="qwenpaw.system.test-tool",
        kind=ActionKind.TOOL,
        action_name="write_file",
        redacted_arguments={},
        arguments_hash=f"sha256:{'a' * 64}",
        idempotency_key=f"tool:{invocation_id}:call-1",
    )
    action_store = lite_action_store(tmp_path)
    await action_store.begin(action)
    await action_store.complete(
        ActionResult(
            action_id=action.action_id,
            invocation_id=invocation_id,
            conversation_id=chat.id,
            status=ActionStatus.SUCCEEDED,
            observation_digest=f"sha256:{'b' * 64}",
        ),
    )
    actions = await action_store.scan_for_conversation(chat.id)
    evidence_digest = model_step_action_evidence_digest(
        actions,
        invocation_id,
    )
    assert evidence_digest is not None
    committed_items = model_step_committed_action_items(
        actions,
        invocation_id,
    )
    assert committed_items is not None
    [committed_item] = committed_items
    checkpoint = ModelStepContextCheckpoint(
        checkpoint_id=uuid5(
            continuation.continuation_id,
            "private-agent-context",
        ),
        continuation_id=continuation.continuation_id,
        invocation_id=invocation_id,
        conversation_id=chat.id,
        source_submission_id=source_submission_id,
        action_evidence_digest=evidence_digest,
        action_count=1,
    )
    binding = committed_item.model_dump(mode="json")
    context_message: dict[str, Any] = (
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_result",
                    "id": "call-1",
                    "name": "write_file",
                    "state": "success",
                    "output": "written",
                    "metadata": {
                        COMMITTED_ACTION_ITEM_METADATA_KEY: binding,
                    },
                },
            ],
        }
        if context_form == "tool_result"
        else {
            "role": "assistant",
            "content": [{"type": "text", "text": "written"}],
            "metadata": {
                COMMITTED_ACTION_ITEM_METADATA_KEY: binding,
            },
        }
    )
    await lite_model_step_context_store(tmp_path).save(
        checkpoint,
        {"state": {"context": [context_message]}},
    )
    if superseded:
        await control.enqueue_turn(
            TurnSubmissionRequest(
                agent_id="default",
                conversation_id=chat.id,
                content="newer user input",
                input_envelope=SubmissionInputEnvelope(
                    kind=CONSOLE_SUBMISSION_ENVELOPE,
                    payload={},
                ),
                idempotency_key="newer-submission",
            ),
        )
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        chat_manager=manager,
        model_resource_wait_service=recovery,
    )
    dispatcher = WorkspaceChatSubmissionDispatcher(
        workspace=workspace,
        control=control,
    )

    # pylint: disable=protected-access
    await dispatcher._dispatch_ready_model_steps()
    # pylint: enable=protected-access

    dispatched = await recovery.get_model_step(
        continuation.continuation_id,
    )
    assert dispatched is not None
    if superseded:
        assert dispatched.status is ModelStepContinuationStatus.CANCELLED
        assert dispatched.submission_id is None
        await control.close()
        return
    assert dispatched.status is ModelStepContinuationStatus.DISPATCHED
    assert dispatched.context_checkpoint == checkpoint
    assert dispatched.submission_id is not None
    assert "written" not in dispatched.model_dump_json()
    submission = await control.get_submission(dispatched.submission_id)
    assert submission is not None
    assert submission.input_envelope is not None
    # pylint: disable=protected-access
    payload = await dispatcher._materialize_model_step_payload(
        submission.input_envelope,
        chat,
        submission,
    )
    # pylint: enable=protected-access
    assert payload["meta"]["request_context"][
        "model_step_context_checkpoint_id"
    ] == str(checkpoint.checkpoint_id)
    await control.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("superseded", [False, True])
# pylint: disable-next=too-many-statements
async def test_harness_step_dispatches_one_fenced_submission(
    tmp_path: Path,
    superseded: bool,
) -> None:
    manager = ChatManager(
        repo=JsonChatRepository(tmp_path / "chats.json"),
    )
    chat = await manager.create_chat(
        ChatSpec(
            id="chat-1",
            session_id="console:chat-1",
            user_id="local-user",
            channel="console",
        ),
    )
    control = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    invocation_id = uuid4()
    correlation_id = uuid4()
    lease = await control.begin_turn(
        TurnSubmissionRequest(
            agent_id="default",
            conversation_id=chat.id,
            content="edit project",
            input_envelope=SubmissionInputEnvelope(
                kind=CONSOLE_SUBMISSION_ENVELOPE,
                payload={},
            ),
            idempotency_key="harness-source-submission",
            correlation_id=correlation_id,
        ),
        invocation_id=invocation_id,
    )
    await control.finish_turn(lease, SubmissionStatus.FAILED)
    committed_item = CommittedActionItem(
        action_id=uuid4(),
        invocation_id=invocation_id,
        conversation_id=chat.id,
        executor_item_id="tool-1",
        observation_digest=f"sha256:{'a' * 64}",
    )
    checkpoint = build_harness_recovery_checkpoint(
        invocation_id=invocation_id,
        conversation_id=chat.id,
        source_submission_id=lease.submission.submission_id,
        backend="codex",
        provider_context_id="private-thread-id",
        provider_item_ids={"tool-1"},
        action_evidence_digest=f"sha256:{'b' * 64}",
        expected_items=(committed_item,),
        session_items=(committed_item,),
    )
    assert checkpoint is not None
    await lite_harness_recovery_context_store(tmp_path).save(checkpoint)
    recovery = lite_harness_step_continuation_store(tmp_path)
    continuation = await recovery.defer(
        checkpoint,
        correlation_id=correlation_id,
        agent_id="default",
        recovery_cycle=1,
    )
    if superseded:
        await control.enqueue_turn(
            TurnSubmissionRequest(
                agent_id="default",
                conversation_id=chat.id,
                content="newer user instruction",
                input_envelope=SubmissionInputEnvelope(
                    kind=CONSOLE_SUBMISSION_ENVELOPE,
                    payload={},
                ),
                idempotency_key="newer-user-submission",
            ),
        )
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        chat_manager=manager,
    )
    dispatcher = WorkspaceChatSubmissionDispatcher(
        workspace=workspace,
        control=control,
    )

    # pylint: disable=protected-access
    dispatcher_store = dispatcher._harness_steps
    assert dispatcher_store is not None
    original_dispatch = dispatcher_store.dispatch
    if not superseded:

        async def fail_after_enqueue(continuation_id, dispatch):
            current = await dispatcher_store.get(continuation_id)
            assert current is not None
            await dispatch(current)
            raise RuntimeError("simulated Harness crash after enqueue")

        dispatcher_store.dispatch = fail_after_enqueue
        with pytest.raises(RuntimeError, match="crash after enqueue"):
            await dispatcher._dispatch_ready_harness_steps()
        dispatcher_store.dispatch = original_dispatch
    await dispatcher._dispatch_ready_harness_steps()
    await dispatcher._dispatch_ready_harness_steps()
    # pylint: enable=protected-access

    dispatched = await recovery.get(continuation.continuation_id)
    assert dispatched is not None
    if superseded:
        assert dispatched.status is HarnessStepContinuationStatus.CANCELLED
        assert dispatched.submission_id is None
        await control.close()
        return
    assert dispatched.status is HarnessStepContinuationStatus.DISPATCHED
    assert dispatched.submission_id is not None
    submission = await control.get_submission(dispatched.submission_id)
    assert submission is not None
    assert submission.correlation_id == correlation_id
    assert submission.input_envelope is not None
    assert submission.input_envelope.kind == (
        CONSOLE_HARNESS_STEP_CONTINUATION_ENVELOPE
    )
    queue = await control.read_queue(
        agent_id="default",
        conversation_id=chat.id,
    )
    assert len(queue.submissions) == 1
    # pylint: disable=protected-access
    payload = await dispatcher._materialize_harness_step_payload(
        submission.input_envelope,
        chat,
        submission,
    )
    # pylint: enable=protected-access
    request_context = payload["meta"]["request_context"]
    assert request_context["harness_backend"] == "codex"
    assert request_context["harness_recovery_cycle"] == 1
    assert request_context["harness_recovery_checkpoint_id"] == str(
        checkpoint.checkpoint_id,
    )
    await control.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("superseded", [False, True])
# pylint: disable-next=too-many-statements
async def test_background_action_repairs_snapshot_and_dispatches(
    tmp_path: Path,
    superseded: bool,
) -> None:
    manager = ChatManager(
        repo=JsonChatRepository(tmp_path / "chats.json"),
    )
    chat = await manager.create_chat(
        ChatSpec(
            id="chat-1",
            session_id="console:chat-1",
            user_id="local-user",
            channel="console",
        ),
    )
    control = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    invocation_id = uuid4()
    correlation_id = uuid4()
    lease = await control.begin_turn(
        TurnSubmissionRequest(
            agent_id="default",
            conversation_id=chat.id,
            content="run background work",
            input_envelope=SubmissionInputEnvelope(
                kind=CONSOLE_SUBMISSION_ENVELOPE,
                payload={},
            ),
            idempotency_key="background-source",
            correlation_id=correlation_id,
        ),
        invocation_id=invocation_id,
    )
    await control.finish_turn(lease, SubmissionStatus.SUCCEEDED)
    committed_item = CommittedActionItem(
        action_id=uuid4(),
        invocation_id=invocation_id,
        conversation_id=chat.id,
        executor_item_id="call-background",
        observation_digest=f"sha256:{'a' * 64}",
    )
    action_store = lite_action_store(tmp_path)

    async def commit_action(item: CommittedActionItem) -> None:
        await action_store.begin(
            ActionRequest(
                action_id=item.action_id,
                invocation_id=item.invocation_id,
                correlation_id=correlation_id,
                conversation_id=chat.id,
                registry_generation=1,
                capability_id="qwenpaw.system.test-tool",
                kind=ActionKind.TOOL,
                action_name="slow_tool",
                redacted_arguments={},
                arguments_hash=f"sha256:{'c' * 64}",
                idempotency_key=f"tool:{item.action_id}",
            ),
        )
        await action_store.complete(
            ActionResult(
                action_id=item.action_id,
                invocation_id=item.invocation_id,
                conversation_id=chat.id,
                status=ActionStatus.SUCCEEDED,
                observation_digest=item.observation_digest,
            ),
        )

    await commit_action(committed_item)
    checkpoint = build_background_action_checkpoint(
        committed_item=committed_item,
        source_submission_id=lease.submission.submission_id,
        correlation_id=correlation_id,
        agent_id="default",
        recovery_cycle=1,
    )
    hint = Msg(
        name="system",
        role="assistant",
        content=[TextBlock(type="text", text="private result")],
        metadata={
            COMMITTED_ACTION_ITEM_METADATA_KEY: (
                committed_item.model_dump(mode="json")
            ),
        },
    )
    snapshot = build_background_action_snapshot(
        {"state": {"context": []}},
        hint,
        committed_item,
    )
    await lite_background_action_context_store(tmp_path).save(
        checkpoint,
        snapshot,
    )
    sibling_item = committed_item.model_copy(
        update={
            "action_id": uuid4(),
            "executor_item_id": "call-background-2",
            "observation_digest": f"sha256:{'b' * 64}",
        },
    )
    await commit_action(sibling_item)
    sibling_checkpoint = build_background_action_checkpoint(
        committed_item=sibling_item,
        source_submission_id=lease.submission.submission_id,
        correlation_id=correlation_id,
        agent_id="default",
        recovery_cycle=1,
    )
    sibling_hint = Msg(
        name="system",
        role="assistant",
        content=[TextBlock(type="text", text="private result 2")],
        metadata={
            COMMITTED_ACTION_ITEM_METADATA_KEY: (
                sibling_item.model_dump(mode="json")
            ),
        },
    )
    sibling_snapshot = build_background_action_snapshot(
        {"state": {"context": []}},
        sibling_hint,
        sibling_item,
    )
    await lite_background_action_context_store(tmp_path).save(
        sibling_checkpoint,
        sibling_snapshot,
    )
    orphan_item = committed_item.model_copy(
        update={
            "action_id": uuid4(),
            "executor_item_id": "call-orphan",
            "observation_digest": f"sha256:{'d' * 64}",
        },
    )
    orphan_checkpoint = build_background_action_checkpoint(
        committed_item=orphan_item,
        source_submission_id=lease.submission.submission_id,
        correlation_id=correlation_id,
        agent_id="default",
        recovery_cycle=1,
    )
    orphan_hint = Msg(
        name="system",
        role="assistant",
        content=[TextBlock(type="text", text="orphan result")],
        metadata={
            COMMITTED_ACTION_ITEM_METADATA_KEY: (
                orphan_item.model_dump(mode="json")
            ),
        },
    )
    orphan_snapshot = build_background_action_snapshot(
        {"state": {"context": []}},
        orphan_hint,
        orphan_item,
    )
    await lite_background_action_context_store(tmp_path).save(
        orphan_checkpoint,
        orphan_snapshot,
    )
    if superseded:
        await control.enqueue_turn(
            TurnSubmissionRequest(
                agent_id="default",
                conversation_id=chat.id,
                content="newer instruction",
                input_envelope=SubmissionInputEnvelope(
                    kind=CONSOLE_SUBMISSION_ENVELOPE,
                    payload={},
                ),
                idempotency_key="background-newer-input",
            ),
        )
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        chat_manager=manager,
        session=SafeJSONSession(str(tmp_path / "sessions")),
    )
    dispatcher = WorkspaceChatSubmissionDispatcher(
        workspace=workspace,
        control=control,
    )

    # pylint: disable=protected-access
    if not superseded:
        dispatcher_store = dispatcher._background_actions
        assert dispatcher_store is not None
        original_dispatch = dispatcher_store.dispatch

        async def fail_after_enqueue(continuation_id, dispatch):
            current = await dispatcher_store.get(continuation_id)
            assert current is not None
            await dispatch(current)
            raise RuntimeError("simulated background Action enqueue crash")

        dispatcher_store.dispatch = fail_after_enqueue
        with pytest.raises(RuntimeError, match="enqueue crash"):
            await dispatcher._dispatch_ready_background_actions()
        dispatcher_store.dispatch = original_dispatch
    await dispatcher._dispatch_ready_background_actions()
    # pylint: enable=protected-access

    outbox = lite_background_action_continuation_store(tmp_path)
    continuation = await outbox.get(checkpoint.continuation_id)
    sibling = await outbox.get(sibling_checkpoint.continuation_id)
    orphan = await outbox.get(orphan_checkpoint.continuation_id)
    assert continuation is not None
    assert sibling is not None
    assert orphan is None
    if superseded:
        assert continuation.status is (
            BackgroundActionContinuationStatus.CANCELLED
        )
        assert continuation.submission_id is None
        assert sibling.status is BackgroundActionContinuationStatus.CANCELLED
        assert sibling.submission_id is None
        await control.close()
        return
    assert continuation.status is (
        BackgroundActionContinuationStatus.DISPATCHED
    )
    assert sibling.status is BackgroundActionContinuationStatus.DISPATCHED
    assert sibling.submission_id is not None
    assert continuation.submission_id is not None
    submission = await control.get_submission(continuation.submission_id)
    assert submission is not None
    assert submission.correlation_id == correlation_id
    assert submission.input_envelope is not None
    assert submission.input_envelope.kind == (
        CONSOLE_BACKGROUND_ACTION_CONTINUATION_ENVELOPE
    )
    # pylint: disable=protected-access
    payload = await dispatcher._materialize_background_action_payload(
        submission.input_envelope,
        chat,
        submission,
    )
    # pylint: enable=protected-access
    context = payload["meta"]["request_context"]
    assert context["background_action_checkpoint_id"] == str(
        checkpoint.checkpoint_id,
    )
    assert context["background_action_recovery_cycle"] == 1
    await control.close()
