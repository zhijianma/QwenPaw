# -*- coding: utf-8 -*-
"""Tests for shared retry and cross-model fallback policy."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import anthropic
import httpx
import openai
import pytest

from qwenpaw.kernel import ModelFailureClass, ModelRecoveryDisposition
from qwenpaw.providers.model_error_policy import (
    classify_model_error,
    classify_model_recovery,
    extract_retry_after_seconds,
)


class HttpError(Exception):
    """Exception carrying a provider-style status code."""

    def __init__(self, status_code: int, message: str = "error") -> None:
        super().__init__(message)
        self.status_code = status_code


class ResponseStatusError(Exception):
    """Exception exposing status only through its response."""

    def __init__(self, status_code: int, message: str = "error") -> None:
        super().__init__(message)
        self.response = type(
            "Response",
            (),
            {"status_code": status_code},
        )()


class CodeStatusError(Exception):
    """Exception exposing status only through its code attribute."""

    def __init__(self, code: int, message: str = "error") -> None:
        super().__init__(message)
        self.code = code


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 529])
def test_transient_http_errors_allow_retry_and_fallback(status: int) -> None:
    decision = classify_model_error(HttpError(status))

    assert decision.retryable is True
    assert decision.fallback_eligible is True


@pytest.mark.parametrize("status", [400, 401, 403, 422])
def test_permanent_http_errors_do_not_fallback(status: int) -> None:
    decision = classify_model_error(HttpError(status))

    assert decision.retryable is False
    assert decision.fallback_eligible is False


def test_model_not_found_allows_fallback_without_retry() -> None:
    decision = classify_model_error(HttpError(404))

    assert decision.kind == "model_not_found"
    assert decision.retryable is False
    assert decision.fallback_eligible is True


def test_context_overflow_does_not_fallback() -> None:
    decision = classify_model_error(
        HttpError(400, "maximum context length exceeded"),
    )

    assert decision.kind == "context_overflow"
    assert decision.fallback_eligible is False


def test_response_status_is_rate_limited() -> None:
    decision = classify_model_error(ResponseStatusError(429))

    assert decision.status_code == 429
    assert decision.kind == "rate_limited"
    assert decision.retryable is True
    assert decision.fallback_eligible is True


def test_code_status_is_transient() -> None:
    decision = classify_model_error(CodeStatusError(529))

    assert decision.status_code == 529
    assert decision.kind == "provider_overloaded"
    assert decision.retryable is True


def test_streaming_status_is_transient() -> None:
    decision = classify_model_error(
        Exception("Streaming response failed: [503] unavailable"),
    )

    assert decision.status_code == 503
    assert decision.kind == "provider_overloaded"
    assert decision.retryable is True


def test_remote_protocol_error_is_transient() -> None:
    decision = classify_model_error(
        httpx.RemoteProtocolError("peer closed connection"),
    )

    assert decision.kind == "transient"
    assert decision.retryable is True


def test_rate_limit_status_precedes_context_heuristic() -> None:
    decision = classify_model_error(
        ResponseStatusError(429, "Too many tokens per minute"),
    )

    assert decision.kind == "rate_limited"
    assert decision.retryable is True


def test_content_safety_does_not_fallback() -> None:
    decision = classify_model_error(
        HttpError(400, "content policy rejected this input"),
    )

    assert decision.kind == "content_safety"
    assert decision.fallback_eligible is False


@pytest.mark.parametrize(
    "error_factory",
    [
        lambda request: httpx.ConnectError(
            f"connection failed for {request.url}",
            request=request,
        ),
        lambda request: httpx.ReadTimeout(
            f"request timed out for {request.url}",
            request=request,
        ),
        lambda request: openai.APIConnectionError(request=request),
        lambda request: openai.APITimeoutError(request=request),
        lambda request: anthropic.APIConnectionError(request=request),
        lambda request: anthropic.APITimeoutError(request=request),
    ],
)
def test_sdk_network_errors_allow_retry_and_fallback(error_factory) -> None:
    method = "POST"
    host = "example.com"
    request = httpx.Request(f"{method}", f"https://{host}")

    decision = classify_model_error(error_factory(request))

    assert decision.kind == "transient"
    assert decision.retryable is True
    assert decision.fallback_eligible is True


def test_unknown_error_does_not_allow_retry_or_fallback() -> None:
    kind = "unexpected"
    decision = classify_model_error(RuntimeError(f"{kind}"))

    assert decision.kind == "unknown"
    assert decision.retryable is False
    assert decision.fallback_eligible is False


def test_quota_is_not_misclassified_as_short_rate_limit() -> None:
    decision = classify_model_error(
        ResponseStatusError(429, "insufficient_quota: usage limit reached"),
    )
    recovery = classify_model_recovery(
        ResponseStatusError(429, "insufficient_quota"),
    )

    assert decision.kind == "quota_exhausted"
    assert decision.retryable is False
    assert recovery.failure_class is ModelFailureClass.QUOTA_EXHAUSTED
    assert recovery.disposition is ModelRecoveryDisposition.WAIT_RESOURCE


def test_rate_limit_recovery_preserves_retry_after_seconds() -> None:
    error = HttpError(429)
    error.headers = {"Retry-After": "12.5"}  # type: ignore[attr-defined]

    recovery = classify_model_recovery(error)

    assert recovery.failure_class is ModelFailureClass.RATE_LIMITED
    assert recovery.disposition is ModelRecoveryDisposition.WAIT_RESOURCE
    assert recovery.retry_after_seconds == 12.5


def test_retry_after_supports_http_date_and_rejects_non_finite() -> None:
    current = datetime(2026, 10, 8, 0, 0, tzinfo=timezone.utc)
    dated = HttpError(429)
    dated.headers = {  # type: ignore[attr-defined]
        "Retry-After": "Thu, 08 Oct 2026 00:02:00 GMT",
    }
    invalid = HttpError(429)
    invalid.headers = {"Retry-After": "inf"}  # type: ignore[attr-defined]

    assert extract_retry_after_seconds(dated, now=current) == 120.0
    assert extract_retry_after_seconds(invalid, now=current) is None


def test_partial_stream_uses_continuation_not_transport_replay() -> None:
    recovery = classify_model_recovery(
        ConnectionError("connection lost"),
        emitted_content=True,
    )

    assert recovery.failure_class is ModelFailureClass.STREAM_INTERRUPTED
    assert (
        recovery.disposition
        is ModelRecoveryDisposition.CONTINUE_MODEL_STEP
    )


def test_user_cancellation_never_enters_network_recovery() -> None:
    recovery = classify_model_recovery(asyncio.CancelledError())

    assert recovery.failure_class is ModelFailureClass.USER_INTERRUPTED
    assert recovery.disposition is ModelRecoveryDisposition.STOP_INTERRUPTED
