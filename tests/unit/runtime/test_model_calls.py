# -*- coding: utf-8 -*-
"""Tests for durable model routing and provider attempt evidence."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Literal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from agentscope.message import TextBlock
from agentscope.model._model_response import ChatResponse

from qwenpaw.kernel import (
    ContextManifest,
    InvocationScope,
    ModelCallStatus,
    ModelRouteReason,
)
from qwenpaw.runtime.model_calls import (
    ModelCallSession,
    call_with_model_session,
    lite_model_call_store,
)
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


@pytest.mark.asyncio
async def test_session_classifies_retry_and_fallback_attempts(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path)
    store = lite_model_call_store(tmp_path)
    session = ModelCallSession(scope, _manifest(scope), store)

    primary = await session.begin(provider_id="provider-a", model_id="m1")
    await session.complete(
        primary,
        status=ModelCallStatus.FAILED,
        error_kind="rate_limit",
        retryable=True,
    )
    retry = await session.begin(provider_id="provider-a", model_id="m1")
    await session.complete(
        retry,
        status=ModelCallStatus.FAILED,
        error_kind="timeout",
        retryable=True,
    )
    fallback = await session.begin(provider_id="provider-b", model_id="m2")
    await session.complete(
        fallback,
        status=ModelCallStatus.SUCCEEDED,
        emitted_content=True,
        input_tokens=11,
        output_tokens=7,
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


@pytest.mark.asyncio
async def test_overflow_retry_has_explicit_first_route_reason(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path)
    store = lite_model_call_store(tmp_path)
    session = ModelCallSession(
        scope,
        _manifest(scope, model_call_index=2, attempt_kind="overflow_retry"),
        store,
    )

    attempt = await session.begin(provider_id="provider-a", model_id="m1")
    await session.complete(
        attempt,
        status=ModelCallStatus.SUCCEEDED,
    )
    [record] = await store.list_for_conversation("chat-1")

    assert record.route.reason is ModelRouteReason.OVERFLOW_RETRY
    assert (
        record.route.context_manifest_id == record.attempt.context_manifest_id
    )


@pytest.mark.asyncio
async def test_token_wrapper_records_actual_provider_attempt(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path)
    store = lite_model_call_store(tmp_path)
    session = ModelCallSession(scope, _manifest(scope), store)
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
    wrapper = TokenRecordingModelWrapper("provider-a", provider)

    response = await call_with_model_session(
        session,
        lambda: wrapper(messages=[]),
    )
    [record] = await store.list_for_conversation("chat-1")

    assert response.content[0].text == "ok"
    assert record.attempt.provider_id == "provider-a"
    assert record.attempt.model_id == "model-a"
    assert record.result is not None
    assert record.result.status is ModelCallStatus.SUCCEEDED
    assert record.result.emitted_content is True


@pytest.mark.asyncio
async def test_stream_can_be_consumed_and_closed_in_different_contexts(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path)
    store = lite_model_call_store(tmp_path)
    session = ModelCallSession(scope, _manifest(scope), store)
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


@pytest.mark.asyncio
async def test_closing_after_terminal_chunk_is_success(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path)
    store = lite_model_call_store(tmp_path)
    session = ModelCallSession(scope, _manifest(scope), store)
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
