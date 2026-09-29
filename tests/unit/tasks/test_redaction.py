# -*- coding: utf-8 -*-
"""Tests for execution payload redaction."""

from qwenpaw.tasks.redaction import REDACTED, redact_payload


def test_redaction_is_recursive_and_does_not_mutate_input() -> None:
    source = {
        "username": "alice",
        "password": "plain-text",
        "nested": {
            "api-key": "secret-key",
            "items": [
                {"access_token": "secret-token"},
                {"token_count": 42},
            ],
        },
    }

    result = redact_payload(source)

    assert result == {
        "username": "alice",
        "password": REDACTED,
        "nested": {
            "api-key": REDACTED,
            "items": [
                {"access_token": REDACTED},
                {"token_count": 42},
            ],
        },
    }
    assert source["password"] == "plain-text"


def test_redaction_handles_authorization_and_cookie_fields() -> None:
    result = redact_payload(
        {
            "Authorization": "Bearer token",
            "session_cookie": "cookie-value",
            "privateKey": "private-key-value",
            "safe": True,
        },
    )

    assert result["Authorization"] == REDACTED
    assert result["session_cookie"] == REDACTED
    assert result["privateKey"] == REDACTED
    assert result["safe"] is True
