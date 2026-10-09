# -*- coding: utf-8 -*-
"""Tests for durable model routing and provider attempt evidence."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Literal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from agentscope.message import TextBlock
from agentscope.model._model_response import ChatResponse
from agentscope.model._model_usage import ChatUsage

from qwenpaw.kernel import (
    ContextManifest,
    InvocationScope,
    ModelCallAttempt,
    ModelCallResult,
    ModelCallStore,
    ModelCallStatus,
    ModelFailureClass,
    ModelOutputBoundary,
    ModelRecoveryDisposition,
    ModelRouteReason,
    ModelTransportProtocol,
    ModelTransportRecoveryMode,
    ModelTransportValidationReason,
)
from qwenpaw.runtime.model_calls import (
    ModelCallPersistenceError,
    ModelCallSession,
    ModelStepRecoveryError,
    call_with_model_session,
    lite_model_call_store,
)
from qwenpaw.recovery import ModelResourceWaitService
from qwenpaw.providers.retry_chat_model import RetryChatModel, RetryConfig
from qwenpaw.token_usage.model_wrapper import TokenRecordingModelWrapper


def _scope(tmp_path: Path) -> InvocationScope:
    return InvocationScope(
        invocation_id=uuid4(),
        agent_id="default",
        conversation_id="chat-1",
        session_id="chat-1",
        root_agent_id="default",
        root_session_id="chat-1",
        workspace_dir=str(tmp_path),
        registry_epoch_id=uuid4(),
        registry_generation=9,
    )


def _manifest(
    scope: InvocationScope,
    *,
    model_call_index: int = 1,
    attempt_kind: Literal["primary", "overflow_retry"] = "primary",
) -> ContextManifest:
    return ContextManifest(
        invocation_id=scope.invocation_id,
        correlation_id=scope.invocation_id,
        conversation_id=scope.conversation_id,
        registry_epoch_id=scope.registry_epoch_id,
        registry_generation=scope.registry_generation,
        model_call_index=model_call_index,
        attempt_kind=attempt_kind,
        policy_id="qwenpaw.system.context-policy",
        policy_version="1",
        total_size_bytes=0,
        total_estimated_tokens=0,
        disclosed_tool_count=0,
        manifest_hash=f"sha256:{'a' * 64}",
    )


def _session(
    scope: InvocationScope,
    manifest: ContextManifest,
    store: ModelCallStore,
    *,
    resource_waits=None,
) -> ModelCallSession:
    return ModelCallSession(
        scope,
        manifest,
        store,
        requested_provider_id="provider-a",
        requested_model_id="m1",
        resource_waits=resource_waits,
    )


def test_session_rejects_manifest_from_another_registry_epoch(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path)
    manifest = _manifest(scope).model_copy(
        update={"registry_epoch_id": uuid4()},
    )

    with pytest.raises(ValueError, match="registry epoch mismatch"):
        _session(scope, manifest, lite_model_call_store(tmp_path))


async def _begin_attempt(
    session: ModelCallSession,
    *,
    provider_id: str = "provider-a",
    model_id: str = "m1",
) -> ModelCallAttempt:
    return await session.begin(
        provider_id=provider_id,
        model_id=model_id,
        adapter_id="agentscope.model.fake.Adapter",
        adapter_version="2.0.7.post1",
        formatter_id="qwenpaw.fake.Formatter",
        formatter_version="2.2.2b1",
    )


@pytest.mark.asyncio
async def test_session_classifies_retry_and_fallback_attempts(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path)
    store = lite_model_call_store(tmp_path)
    session = _session(scope, _manifest(scope), store)

    primary = await _begin_attempt(session)
    await session.complete(
        primary,
        status=ModelCallStatus.FAILED,
        error_kind="rate_limit",
        retryable=True,
    )
    retry = await _begin_attempt(session)
    await session.complete(
        retry,
        status=ModelCallStatus.FAILED,
        error_kind="timeout",
        retryable=True,
    )
    fallback = await _begin_attempt(
        session,
        provider_id="provider-b",
        model_id="m2",
    )
    await session.complete(
        fallback,
        status=ModelCallStatus.SUCCEEDED,
        emitted_content=True,
        input_tokens=11,
        output_tokens=7,
        usage_measurement="provider_reported",
        cache_read_tokens=8,
        cache_write_tokens=1,
        cache_eligible_input_tokens=11,
        cache_observed=True,
        cost_micros=23,
    )

    records = sorted(
        await store.list_for_conversation("chat-1"),
        key=lambda item: item.attempt.attempt_index,
    )

    assert [record.route.reason for record in records] == [
        ModelRouteReason.PRIMARY,
        ModelRouteReason.SAME_MODEL_RETRY,
        ModelRouteReason.FALLBACK,
    ]
    assert records[1].route.previous_attempt_id == primary.attempt_id
    assert records[2].route.previous_attempt_id == retry.attempt_id
    assert records[2].result is not None
    assert records[2].result.input_tokens == 11
    assert records[2].result.output_tokens == 7
    assert records[2].result.usage_measurement == "provider_reported"
    assert records[2].result.cache_read_tokens == 8
    assert records[2].result.cache_write_tokens == 1
    assert records[2].result.cache_eligible_input_tokens == 11
    assert records[2].result.cache_observed is True
    assert records[2].result.cost_micros == 23
    assert records[2].result.cost_unknown is False
    assert records[2].route.requested_provider_id == "provider-a"
    assert records[2].route.requested_model_id == "m1"
    assert records[2].route.registry_epoch_id == scope.registry_epoch_id
    assert records[2].attempt.registry_epoch_id == scope.registry_epoch_id
    assert records[2].attempt.adapter_version == "2.0.7.post1"
    assert records[2].attempt.formatter_version == "2.2.2b1"
    assert records[2].attempt.agent_id == "default"


@pytest.mark.asyncio
async def test_store_scans_all_chat_owners_for_projection_rebuild(
    tmp_path: Path,
) -> None:
    store = lite_model_call_store(tmp_path)
    first_scope = _scope(tmp_path)
    second_scope = _scope(tmp_path).model_copy(
        update={
            "invocation_id": uuid4(),
            "chat_id": "chat-2",
            "session_id": "chat-2",
        },
    )
    for scope in (first_scope, second_scope):
        session = _session(scope, _manifest(scope), store)
        attempt = await _begin_attempt(session)
        await session.complete(
            attempt,
            status=ModelCallStatus.SUCCEEDED,
            input_tokens=10,
            output_tokens=2,
            usage_measurement="provider_reported",
        )

    records = await store.scan_all()

    assert {record.attempt.conversation_id for record in records} == {
        "chat-1",
        "chat-2",
    }


@pytest.mark.asyncio
async def test_overflow_retry_has_explicit_first_route_reason(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path)
    store = lite_model_call_store(tmp_path)
    session = _session(
        scope,
        _manifest(scope, model_call_index=2, attempt_kind="overflow_retry"),
        store,
    )

    attempt = await _begin_attempt(session)
    await session.complete(
        attempt,
        status=ModelCallStatus.SUCCEEDED,
    )
    [record] = await store.list_for_conversation("chat-1")

    assert record.route.reason is ModelRouteReason.OVERFLOW_RETRY
    assert (
        record.route.context_manifest_id == record.attempt.context_manifest_id
    )
    assert record.result is not None
    assert record.result.cost_micros is None
    assert record.result.cost_unknown is True


@pytest.mark.asyncio
async def test_token_wrapper_records_actual_provider_attempt(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path)
    store = lite_model_call_store(tmp_path)
    session = _session(scope, _manifest(scope), store)
    provider = AsyncMock()
    provider.model = "model-a"
    provider.credential = None
    provider.parameters = None
    provider.stream = False
    provider.context_size = 32_768
    provider.formatter = object()
    provider.return_value = ChatResponse(
        content=[TextBlock(text="ok")],
        is_last=True,
    )
    wrapper = TokenRecordingModelWrapper(
        "provider-a",
        provider,
        compact_threshold=0.8,
    )

    response = await call_with_model_session(
        session,
        lambda: wrapper(messages=[]),
    )
    [record] = await store.list_for_conversation("chat-1")

    assert response.content[0].text == "ok"
    assert record.attempt.provider_id == "provider-a"
    assert record.attempt.model_id == "model-a"
    assert record.attempt.context_window_tokens == 32_768
    assert record.attempt.compaction_threshold == 0.8
    assert record.attempt.transport_contract.protocol is (
        ModelTransportProtocol.HTTP
    )
    assert record.attempt.adapter_id is not None
    assert record.attempt.adapter_version is None
    assert record.attempt.formatter_id == "builtins.object"
    assert record.attempt.formatter_version is None
    assert record.result is not None
    assert record.result.status is ModelCallStatus.SUCCEEDED
    assert record.result.emitted_content is True
    assert record.result.output_boundary is (
        ModelOutputBoundary.COMPLETE_RESPONSE
    )
    assert record.result.failure_class is None
    assert record.result.recovery_disposition is None


@pytest.mark.asyncio
async def test_token_wrapper_persists_complete_provider_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _scope(tmp_path)
    store = lite_model_call_store(tmp_path)
    session = _session(scope, _manifest(scope), store)
    provider = AsyncMock()
    provider.model = "model-a"
    provider.credential = None
    provider.parameters = None
    provider.stream = False
    provider.context_size = 32_768
    provider.formatter = object()
    provider.return_value = ChatResponse(
        content=[TextBlock(text="ok")],
        is_last=True,
        usage=ChatUsage(
            input_tokens=100,
            output_tokens=17,
            time=0.25,
            cache_input_tokens=70,
            cache_creation_input_tokens=5,
        ),
    )
    monkeypatch.setattr(
        "qwenpaw.token_usage.model_wrapper._cache_usage_metrics",
        lambda *_args: (True, 100),
    )
    usage_manager = MagicMock()
    usage_manager.project_model_call = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "qwenpaw.token_usage.model_wrapper.get_token_usage_manager",
        lambda: usage_manager,
    )
    wrapper = TokenRecordingModelWrapper("provider-a", provider)

    await call_with_model_session(
        session,
        lambda: wrapper(messages=[]),
    )
    [record] = await store.list_for_conversation("chat-1")

    assert record.result is not None
    assert record.result.input_tokens == 100
    assert record.result.output_tokens == 17
    assert record.result.usage_measurement == "provider_reported"
    assert record.result.cache_read_tokens == 70
    assert record.result.cache_write_tokens == 5
    assert record.result.cache_eligible_input_tokens == 100
    assert record.result.cache_observed is True


@pytest.mark.asyncio
async def test_token_wrapper_projects_calls_without_provider_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _scope(tmp_path)
    store = lite_model_call_store(tmp_path)
    session = _session(scope, _manifest(scope), store)
    provider = AsyncMock()
    provider.model = "model-a"
    provider.credential = None
    provider.parameters = None
    provider.stream = False
    provider.context_size = 32_768
    provider.formatter = object()
    provider.return_value = ChatResponse(
        content=[TextBlock(text="ok")],
        is_last=True,
        usage=None,
    )
    usage_manager = MagicMock()
    usage_manager.project_model_call = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "qwenpaw.token_usage.model_wrapper.get_token_usage_manager",
        lambda: usage_manager,
    )
    wrapper = TokenRecordingModelWrapper("provider-a", provider)

    await call_with_model_session(
        session,
        lambda: wrapper(messages=[]),
    )

    usage_manager.project_model_call.assert_awaited_once()
    attempt, result = usage_manager.project_model_call.await_args.args
    assert attempt.conversation_id == "chat-1"
    assert result.usage_measurement is None
    assert result.input_tokens is None
    assert result.output_tokens is None


@pytest.mark.asyncio
async def test_usage_projection_waits_for_durable_model_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _scope(tmp_path)
    store = AsyncMock(spec=ModelCallStore)
    store.complete.side_effect = OSError("disk full")
    session = _session(scope, _manifest(scope), store)
    provider = AsyncMock()
    provider.model = "model-a"
    provider.credential = None
    provider.parameters = None
    provider.stream = False
    provider.context_size = 32_768
    provider.formatter = object()
    provider.return_value = ChatResponse(
        content=[TextBlock(text="ok")],
        is_last=True,
        usage=ChatUsage(
            input_tokens=100,
            output_tokens=17,
            time=0.25,
        ),
    )
    projected: list[object] = []
    monkeypatch.setattr(
        "qwenpaw.token_usage.model_wrapper.get_token_usage_manager",
        lambda: MagicMock(enqueue=projected.append),
    )
    wrapper = TokenRecordingModelWrapper("provider-a", provider)

    with pytest.raises(
        ModelCallPersistenceError,
        match="result could not be durably recorded",
    ):
        await call_with_model_session(
            session,
            lambda: wrapper(messages=[]),
        )

    assert not projected


@pytest.mark.asyncio
async def test_token_wrapper_records_transport_recovery_contract(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path)
    store = lite_model_call_store(tmp_path)
    session = _session(scope, _manifest(scope), store)
    provider = AsyncMock()
    provider.model = "model-a"
    provider.credential = None
    provider.parameters = None
    provider.stream = False
    provider.context_size = 32_768
    provider.formatter = object()
    provider.side_effect = TimeoutError("provider timed out")
    wrapper = TokenRecordingModelWrapper("provider-a", provider)

    with pytest.raises(TimeoutError):
        await call_with_model_session(
            session,
            lambda: wrapper(messages=[]),
        )
    [record] = await store.list_for_conversation("chat-1")

    assert record.result is not None
    assert record.result.failure_class is (
        ModelFailureClass.TRANSPORT_UNAVAILABLE
    )
    assert record.result.recovery_disposition is (
        ModelRecoveryDisposition.RETRY_TRANSPORT
    )
    assert record.result.output_boundary is ModelOutputBoundary.PRE_OUTPUT


@pytest.mark.asyncio
async def test_token_wrapper_records_rate_limit_retry_hint(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path)
    store = lite_model_call_store(tmp_path)
    session = _session(scope, _manifest(scope), store)
    provider = AsyncMock()
    provider.model = "model-a"
    provider.credential = None
    provider.parameters = None
    provider.stream = False
    provider.context_size = 32_768
    provider.formatter = object()
    error = RuntimeError("rate limited")
    error.status_code = 429
    error.headers = {"Retry-After": "45"}
    provider.side_effect = error
    wrapper = TokenRecordingModelWrapper("provider-a", provider)

    with pytest.raises(RuntimeError, match="rate limited"):
        await call_with_model_session(
            session,
            lambda: wrapper(messages=[]),
        )
    [record] = await store.list_for_conversation("chat-1")

    assert record.result is not None
    assert record.result.failure_class is ModelFailureClass.RATE_LIMITED
    assert record.result.recovery_disposition is (
        ModelRecoveryDisposition.WAIT_RESOURCE
    )
    assert record.result.retry_after_seconds == 45.0


@pytest.mark.asyncio
async def test_model_session_persists_only_terminal_resource_wait(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path)
    resource_waits = ModelResourceWaitService(
        tmp_path / "resource-waits.sqlite3",
        agent_id="default",
    )
    session = _session(
        scope,
        _manifest(scope),
        lite_model_call_store(tmp_path),
        resource_waits=resource_waits,
    )
    attempt = await _begin_attempt(session)

    await session.complete(
        attempt,
        status=ModelCallStatus.FAILED,
        error_kind="quota_exhausted",
        failure_class=ModelFailureClass.QUOTA_EXHAUSTED,
        recovery_disposition=ModelRecoveryDisposition.WAIT_RESOURCE,
    )
    await session.defer_terminal_resource_wait()

    [condition] = await resource_waits.list_wait_conditions(
        agent_id="default",
        conversation_id="chat-1",
    )
    assert condition.source_id == attempt.attempt_id


@pytest.mark.asyncio
async def test_successful_model_attempt_releases_exact_quota_wait(
    tmp_path: Path,
) -> None:
    resource_waits = ModelResourceWaitService(
        tmp_path / "resource-waits.sqlite3",
        agent_id="default",
    )
    blocked_scope = _scope(tmp_path)
    blocked = _session(
        blocked_scope,
        _manifest(blocked_scope),
        lite_model_call_store(tmp_path),
        resource_waits=resource_waits,
    )
    blocked_attempt = await _begin_attempt(blocked)
    await blocked.complete(
        blocked_attempt,
        status=ModelCallStatus.FAILED,
        error_kind="quota_exhausted",
        failure_class=ModelFailureClass.QUOTA_EXHAUSTED,
        recovery_disposition=ModelRecoveryDisposition.WAIT_RESOURCE,
    )
    wait = await blocked.defer_terminal_resource_wait()
    assert wait is not None

    healthy_scope = _scope(tmp_path)
    healthy = _session(
        healthy_scope,
        _manifest(healthy_scope),
        lite_model_call_store(tmp_path),
        resource_waits=resource_waits,
    )
    healthy_attempt = await _begin_attempt(healthy)
    await healthy.complete(
        healthy_attempt,
        status=ModelCallStatus.SUCCEEDED,
    )

    [ready] = await resource_waits.list_ready()
    assert ready.wait_id == wait.wait_id


@pytest.mark.asyncio
async def test_successful_fallback_suppresses_intermediate_resource_wait(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path)
    resource_waits = ModelResourceWaitService(
        tmp_path / "resource-waits.sqlite3",
        agent_id="default",
    )
    session = _session(
        scope,
        _manifest(scope),
        lite_model_call_store(tmp_path),
        resource_waits=resource_waits,
    )
    primary = await _begin_attempt(session)
    await session.complete(
        primary,
        status=ModelCallStatus.FAILED,
        error_kind="rate_limited",
        failure_class=ModelFailureClass.RATE_LIMITED,
        recovery_disposition=ModelRecoveryDisposition.WAIT_RESOURCE,
    )
    fallback = await _begin_attempt(
        session,
        provider_id="provider-b",
        model_id="m2",
    )
    await session.complete(fallback, status=ModelCallStatus.SUCCEEDED)

    await session.defer_terminal_resource_wait()

    assert not await resource_waits.list_wait_conditions(
        agent_id="default",
        conversation_id="chat-1",
    )


@pytest.mark.asyncio
async def test_failed_logical_call_creates_resource_wait_automatically(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path)
    resource_waits = ModelResourceWaitService(
        tmp_path / "resource-waits.sqlite3",
        agent_id="default",
    )
    session = _session(
        scope,
        _manifest(scope),
        lite_model_call_store(tmp_path),
        resource_waits=resource_waits,
    )
    provider = AsyncMock()
    provider.model = "model-a"
    provider.credential = None
    provider.parameters = None
    provider.stream = False
    provider.context_size = 32_768
    provider.formatter = object()
    provider.side_effect = RuntimeError("insufficient_quota")
    wrapper = TokenRecordingModelWrapper("provider-a", provider)

    with pytest.raises(RuntimeError, match="insufficient_quota"):
        await call_with_model_session(
            session,
            lambda: wrapper(messages=[]),
        )

    [condition] = await resource_waits.list_wait_conditions(
        agent_id="default",
        conversation_id="chat-1",
    )
    assert condition.source_type == "qwenpaw.model-resource"


@pytest.mark.asyncio
async def test_short_transport_retry_success_does_not_create_wait(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path)
    store = lite_model_call_store(tmp_path)
    resource_waits = ModelResourceWaitService(
        tmp_path / "resource-waits.sqlite3",
        agent_id="default",
    )
    session = _session(
        scope,
        _manifest(scope),
        store,
        resource_waits=resource_waits,
    )
    provider = AsyncMock()
    provider.model = "model-a"
    provider.credential = None
    provider.parameters = None
    provider.stream = False
    provider.context_size = 32_768
    provider.formatter = object()
    provider.side_effect = [
        TimeoutError("temporary outage"),
        ChatResponse(
            content=[TextBlock(text="recovered")],
            is_last=True,
        ),
    ]
    recorded = TokenRecordingModelWrapper("provider-a", provider)
    model = RetryChatModel(
        recorded,
        retry_config=RetryConfig(
            enabled=True,
            max_retries=1,
            backoff_base=0.1,
            backoff_cap=0.1,
        ),
    )

    response = await call_with_model_session(
        session,
        lambda: model(messages=[]),
    )
    records = await store.list_for_conversation("chat-1")

    assert response.content[0].text == "recovered"
    assert len(records) == 2
    results = [record.result for record in records]
    assert all(result is not None for result in results)
    failed = next(
        result
        for result in results
        if result is not None and result.status is ModelCallStatus.FAILED
    )
    succeeded = next(
        result
        for result in results
        if result is not None and result.status is ModelCallStatus.SUCCEEDED
    )
    assert failed.failure_class is (ModelFailureClass.TRANSPORT_UNAVAILABLE)
    assert succeeded.failure_class is None
    assert not await resource_waits.list_wait_conditions(
        agent_id="default",
        conversation_id="chat-1",
    )


@pytest.mark.asyncio
async def test_exhausted_transport_retry_creates_durable_wait(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path)
    store = lite_model_call_store(tmp_path)
    resource_waits = ModelResourceWaitService(
        tmp_path / "resource-waits.sqlite3",
        agent_id="default",
    )
    session = _session(
        scope,
        _manifest(scope),
        store,
        resource_waits=resource_waits,
    )
    provider = AsyncMock()
    provider.model = "model-a"
    provider.credential = None
    provider.parameters = None
    provider.stream = False
    provider.context_size = 32_768
    provider.formatter = object()
    provider.side_effect = TimeoutError("persistent outage")
    recorded = TokenRecordingModelWrapper("provider-a", provider)
    model = RetryChatModel(
        recorded,
        retry_config=RetryConfig(
            enabled=True,
            max_retries=1,
            backoff_base=0.1,
            backoff_cap=0.1,
        ),
    )

    with pytest.raises(TimeoutError, match="persistent outage"):
        await call_with_model_session(
            session,
            lambda: model(messages=[]),
        )
    records = await store.list_for_conversation("chat-1")
    [condition] = await resource_waits.list_wait_conditions(
        agent_id="default",
        conversation_id="chat-1",
    )

    assert len(records) == 2
    assert condition.kind.value == "resource"
    assert condition.not_before is not None


@pytest.mark.asyncio
async def test_stream_can_be_consumed_and_closed_in_different_contexts(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path)
    store = lite_model_call_store(tmp_path)
    session = _session(scope, _manifest(scope), store)
    provider = AsyncMock()
    provider.model = "model-a"
    provider.credential = None
    provider.parameters = None
    provider.stream = True
    provider.context_size = 32_768
    provider.formatter = object()

    async def provider_stream() -> AsyncGenerator[ChatResponse, None]:
        yield ChatResponse(
            content=[TextBlock(text="first")],
            is_last=False,
        )
        yield ChatResponse(
            content=[TextBlock(text="second")],
            is_last=True,
        )

    provider.return_value = provider_stream()
    wrapper = TokenRecordingModelWrapper("provider-a", provider)
    stream = await call_with_model_session(
        session,
        lambda: wrapper(messages=[]),
    )

    first = await asyncio.create_task(anext(stream))
    await asyncio.create_task(stream.aclose())
    [record] = await store.list_for_conversation("chat-1")

    assert first.content[0].text == "first"
    assert record.result is not None
    assert record.result.status is ModelCallStatus.CANCELLED
    assert record.result.emitted_content is True
    assert record.result.failure_class is ModelFailureClass.USER_INTERRUPTED
    assert record.result.recovery_disposition is (
        ModelRecoveryDisposition.STOP_INTERRUPTED
    )
    assert record.result.output_boundary is (
        ModelOutputBoundary.PARTIAL_STREAM
    )
    assert record.result.transport_recovery_mode is None


@pytest.mark.asyncio
async def test_partial_stream_records_continue_model_step(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path)
    store = lite_model_call_store(tmp_path)
    recovery = ModelResourceWaitService(
        tmp_path / "resource-waits.sqlite3",
        agent_id="default",
    )
    session = _session(
        scope,
        _manifest(scope),
        store,
        resource_waits=recovery,
    )
    provider = AsyncMock()
    provider.model = "model-a"
    provider.credential = None
    provider.parameters = None
    provider.stream = True
    provider.context_size = 32_768
    provider.formatter = object()

    async def interrupted_stream() -> AsyncGenerator[ChatResponse, None]:
        yield ChatResponse(
            content=[TextBlock(text="partial")],
            is_last=False,
        )
        raise ConnectionError("stream disconnected")

    provider.return_value = interrupted_stream()
    wrapper = TokenRecordingModelWrapper("provider-a", provider)
    stream = await call_with_model_session(
        session,
        lambda: wrapper(messages=[]),
    )

    assert (await anext(stream)).content[0].text == "partial"
    with pytest.raises(ModelStepRecoveryError) as captured:
        await anext(stream)
    [record] = await store.list_for_conversation("chat-1")

    assert record.result is not None
    assert record.result.failure_class is ModelFailureClass.STREAM_INTERRUPTED
    assert record.result.recovery_disposition is (
        ModelRecoveryDisposition.CONTINUE_MODEL_STEP
    )
    assert record.result.output_boundary is (
        ModelOutputBoundary.PARTIAL_STREAM
    )
    assert record.result.transport_recovery_mode is (
        ModelTransportRecoveryMode.DURABLE_CONTEXT_REBUILD
    )
    assert record.result.transport_validation_reason is (
        ModelTransportValidationReason.CAPABILITY_UNAVAILABLE
    )
    [continuation] = await recovery.list_ready_model_steps()
    assert captured.value.continuation_id == continuation.continuation_id
    assert isinstance(captured.value.__cause__, ConnectionError)
    assert continuation.attempt_id == record.attempt.attempt_id
    assert continuation.correlation_id == scope.invocation_id
    assert not hasattr(continuation, "content")


@pytest.mark.asyncio
async def test_closing_after_terminal_chunk_is_success(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path)
    store = lite_model_call_store(tmp_path)
    session = _session(scope, _manifest(scope), store)
    provider = AsyncMock()
    provider.model = "model-a"
    provider.credential = None
    provider.parameters = None
    provider.stream = True
    provider.context_size = 32_768
    provider.formatter = object()

    async def provider_stream() -> AsyncGenerator[ChatResponse, None]:
        yield ChatResponse(
            content=[TextBlock(text="complete")],
            is_last=True,
        )

    provider.return_value = provider_stream()
    wrapper = TokenRecordingModelWrapper("provider-a", provider)
    stream = await call_with_model_session(
        session,
        lambda: wrapper(messages=[]),
    )

    terminal = await anext(stream)
    await stream.aclose()
    [record] = await store.list_for_conversation("chat-1")

    assert terminal.is_last is True
    assert record.result is not None
    assert record.result.status is ModelCallStatus.SUCCEEDED
    assert record.result.error_kind == ""
    assert record.result.output_boundary is (
        ModelOutputBoundary.TERMINAL_STREAM
    )


@pytest.mark.asyncio
async def test_incomplete_stream_end_is_failure_not_success(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path)
    store = lite_model_call_store(tmp_path)
    session = _session(scope, _manifest(scope), store)
    provider = AsyncMock()
    provider.model = "model-a"
    provider.credential = None
    provider.parameters = None
    provider.stream = True
    provider.context_size = 32_768
    provider.formatter = object()

    async def provider_stream() -> AsyncGenerator[ChatResponse, None]:
        yield ChatResponse(
            content=[TextBlock(text="partial by eof")],
            is_last=False,
        )

    provider.return_value = provider_stream()
    wrapper = TokenRecordingModelWrapper("provider-a", provider)
    stream = await call_with_model_session(
        session,
        lambda: wrapper(messages=[]),
    )

    assert (await anext(stream)).content[0].text == "partial by eof"
    with pytest.raises(
        ConnectionError,
        match="without a terminal chunk",
    ):
        await anext(stream)
    [record] = await store.list_for_conversation("chat-1")

    assert record.result is not None
    assert record.result.status is ModelCallStatus.FAILED
    assert record.result.failure_class is ModelFailureClass.STREAM_INTERRUPTED
    assert record.result.recovery_disposition is (
        ModelRecoveryDisposition.CONTINUE_MODEL_STEP
    )
    assert record.result.output_boundary is (
        ModelOutputBoundary.INCOMPLETE_STREAM_END
    )
    assert record.result.transport_recovery_mode is (
        ModelTransportRecoveryMode.DURABLE_CONTEXT_REBUILD
    )


def test_historical_result_defaults_to_unknown_cost() -> None:
    result = ModelCallResult.model_validate(
        {
            "attempt_id": str(uuid4()),
            "invocation_id": str(uuid4()),
            "conversation_id": "chat-1",
            "status": "succeeded",
        },
    )

    assert result.cost_micros is None
    assert result.cost_unknown is True
    assert result.failure_class is None
    assert result.recovery_disposition is None
    assert result.output_boundary is None
    assert result.usage_measurement is None
    assert result.cache_observed is False


def test_legacy_result_infers_provider_usage() -> None:
    result = ModelCallResult.model_validate(
        {
            "attempt_id": str(uuid4()),
            "invocation_id": str(uuid4()),
            "conversation_id": "chat-1",
            "status": "succeeded",
            "input_tokens": 21,
            "output_tokens": 5,
        },
    )

    assert result.input_tokens == 21
    assert result.output_tokens == 5
    assert result.usage_measurement == "provider_reported"


def test_result_rejects_partial_usage_counters() -> None:
    with pytest.raises(
        ValueError,
        match="requires both input and output tokens",
    ):
        ModelCallResult(
            attempt_id=uuid4(),
            invocation_id=uuid4(),
            conversation_id="chat-1",
            status=ModelCallStatus.SUCCEEDED,
            input_tokens=21,
        )


def test_result_rejects_unobserved_cache_counters() -> None:
    with pytest.raises(
        ValueError,
        match="unobserved model cache usage cannot contain counters",
    ):
        ModelCallResult(
            attempt_id=uuid4(),
            invocation_id=uuid4(),
            conversation_id="chat-1",
            status=ModelCallStatus.SUCCEEDED,
            cache_read_tokens=1,
        )


def test_result_requires_complete_recovery_contract() -> None:
    with pytest.raises(
        ValueError,
        match="failure class and recovery disposition must agree",
    ):
        ModelCallResult(
            attempt_id=uuid4(),
            invocation_id=uuid4(),
            conversation_id="chat-1",
            status=ModelCallStatus.FAILED,
            failure_class=ModelFailureClass.TRANSPORT_UNAVAILABLE,
        )


@pytest.mark.parametrize(
    ("failure_class", "disposition"),
    [
        (
            ModelFailureClass.TRANSPORT_UNAVAILABLE,
            ModelRecoveryDisposition.RETRY_TRANSPORT,
        ),
        (
            ModelFailureClass.STREAM_INTERRUPTED,
            ModelRecoveryDisposition.CONTINUE_MODEL_STEP,
        ),
        (
            ModelFailureClass.RATE_LIMITED,
            ModelRecoveryDisposition.WAIT_RESOURCE,
        ),
        (
            ModelFailureClass.QUOTA_EXHAUSTED,
            ModelRecoveryDisposition.WAIT_RESOURCE,
        ),
        (
            ModelFailureClass.AUTHENTICATION_REQUIRED,
            ModelRecoveryDisposition.FAIL_TERMINAL,
        ),
        (
            ModelFailureClass.USER_INTERRUPTED,
            ModelRecoveryDisposition.STOP_INTERRUPTED,
        ),
    ],
)
def test_result_accepts_kernel_recovery_matrix(
    failure_class: ModelFailureClass,
    disposition: ModelRecoveryDisposition,
) -> None:
    result = ModelCallResult(
        attempt_id=uuid4(),
        invocation_id=uuid4(),
        conversation_id="chat-1",
        status=ModelCallStatus.FAILED,
        failure_class=failure_class,
        recovery_disposition=disposition,
    )

    assert result.failure_class is failure_class
    assert result.recovery_disposition is disposition


def test_result_rejects_recovery_pair_outside_kernel_matrix() -> None:
    with pytest.raises(
        ValueError,
        match="disposition is not allowed for failure class",
    ):
        ModelCallResult(
            attempt_id=uuid4(),
            invocation_id=uuid4(),
            conversation_id="chat-1",
            status=ModelCallStatus.FAILED,
            failure_class=ModelFailureClass.AUTHENTICATION_REQUIRED,
            recovery_disposition=(ModelRecoveryDisposition.RETRY_TRANSPORT),
        )


def test_retry_after_requires_rate_limited_resource_wait() -> None:
    with pytest.raises(
        ValueError,
        match="retry-after hint requires rate-limited resource wait",
    ):
        ModelCallResult(
            attempt_id=uuid4(),
            invocation_id=uuid4(),
            conversation_id="chat-1",
            status=ModelCallStatus.FAILED,
            failure_class=ModelFailureClass.TRANSPORT_UNAVAILABLE,
            recovery_disposition=(ModelRecoveryDisposition.RETRY_TRANSPORT),
            retry_after_seconds=10,
        )


def test_partial_output_cannot_be_marked_for_transport_replay() -> None:
    with pytest.raises(
        ValueError,
        match="emitted content cannot retry transport",
    ):
        ModelCallResult(
            attempt_id=uuid4(),
            invocation_id=uuid4(),
            conversation_id="chat-1",
            status=ModelCallStatus.FAILED,
            emitted_content=True,
            failure_class=ModelFailureClass.STREAM_INTERRUPTED,
            recovery_disposition=(ModelRecoveryDisposition.RETRY_TRANSPORT),
        )


def test_partial_stream_boundary_requires_emitted_content() -> None:
    with pytest.raises(ValueError, match="requires emitted content"):
        ModelCallResult(
            attempt_id=uuid4(),
            invocation_id=uuid4(),
            conversation_id="chat-1",
            status=ModelCallStatus.FAILED,
            output_boundary=ModelOutputBoundary.PARTIAL_STREAM,
            failure_class=ModelFailureClass.STREAM_INTERRUPTED,
            recovery_disposition=(
                ModelRecoveryDisposition.CONTINUE_MODEL_STEP
            ),
        )


@pytest.mark.parametrize(
    ("cost_micros", "cost_unknown"),
    [(None, False), (0, True)],
)
def test_result_rejects_conflicting_cost_evidence(
    cost_micros: int | None,
    cost_unknown: bool,
) -> None:
    with pytest.raises(
        ValueError,
        match="model call cost value and unknown flag conflict",
    ):
        ModelCallResult(
            attempt_id=uuid4(),
            invocation_id=uuid4(),
            conversation_id="chat-1",
            status=ModelCallStatus.SUCCEEDED,
            cost_micros=cost_micros,
            cost_unknown=cost_unknown,
        )
