# -*- coding: utf-8 -*-
"""Tests for recoverable conversation runtime projections."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from qwenpaw.interactions import InteractionService
from qwenpaw.invocation_control import (
    ConversationRuntimeProjectionService,
    InvocationControlService,
    SQLiteInvocationControl,
)
from qwenpaw.kernel import (
    ConversationExecutionState,
    ConversationOutcome,
    ConversationOutcomeStatus,
    ConversationRuntimeProjection,
    InteractionKind,
    InteractionMode,
    InteractionRequest,
    UserInputReason,
    ObservationCategory,
    ObservationPage,
    ObservationSource,
    ObservationStage,
    ObservationStatus,
    RuntimeObservation,
    SubmissionStatus,
    TurnSubmissionRequest,
)
from qwenpaw.kernel.models import utc_now
from qwenpaw.runtime.outcomes import lite_conversation_outcome_store


@pytest.mark.asyncio
async def test_projection_cursor_tracks_both_durable_sources(tmp_path) -> None:
    control = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    interactions = InteractionService(tmp_path / "interactions.sqlite3")
    projection_service = ConversationRuntimeProjectionService(
        control,
        interactions,
    )
    first = await projection_service.read(
        agent_id="default",
        conversation_id="chat-1",
    )
    repeated = await projection_service.read(
        agent_id="default",
        conversation_id="chat-1",
    )
    unchanged = await projection_service.wait_for_change(
        agent_id="default",
        conversation_id="chat-1",
        after_cursor=first.cursor,
        timeout=0.01,
        poll_interval=0.001,
    )

    invocation_id = uuid4()
    interaction = InteractionRequest(
        kind=InteractionKind.USER_INPUT,
        mode=InteractionMode.BLOCKING,
        agent_id="default",
        conversation_id="chat-1",
        invocation_id=invocation_id,
        user_input_reason=UserInputReason.MATERIAL_PREFERENCE,
        title="Need input",
        prompt="Choose one option.",
    )
    await interactions.open(interaction)
    opened = await projection_service.wait_for_change(
        agent_id="default",
        conversation_id="chat-1",
        after_cursor=first.cursor,
        timeout=1,
    )
    assert opened is not None

    await interactions.cancel_invocation(
        invocation_id,
        detail="test finished",
        include_non_blocking=False,
    )
    closed = await projection_service.wait_for_change(
        agent_id="default",
        conversation_id="chat-1",
        after_cursor=opened.cursor,
        timeout=1,
    )

    assert repeated.cursor == first.cursor
    assert unchanged is None
    assert opened.interactions == (interaction,)
    assert closed is not None
    assert closed.interactions == ()
    assert closed.cursor == first.cursor

    assert control.store is not None
    await control.store.submit(
        TurnSubmissionRequest(
            agent_id="default",
            conversation_id="chat-1",
            content="queued input",
            idempotency_key="turn-1",
        ),
        expected_revision=0,
    )
    queued = await projection_service.wait_for_change(
        agent_id="default",
        conversation_id="chat-1",
        after_cursor=closed.cursor,
        timeout=1,
    )
    assert queued is not None
    assert queued.queue.revision == 1
    assert len(queued.queue.submissions) == 1
    await control.close()


@pytest.mark.asyncio
async def test_projection_cursor_tracks_semantic_activity(tmp_path) -> None:
    control = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    interactions = InteractionService(tmp_path / "interactions.sqlite3")
    activity = AsyncMock()
    activity.page_for_conversation.return_value = ObservationPage()
    projection_service = ConversationRuntimeProjectionService(
        control,
        interactions,
        activity,
    )
    initial = await projection_service.read(
        agent_id="default",
        conversation_id="chat-1",
    )
    observation = RuntimeObservation(
        observation_id=uuid4(),
        category=ObservationCategory.ACTION,
        stage=ObservationStage.EXECUTION,
        status=ObservationStatus.RUNNING,
        source=ObservationSource(
            source_type="qwenpaw.action.request",
            source_id=str(uuid4()),
        ),
        conversation_id="chat-1",
        title="Action running",
        occurred_at=utc_now(),
    )
    activity.page_for_conversation.return_value = ObservationPage(
        items=(observation,),
    )

    changed = await projection_service.wait_for_change(
        agent_id="default",
        conversation_id="chat-1",
        after_cursor=initial.cursor,
        timeout=1,
    )

    assert changed is not None
    assert changed.activity.items == (observation,)
    assert changed.cursor != initial.cursor
    activity.page_for_conversation.assert_awaited_with(
        "chat-1",
        limit=50,
    )
    foreign = observation.model_copy(
        update={"conversation_id": "chat-2"},
    )
    with pytest.raises(
        ValidationError,
        match="activity conversation_id mismatch",
    ):
        ConversationRuntimeProjection(
            agent_id="default",
            conversation_id="chat-1",
            queue=initial.queue,
            activity=ObservationPage(items=(foreign,)),
            cursor="v2-invalid-owner",
        )
    await control.close()


@pytest.mark.asyncio
async def test_execution_chain_does_not_treat_response_as_outcome(
    tmp_path,
) -> None:
    control = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    interactions = InteractionService(tmp_path / "interactions.sqlite3")
    outcomes = lite_conversation_outcome_store(tmp_path)
    projection_service = ConversationRuntimeProjectionService(
        control,
        interactions,
        outcomes=outcomes,
    )
    correlation_id = uuid4()
    first_invocation_id = uuid4()
    first = await control.begin_turn(
        TurnSubmissionRequest(
            agent_id="default",
            conversation_id="chat-1",
            content="complete a multi-step intent",
            idempotency_key="intent-source",
            correlation_id=correlation_id,
        ),
        invocation_id=first_invocation_id,
    )
    await control.finish_turn(first, SubmissionStatus.SUCCEEDED)

    inactive = await projection_service.read(
        agent_id="default",
        conversation_id="chat-1",
    )

    assert len(inactive.execution_chains) == 1
    [chain] = inactive.execution_chains
    assert chain.correlation_id == correlation_id
    assert chain.state is ConversationExecutionState.INACTIVE
    assert chain.latest_submission_status is SubmissionStatus.SUCCEEDED
    assert chain.invocation_ids == (first_invocation_id,)

    outcome = ConversationOutcome(
        agent_id="default",
        conversation_id="chat-1",
        correlation_id=correlation_id,
        status=ConversationOutcomeStatus.ACHIEVED,
        producer_id="qwenpaw.system.verifier",
        summary="The requested result passed explicit verification.",
    )
    await outcomes.append(outcome)
    achieved = await projection_service.read(
        agent_id="default",
        conversation_id="chat-1",
    )
    [chain] = achieved.execution_chains
    assert chain.state is ConversationExecutionState.ACHIEVED
    assert chain.outcome == outcome

    interaction = InteractionRequest(
        kind=InteractionKind.USER_INPUT,
        mode=InteractionMode.BLOCKING,
        agent_id="default",
        conversation_id="chat-1",
        invocation_id=first_invocation_id,
        correlation_id=correlation_id,
        user_input_reason=UserInputReason.MATERIAL_PREFERENCE,
        title="Choose format",
        prompt="Markdown or HTML?",
    )
    await interactions.open(interaction)
    waiting = await projection_service.read(
        agent_id="default",
        conversation_id="chat-1",
    )

    [chain] = waiting.execution_chains
    assert chain.state is ConversationExecutionState.WAITING_USER
    assert chain.open_interaction_ids == (interaction.interaction_id,)

    await interactions.cancel_invocation(
        first_invocation_id,
        detail="continued in a new invocation",
        include_non_blocking=False,
    )
    second_invocation_id = uuid4()
    second = await control.begin_turn(
        TurnSubmissionRequest(
            agent_id="default",
            conversation_id="chat-1",
            content="[durable continuation]",
            idempotency_key="intent-continuation",
            correlation_id=correlation_id,
        ),
        invocation_id=second_invocation_id,
    )
    running = await projection_service.read(
        agent_id="default",
        conversation_id="chat-1",
    )

    [chain] = running.execution_chains
    assert chain.state is ConversationExecutionState.RUNNING
    assert chain.submission_ids == (
        first.submission.submission_id,
        second.submission.submission_id,
    )
    assert chain.invocation_ids == (
        first_invocation_id,
        second_invocation_id,
    )
    assert chain.head_invocation_id == second_invocation_id
    await control.finish_turn(second, SubmissionStatus.SUCCEEDED)
    await control.close()


@pytest.mark.asyncio
async def test_execution_chain_projection_reports_bounded_window(
    tmp_path,
) -> None:
    control = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    interactions = InteractionService(tmp_path / "interactions.sqlite3")
    projection_service = ConversationRuntimeProjectionService(
        control,
        interactions,
        execution_submission_limit=1,
    )
    older_correlation_id = uuid4()
    newer_correlation_id = uuid4()
    for idempotency_key, correlation_id in (
        ("older-intent", older_correlation_id),
        ("newer-intent", newer_correlation_id),
    ):
        assert control.store is not None
        await control.store.submit(
            TurnSubmissionRequest(
                agent_id="default",
                conversation_id="chat-1",
                content=idempotency_key,
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
            ),
        )

    projection = await projection_service.read(
        agent_id="default",
        conversation_id="chat-1",
    )

    assert projection.execution_window_truncated is True
    assert {
        item.correlation_id for item in projection.execution_chains
    } == {older_correlation_id, newer_correlation_id}
    await control.close()
