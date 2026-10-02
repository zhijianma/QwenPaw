# -*- coding: utf-8 -*-
"""Tests for durable model-resource waits and restart recovery."""

from pathlib import Path
from uuid import uuid4

import pytest

from qwenpaw.kernel import (
    ModelCallAttempt,
    ModelCallResult,
    ModelCallStatus,
    ModelFailureClass,
    ModelRecoveryDisposition,
    ResourceWaitStatus,
    ResourceWaitTrigger,
    WaitConditionKind,
    WaitConditionStatus,
)
from qwenpaw.recovery import ModelResourceWaitService


def _attempt() -> ModelCallAttempt:
    attempt_id = uuid4()
    return ModelCallAttempt(
        attempt_id=attempt_id,
        route_decision_id=uuid4(),
        invocation_id=uuid4(),
        correlation_id=uuid4(),
        conversation_id="chat-1",
        registry_generation=1,
        context_manifest_id=uuid4(),
        model_call_index=1,
        attempt_index=1,
        provider_id="provider-a",
        model_id="model-a",
    )


def _result(
    attempt: ModelCallAttempt,
    failure_class: ModelFailureClass,
) -> ModelCallResult:
    return ModelCallResult(
        attempt_id=attempt.attempt_id,
        invocation_id=attempt.invocation_id,
        conversation_id=attempt.conversation_id,
        status=ModelCallStatus.FAILED,
        error_kind=failure_class.value,
        failure_class=failure_class,
        recovery_disposition=ModelRecoveryDisposition.WAIT_RESOURCE,
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
    assert 0 < await service.seconds_until_next_timer() <= 5
    assert not await service.list_ready()


@pytest.mark.asyncio
async def test_non_resource_result_does_not_create_wait(
    tmp_path: Path,
) -> None:
    attempt = _attempt()
    service = ModelResourceWaitService(
        tmp_path / "resource-waits.sqlite3",
        agent_id="default",
    )
    result = ModelCallResult(
        attempt_id=attempt.attempt_id,
        invocation_id=attempt.invocation_id,
        conversation_id=attempt.conversation_id,
        status=ModelCallStatus.FAILED,
        failure_class=ModelFailureClass.TRANSPORT_UNAVAILABLE,
        recovery_disposition=ModelRecoveryDisposition.RETRY_TRANSPORT,
    )

    assert await service.defer(attempt, result) is None
    assert not await service.list_ready()
