# -*- coding: utf-8 -*-
"""Shared error classification for retries, checks, and model fallback."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Literal

import anthropic
import httpx
import openai

from ..kernel import ModelFailureClass, ModelRecoveryDisposition
from .error_utils import extract_status_code

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504, 529})

ModelErrorKind = Literal[
    "authentication",
    "bad_request",
    "context_overflow",
    "content_safety",
    "model_not_found",
    "provider_overloaded",
    "quota_exhausted",
    "rate_limited",
    "transient",
    "unknown",
]

_SDK_RATE_LIMIT_ERRORS = (
    openai.RateLimitError,
    anthropic.RateLimitError,
)
_SDK_TRANSIENT_ERRORS = (
    httpx.NetworkError,
    httpx.TimeoutException,
    httpx.RemoteProtocolError,
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.InternalServerError,
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
    anthropic.InternalServerError,
)


def _is_sdk_rate_limit(exc: Exception) -> bool:
    """Return whether an installed SDK identifies a rate-limit error."""
    return isinstance(exc, _SDK_RATE_LIMIT_ERRORS)


def _is_sdk_transient(exc: Exception) -> bool:
    """Return whether an installed SDK identifies a transient failure."""
    return isinstance(exc, _SDK_TRANSIENT_ERRORS)


@dataclass(frozen=True, slots=True)
class ModelErrorDecision:
    """Stable model error policy result."""

    kind: ModelErrorKind
    status_code: int | None
    retryable: bool
    fallback_eligible: bool


@dataclass(frozen=True, slots=True)
class ModelRecoveryDecision:
    """Provider-neutral failure and the Runtime action it permits."""

    failure_class: ModelFailureClass
    disposition: ModelRecoveryDisposition
    retry_after_seconds: float | None = None


class IncompleteModelStreamError(ConnectionError):
    """Raised when a provider stream ends without a terminal chunk."""


def extract_retry_after_seconds(
    exc: Exception,
    *,
    now: datetime | None = None,
) -> float | None:
    """Return a validated delta-seconds hint from provider headers."""
    headers = getattr(exc, "headers", None) or getattr(
        getattr(exc, "response", None),
        "headers",
        None,
    )
    if not headers:
        return None
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(str(raw))
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        seconds = max((retry_at - current).total_seconds(), 0.0)
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def classify_model_error(exc: Exception) -> ModelErrorDecision:
    """Classify whether a model error may retry or cross-model fallback."""
    status = extract_status_code(exc)
    message = str(exc).lower()
    if status in {401, 403}:
        kind: ModelErrorKind = "authentication"
    elif status == 404 or "model not found" in message:
        kind = "model_not_found"
    elif any(
        marker in message
        for marker in (
            "insufficient_quota",
            "quota exceeded",
            "quota_exceeded",
            "usage limit",
        )
    ):
        kind = "quota_exhausted"
    elif status == 429 or _is_sdk_rate_limit(exc):
        kind = "rate_limited"
    elif status in {502, 503, 504, 529}:
        kind = "provider_overloaded"
    elif (
        status in RETRYABLE_STATUS_CODES
        or isinstance(
            exc,
            (ConnectionError, TimeoutError),
        )
        or _is_sdk_transient(exc)
    ):
        kind = "transient"
    elif any(
        marker in message
        for marker in (
            "context length",
            "context_length",
            "maximum context",
            "too many tokens",
        )
    ):
        kind = "context_overflow"
    elif any(
        marker in message
        for marker in (
            "content policy",
            "content_policy",
            "content safety",
            "safety_filter",
            "moderation",
        )
    ):
        kind = "content_safety"
    elif status is not None and 400 <= status < 500:
        kind = "bad_request"
    else:
        kind = "unknown"
    retryable = kind in {
        "provider_overloaded",
        "rate_limited",
        "transient",
    }
    fallback_eligible = retryable or kind == "model_not_found"
    return ModelErrorDecision(
        kind=kind,
        status_code=status,
        retryable=retryable,
        fallback_eligible=fallback_eligible,
    )


def classify_model_recovery(
    exc: BaseException,
    *,
    emitted_content: bool = False,
) -> ModelRecoveryDecision:
    """Map one failed attempt to a stable Runtime recovery contract."""
    if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
        return ModelRecoveryDecision(
            failure_class=ModelFailureClass.USER_INTERRUPTED,
            disposition=ModelRecoveryDisposition.STOP_INTERRUPTED,
        )
    if emitted_content:
        return ModelRecoveryDecision(
            failure_class=ModelFailureClass.STREAM_INTERRUPTED,
            disposition=ModelRecoveryDisposition.CONTINUE_MODEL_STEP,
        )
    if not isinstance(exc, Exception):
        return ModelRecoveryDecision(
            failure_class=ModelFailureClass.UNKNOWN,
            disposition=ModelRecoveryDisposition.FAIL_TERMINAL,
        )
    kind = classify_model_error(exc).kind
    failure_class, disposition = {
        "authentication": (
            ModelFailureClass.AUTHENTICATION_REQUIRED,
            ModelRecoveryDisposition.FAIL_TERMINAL,
        ),
        "bad_request": (
            ModelFailureClass.INVALID_REQUEST,
            ModelRecoveryDisposition.FAIL_TERMINAL,
        ),
        "context_overflow": (
            ModelFailureClass.CONTEXT_OVERFLOW,
            ModelRecoveryDisposition.FAIL_TERMINAL,
        ),
        "content_safety": (
            ModelFailureClass.POLICY_DENIED,
            ModelRecoveryDisposition.FAIL_TERMINAL,
        ),
        "model_not_found": (
            ModelFailureClass.PROVIDER_UNAVAILABLE,
            ModelRecoveryDisposition.RETRY_TRANSPORT,
        ),
        "provider_overloaded": (
            ModelFailureClass.PROVIDER_OVERLOADED,
            ModelRecoveryDisposition.RETRY_TRANSPORT,
        ),
        "quota_exhausted": (
            ModelFailureClass.QUOTA_EXHAUSTED,
            ModelRecoveryDisposition.WAIT_RESOURCE,
        ),
        "rate_limited": (
            ModelFailureClass.RATE_LIMITED,
            ModelRecoveryDisposition.WAIT_RESOURCE,
        ),
        "transient": (
            ModelFailureClass.TRANSPORT_UNAVAILABLE,
            ModelRecoveryDisposition.RETRY_TRANSPORT,
        ),
        "unknown": (
            ModelFailureClass.UNKNOWN,
            ModelRecoveryDisposition.FAIL_TERMINAL,
        ),
    }[kind]
    return ModelRecoveryDecision(
        failure_class=failure_class,
        disposition=disposition,
        retry_after_seconds=(
            extract_retry_after_seconds(exc)
            if failure_class is ModelFailureClass.RATE_LIMITED
            else None
        ),
    )


def is_retryable_same_model(exc: Exception) -> bool:
    """Return whether the same model may be retried."""
    return classify_model_error(exc).retryable


def is_fallback_eligible(exc: Exception) -> bool:
    """Return whether the next configured model may be attempted."""
    return classify_model_error(exc).fallback_eligible
