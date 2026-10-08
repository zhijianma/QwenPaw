# -*- coding: utf-8 -*-
"""Tests for durable model-resource waits and restart recovery."""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from qwenpaw.kernel import (
    ModelCallAttempt,
    ModelCallResult,
    ModelCallStatus,
    ModelFailureClass,
    ModelOutputBoundary,
    ModelRecoveryDisposition,
    ModelStepContextCheckpoint,
    ModelStepReconciliation,
    ModelStepReconciliationReason,
    ModelStepRetryAuthorization,
    ModelStepContinuationStatus,
    ResourceWaitStatus,
    ResourceWaitTrigger,
    WaitConditionKind,
    WaitConditionStatus,
)
from qwenpaw.recovery import ModelResourceWaitService
from qwenpaw.recovery.model_resource_waits import (
    ModelRecoveryHistory,
    ModelResourceWaitConflictError,
)


def _attempt(
    *,
    provider_id: str = "provider-a",
    model_id: str = "model-a",
    conversation_id: str = "chat-1",
) -> ModelCallAttempt:
    attempt_id = uuid4()
    return ModelCallAttempt(
        attempt_id=attempt_id,
        route_decision_id=uuid4(),
        invocation_id=uuid4(),
        correlation_id=uuid4(),
        conversation_id=conversation_id,
        registry_generation=1,
        context_manifest_id=uuid4(),
        model_call_index=1,
        attempt_index=1,
        provider_id=provider_id,
        model_id=model_id,
    )


def _result(
    attempt: ModelCallAttempt,
    failure_class: ModelFailureClass,
    *,
    retry_after_seconds: float | None = None,
) -> ModelCallResult:
    return ModelCallResult(
        attempt_id=attempt.attempt_id,
        invocation_id=attempt.invocation_id,
        conversation_id=attempt.conversation_id,
        status=ModelCallStatus.FAILED,
        error_kind=failure_class.value,
        failure_class=failure_class,
        recovery_disposition=ModelRecoveryDisposition.WAIT_RESOURCE,
        retry_after_seconds=retry_after_seconds,
    )


@pytest.mark.asyncio
async def test_quota_wait_requires_external_release_and_survives_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "resource-waits.sqlite3"
    attempt = _attempt()
    service = ModelResourceWaitService(database, agent_id="default")
    result = _result(attempt, ModelFailureClass.QUOTA_EXHAUSTED)

    wait = await service.defer(
        attempt,
        result,
    )

    assert wait is not None
    assert wait.trigger is ResourceWaitTrigger.EXTERNAL_EVENT
    assert wait.status is ResourceWaitStatus.WAITING
    assert wait.provider_id == attempt.provider_id
    assert wait.model_id == attempt.model_id
    assert not await service.list_ready()

    restarted = ModelResourceWaitService(database, agent_id="default")
    released = await restarted.release(wait.wait_id)
    [ready] = await restarted.list_ready()

    assert released.status is ResourceWaitStatus.READY
    assert ready.wait_id == wait.wait_id
    assert not await restarted.list_wait_conditions(
        agent_id="default",
        conversation_id="chat-1",
    )
    [released_condition] = await restarted.list_wait_conditions(
        agent_id="default",
        conversation_id="chat-1",
        include_terminal=True,
    )
    assert released_condition.status is WaitConditionStatus.SATISFIED
    submission_id = uuid4()
    dispatched = await restarted.mark_dispatched(
        wait.wait_id,
        submission_id,
    )
    replayed = await restarted.mark_dispatched(
        wait.wait_id,
        submission_id,
    )
    assert dispatched.status is ResourceWaitStatus.DISPATCHED
    assert replayed == dispatched
    replayed_defer = await restarted.defer(attempt, result)
    assert replayed_defer == dispatched

    [condition] = await restarted.list_wait_conditions(
        agent_id="default",
        conversation_id="chat-1",
        include_terminal=True,
    )
    assert condition.kind is WaitConditionKind.RESOURCE
    assert condition.status is WaitConditionStatus.SATISFIED
    assert condition.source_id == attempt.attempt_id


@pytest.mark.asyncio
async def test_provider_health_releases_only_exact_quota_waits(
    tmp_path: Path,
) -> None:
    service = ModelResourceWaitService(
        tmp_path / "resource-waits.sqlite3",
        agent_id="default",
    )
    matching_attempt = _attempt(conversation_id="chat-matching")
    other_model_attempt = _attempt(
        model_id="model-b",
        conversation_id="chat-other-model",
    )
    rate_limited_attempt = _attempt(
        conversation_id="chat-rate-limited",
    )
    matching = await service.defer(
        matching_attempt,
        _result(
            matching_attempt,
            ModelFailureClass.QUOTA_EXHAUSTED,
        ),
    )
    other_model = await service.defer(
        other_model_attempt,
        _result(
            other_model_attempt,
            ModelFailureClass.QUOTA_EXHAUSTED,
        ),
    )
    rate_limited = await service.defer(
        rate_limited_attempt,
        _result(
            rate_limited_attempt,
            ModelFailureClass.RATE_LIMITED,
        ),
    )
    assert matching is not None
    assert other_model is not None
    assert rate_limited is not None

    released = await service.release_provider_resource(
        provider_id="provider-a",
        model_id="model-a",
    )
    replay = await service.release_provider_resource(
        provider_id="provider-a",
        model_id="model-a",
    )

    assert [item.wait_id for item in released] == [matching.wait_id]
    assert replay == ()
    assert (await service.get(matching.wait_id)).status is (
        ResourceWaitStatus.READY
    )
    assert (await service.get(other_model.wait_id)).status is (
        ResourceWaitStatus.WAITING
    )
    assert (await service.get(rate_limited.wait_id)).status is (
        ResourceWaitStatus.WAITING
    )


@pytest.mark.asyncio
async def test_legacy_wait_schema_preserves_unscoped_waits(
    tmp_path: Path,
) -> None:
    database = tmp_path / "resource-waits.sqlite3"
    wait_id = uuid4()
    attempt_id = uuid4()
    invocation_id = uuid4()
    correlation_id = uuid4()
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE model_resource_waits ("
            "wait_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL UNIQUE, "
            "invocation_id TEXT NOT NULL, correlation_id TEXT NOT NULL, "
            "agent_id TEXT NOT NULL, conversation_id TEXT NOT NULL, "
            "failure_class TEXT NOT NULL, trigger_kind TEXT NOT NULL, "
            "status TEXT NOT NULL, not_before TEXT, submission_id TEXT, "
            "revision INTEGER NOT NULL, created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL)",
        )
        connection.execute(
            "INSERT INTO model_resource_waits VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(wait_id),
                str(attempt_id),
                str(invocation_id),
                str(correlation_id),
                "default",
                "chat-legacy",
                ModelFailureClass.QUOTA_EXHAUSTED.value,
                ResourceWaitTrigger.EXTERNAL_EVENT.value,
                ResourceWaitStatus.WAITING.value,
                None,
                None,
                1,
                now,
                now,
            ),
        )
    service = ModelResourceWaitService(database, agent_id="default")

    await service.start()
    legacy = await service.get(wait_id)
    released = await service.release_provider_resource(
        provider_id="provider-a",
        model_id="model-a",
    )

    assert legacy is not None
    assert legacy.provider_id is None
    assert legacy.model_id is None
    assert released == ()


@pytest.mark.asyncio
async def test_rate_limit_wait_uses_timer_without_provider_payload(
    tmp_path: Path,
) -> None:
    attempt = _attempt()
    service = ModelResourceWaitService(
        tmp_path / "resource-waits.sqlite3",
        agent_id="default",
        rate_limit_delay_seconds=5,
    )

    wait = await service.defer(
        attempt,
        _result(attempt, ModelFailureClass.RATE_LIMITED),
    )

    assert wait is not None
    assert wait.trigger is ResourceWaitTrigger.TIMER
    assert wait.not_before is not None
    assert (wait.not_before - wait.created_at).total_seconds() == 5
    assert 0 < await service.seconds_until_next_timer() <= 5
    assert not await service.list_ready()


@pytest.mark.asyncio
async def test_rate_limit_wait_prefers_durable_retry_after_hint(
    tmp_path: Path,
) -> None:
    attempt = _attempt()
    service = ModelResourceWaitService(
        tmp_path / "resource-waits.sqlite3",
        agent_id="default",
        rate_limit_delay_seconds=5,
    )

    result = _result(
        attempt,
        ModelFailureClass.RATE_LIMITED,
        retry_after_seconds=120.0,
    )
    wait = await service.defer(attempt, result)

    assert wait is not None
    assert wait.not_before is not None
    delay = (wait.not_before - wait.created_at).total_seconds()
    assert delay == 120.0
    [condition] = await service.list_wait_conditions(
        agent_id="default",
        conversation_id="chat-1",
    )
    assert condition.not_before == wait.not_before

    restarted = ModelResourceWaitService(
        tmp_path / "resource-waits.sqlite3",
        agent_id="default",
        rate_limit_delay_seconds=1,
    )
    replayed = await restarted.defer(attempt, result)
    assert replayed == wait


@pytest.mark.asyncio
async def test_conversation_cancel_fences_wait_before_dispatch(
    tmp_path: Path,
) -> None:
    attempt = _attempt()
    service = ModelResourceWaitService(
        tmp_path / "resource-waits.sqlite3",
        agent_id="default",
    )
    wait = await service.defer(
        attempt,
        _result(attempt, ModelFailureClass.QUOTA_EXHAUSTED),
    )
    assert wait is not None
    await service.release(wait.wait_id)

    [cancelled] = await service.cancel_for_conversation(
        agent_id="default",
        conversation_id="chat-1",
    )
    dispatched = False

    async def dispatch(_wait):
        nonlocal dispatched
        dispatched = True
        return uuid4()

    replay = await service.dispatch_ready(wait.wait_id, dispatch)

    assert cancelled.status is ResourceWaitStatus.CANCELLED
    assert replay == cancelled
    assert not dispatched
    assert not await service.list_ready()
    [condition] = await service.list_wait_conditions(
        agent_id="default",
        conversation_id="chat-1",
        include_terminal=True,
    )
    assert condition.status is WaitConditionStatus.CANCELLED


@pytest.mark.asyncio
async def test_dispatch_ready_persists_callback_submission_once(
    tmp_path: Path,
) -> None:
    attempt = _attempt()
    service = ModelResourceWaitService(
        tmp_path / "resource-waits.sqlite3",
        agent_id="default",
    )
    wait = await service.defer(
        attempt,
        _result(attempt, ModelFailureClass.QUOTA_EXHAUSTED),
    )
    assert wait is not None
    await service.release(wait.wait_id)
    submission_id = uuid4()
    calls = 0

    async def dispatch(_wait):
        nonlocal calls
        calls += 1
        return submission_id

    dispatched = await service.dispatch_ready(wait.wait_id, dispatch)
    replay = await service.dispatch_ready(wait.wait_id, dispatch)

    assert dispatched.status is ResourceWaitStatus.DISPATCHED
    assert dispatched.submission_id == submission_id
    assert replay == dispatched
    assert calls == 1


@pytest.mark.asyncio
async def test_transport_retry_exhaustion_creates_timer_wait(
    tmp_path: Path,
) -> None:
    attempt = _attempt()
    service = ModelResourceWaitService(
        tmp_path / "resource-waits.sqlite3",
        agent_id="default",
        transport_delay_seconds=17,
    )
    result = ModelCallResult(
        attempt_id=attempt.attempt_id,
        invocation_id=attempt.invocation_id,
        conversation_id=attempt.conversation_id,
        status=ModelCallStatus.FAILED,
        failure_class=ModelFailureClass.TRANSPORT_UNAVAILABLE,
        recovery_disposition=ModelRecoveryDisposition.RETRY_TRANSPORT,
    )

    wait = await service.defer(attempt, result)

    assert wait is not None
    assert wait.failure_class is ModelFailureClass.TRANSPORT_UNAVAILABLE
    assert wait.trigger is ResourceWaitTrigger.TIMER
    assert wait.not_before is not None
    assert (wait.not_before - wait.created_at).total_seconds() == 17
    assert not await service.list_ready()


@pytest.mark.asyncio
async def test_transport_recovery_budget_is_bounded_and_idempotent(
    tmp_path: Path,
) -> None:
    service = ModelResourceWaitService(
        tmp_path / "resource-waits.sqlite3",
        agent_id="default",
        max_automatic_recovery_cycles=2,
    )
    first = _attempt()
    attempts = (
        first,
        first.model_copy(
            update={
                "attempt_id": uuid4(),
                "route_decision_id": uuid4(),
                "invocation_id": uuid4(),
            },
        ),
        first.model_copy(
            update={
                "attempt_id": uuid4(),
                "route_decision_id": uuid4(),
                "invocation_id": uuid4(),
            },
        ),
    )

    async def defer(
        attempt: ModelCallAttempt,
        failure_class: ModelFailureClass,
    ):
        return await service.defer(
            attempt,
            ModelCallResult(
                attempt_id=attempt.attempt_id,
                invocation_id=attempt.invocation_id,
                conversation_id=attempt.conversation_id,
                status=ModelCallStatus.FAILED,
                failure_class=failure_class,
                recovery_disposition=(
                    ModelRecoveryDisposition.WAIT_RESOURCE
                    if failure_class is ModelFailureClass.RATE_LIMITED
                    else ModelRecoveryDisposition.RETRY_TRANSPORT
                ),
            ),
        )

    first_wait = await defer(
        attempts[0],
        ModelFailureClass.TRANSPORT_UNAVAILABLE,
    )
    second_wait = await defer(
        attempts[1],
        ModelFailureClass.RATE_LIMITED,
    )
    exhausted = await defer(
        attempts[2],
        ModelFailureClass.TRANSPORT_UNAVAILABLE,
    )

    assert first_wait is not None
    assert second_wait is not None
    assert exhausted is not None
    assert exhausted.status is ResourceWaitStatus.RECOVERY_EXHAUSTED
    assert await service.defer(
        attempts[0],
        ModelCallResult(
            attempt_id=attempts[0].attempt_id,
            invocation_id=attempts[0].invocation_id,
            conversation_id=attempts[0].conversation_id,
            status=ModelCallStatus.FAILED,
            failure_class=ModelFailureClass.TRANSPORT_UNAVAILABLE,
            recovery_disposition=ModelRecoveryDisposition.RETRY_TRANSPORT,
            completed_at=first_wait.created_at,
        ),
    ) == first_wait
    terminal = await service.list_wait_conditions(
        agent_id="default",
        conversation_id="chat-1",
        include_terminal=True,
    )
    exhausted_condition = next(
        condition
        for condition in terminal
        if condition.source_id == attempts[2].attempt_id
    )
    assert exhausted_condition.status is WaitConditionStatus.EXPIRED


@pytest.mark.asyncio
async def test_partial_model_step_outbox_is_bounded_and_dispatches_once(
    tmp_path: Path,
) -> None:
    service = ModelResourceWaitService(
        tmp_path / "resource-waits.sqlite3",
        agent_id="default",
        max_model_step_recovery_cycles=1,
    )
    first = _attempt()

    def result(attempt: ModelCallAttempt) -> ModelCallResult:
        return ModelCallResult(
            attempt_id=attempt.attempt_id,
            invocation_id=attempt.invocation_id,
            conversation_id=attempt.conversation_id,
            status=ModelCallStatus.FAILED,
            emitted_content=True,
            output_boundary=ModelOutputBoundary.PARTIAL_STREAM,
            failure_class=ModelFailureClass.STREAM_INTERRUPTED,
            recovery_disposition=(
                ModelRecoveryDisposition.CONTINUE_MODEL_STEP
            ),
        )

    ready = await service.defer_model_step(first, result(first))
    assert ready is not None
    assert ready.status is ModelStepContinuationStatus.READY
    submission_id = uuid4()
    calls = 0

    async def dispatch(_continuation):
        nonlocal calls
        calls += 1
        return submission_id

    dispatched = await service.dispatch_model_step(
        ready.continuation_id,
        dispatch,
    )
    replayed = await service.dispatch_model_step(
        ready.continuation_id,
        dispatch,
    )
    assert dispatched.status is ModelStepContinuationStatus.DISPATCHED
    assert dispatched.submission_id == submission_id
    assert replayed == dispatched
    assert calls == 1

    second = first.model_copy(
        update={
            "attempt_id": uuid4(),
            "route_decision_id": uuid4(),
            "invocation_id": uuid4(),
        },
    )
    exhausted = await service.defer_model_step(second, result(second))
    assert exhausted is not None
    assert exhausted.status is (
        ModelStepContinuationStatus.RECOVERY_EXHAUSTED
    )
    assert not await service.list_ready_model_steps()


@pytest.mark.asyncio
async def test_model_step_reconciliation_survives_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "resource-waits.sqlite3"
    service = ModelResourceWaitService(database, agent_id="default")
    attempt = _attempt()
    result = ModelCallResult(
        attempt_id=attempt.attempt_id,
        invocation_id=attempt.invocation_id,
        conversation_id=attempt.conversation_id,
        status=ModelCallStatus.FAILED,
        emitted_content=True,
        output_boundary=ModelOutputBoundary.PARTIAL_STREAM,
        failure_class=ModelFailureClass.STREAM_INTERRUPTED,
        recovery_disposition=ModelRecoveryDisposition.CONTINUE_MODEL_STEP,
    )
    continuation = await service.defer_model_step(attempt, result)
    assert continuation is not None
    assessment = ModelStepReconciliation(
        reason=ModelStepReconciliationReason.DURABLE_CONTEXT_REQUIRED,
        action_count=1,
        pending_result_count=0,
        uncertain_side_effect_count=0,
        terminal_result_count=1,
    )

    blocked = await service.require_action_reconciliation(
        continuation.continuation_id,
        assessment,
    )
    restarted = ModelResourceWaitService(database, agent_id="default")
    restored = await restarted.get_model_step(
        continuation.continuation_id,
    )
    replayed = await restarted.require_action_reconciliation(
        continuation.continuation_id,
        assessment.model_copy(
            update={
                "assessed_at": assessment.assessed_at
                + timedelta(seconds=1),
            },
        ),
    )

    assert blocked.reconciliation == assessment
    assert restored == blocked
    assert replayed == blocked


@pytest.mark.asyncio
async def test_model_step_reconciliation_migrates_existing_database(
    tmp_path: Path,
) -> None:
    database = tmp_path / "resource-waits.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE model_step_continuations (
                continuation_id TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL UNIQUE,
                invocation_id TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                output_boundary TEXT NOT NULL,
                status TEXT NOT NULL,
                submission_id TEXT,
                revision INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
        )

    service = ModelResourceWaitService(database, agent_id="default")
    await service.start()

    with sqlite3.connect(database) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(model_step_continuations)",
            ).fetchall()
        }
    assert "reconciliation_json" in columns
    assert "context_checkpoint_json" in columns
    assert "retry_authorization_json" in columns


@pytest.mark.asyncio
async def test_model_step_context_unblocks_certain_actions_and_persists(
    tmp_path: Path,
) -> None:
    database = tmp_path / "resource-waits.sqlite3"
    service = ModelResourceWaitService(database, agent_id="default")
    attempt = _attempt()
    continuation = await service.defer_model_step(
        attempt,
        ModelCallResult(
            attempt_id=attempt.attempt_id,
            invocation_id=attempt.invocation_id,
            conversation_id=attempt.conversation_id,
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
    assessment = ModelStepReconciliation(
        reason=ModelStepReconciliationReason.DURABLE_CONTEXT_REQUIRED,
        action_count=1,
        pending_result_count=0,
        uncertain_side_effect_count=0,
        terminal_result_count=1,
    )
    await service.require_action_reconciliation(
        continuation.continuation_id,
        assessment,
    )
    checkpoint = ModelStepContextCheckpoint(
        checkpoint_id=uuid4(),
        continuation_id=continuation.continuation_id,
        invocation_id=continuation.invocation_id,
        conversation_id=continuation.conversation_id,
        source_submission_id=uuid4(),
        action_evidence_digest=f"sha256:{'d' * 64}",
        action_count=1,
    )

    ready = await service.attach_model_step_context(
        continuation.continuation_id,
        checkpoint,
    )
    restarted = ModelResourceWaitService(database, agent_id="default")
    restored = await restarted.get_model_step(
        continuation.continuation_id,
    )

    assert ready.status is ModelStepContinuationStatus.READY
    assert ready.reconciliation is None
    assert ready.context_checkpoint == checkpoint
    assert restored == ready


@pytest.mark.asyncio
async def test_model_step_context_cannot_resolve_uncertain_action(
    tmp_path: Path,
) -> None:
    service = ModelResourceWaitService(
        tmp_path / "resource-waits.sqlite3",
        agent_id="default",
    )
    attempt = _attempt()
    continuation = await service.defer_model_step(
        attempt,
        ModelCallResult(
            attempt_id=attempt.attempt_id,
            invocation_id=attempt.invocation_id,
            conversation_id=attempt.conversation_id,
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
    await service.require_action_reconciliation(
        continuation.continuation_id,
        ModelStepReconciliation(
            reason=ModelStepReconciliationReason.UNCERTAIN_SIDE_EFFECT,
            action_count=1,
            pending_result_count=0,
            uncertain_side_effect_count=1,
            terminal_result_count=1,
        ),
    )
    checkpoint = ModelStepContextCheckpoint(
        checkpoint_id=uuid4(),
        continuation_id=continuation.continuation_id,
        invocation_id=continuation.invocation_id,
        conversation_id=continuation.conversation_id,
        source_submission_id=uuid4(),
        action_evidence_digest=f"sha256:{'e' * 64}",
        action_count=1,
    )

    with pytest.raises(
        ModelResourceWaitConflictError,
        match="cannot resolve Action uncertainty",
    ):
        await service.attach_model_step_context(
            continuation.continuation_id,
            checkpoint,
        )


@pytest.mark.asyncio
async def test_uncertain_action_retry_authorization_survives_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "resource-waits.sqlite3"
    service = ModelResourceWaitService(database, agent_id="default")
    attempt = _attempt()
    continuation = await service.defer_model_step(
        attempt,
        ModelCallResult(
            attempt_id=attempt.attempt_id,
            invocation_id=attempt.invocation_id,
            conversation_id=attempt.conversation_id,
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
    await service.require_action_reconciliation(
        continuation.continuation_id,
        ModelStepReconciliation(
            reason=ModelStepReconciliationReason.UNCERTAIN_SIDE_EFFECT,
            action_count=1,
            pending_result_count=0,
            uncertain_side_effect_count=1,
            terminal_result_count=1,
        ),
    )
    authorization = ModelStepRetryAuthorization(
        interaction_id=uuid4(),
        response_revision=2,
        continuation_id=continuation.continuation_id,
        invocation_id=continuation.invocation_id,
        conversation_id=continuation.conversation_id,
        action_evidence_digest=f"sha256:{'f' * 64}",
        action_count=1,
    )

    ready = await service.authorize_uncertain_action_retry(
        continuation.continuation_id,
        authorization,
    )
    replayed = await service.authorize_uncertain_action_retry(
        continuation.continuation_id,
        authorization,
    )
    restarted = ModelResourceWaitService(database, agent_id="default")
    restored = await restarted.get_model_step(
        continuation.continuation_id,
    )
    [history_record] = await ModelRecoveryHistory(
        restarted,
    ).scan_model_steps_for_conversation(
        continuation.conversation_id,
    )

    assert ready.status is ModelStepContinuationStatus.READY
    assert ready.reconciliation is None
    assert ready.retry_authorization == authorization
    assert replayed == ready
    assert restored == ready
    assert history_record == ready
