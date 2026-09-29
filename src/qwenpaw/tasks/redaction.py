# -*- coding: utf-8 -*-
"""Deterministic redaction for payloads persisted in the task ledger."""

from __future__ import annotations

import re

from pydantic import JsonValue

from ..kernel.models import JsonObject

REDACTED = "[REDACTED]"

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SENSITIVE_NAMES = frozenset(
    {
        "api_key",
        "authorization",
        "cookie",
        "credential",
        "password",
        "private_key",
        "secret",
        "session_cookie",
        "token",
    },
)
_SENSITIVE_SUFFIXES = (
    "_api_key",
    "_authorization",
    "_cookie",
    "_credential",
    "_password",
    "_private_key",
    "_secret",
    "_token",
)


def _normalize_key(key: str) -> str:
    """Normalize common snake, kebab, and camel case key styles."""
    with_boundaries = _CAMEL_BOUNDARY.sub("_", key)
    return with_boundaries.strip().lower().replace("-", "_")


def _is_sensitive_key(key: str) -> bool:
    normalized = _normalize_key(key)
    return normalized in _SENSITIVE_NAMES or normalized.endswith(
        _SENSITIVE_SUFFIXES,
    )


def _redact(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return {
            key: REDACTED if _is_sensitive_key(key) else _redact(nested)
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def redact_payload(payload: JsonObject) -> JsonObject:
    """Return a recursively redacted copy of a JSON object."""
    return {
        key: REDACTED if _is_sensitive_key(key) else _redact(value)
        for key, value in payload.items()
    }
