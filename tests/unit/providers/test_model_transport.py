# -*- coding: utf-8 -*-
"""Tests for Host-owned model transport evidence helpers."""

import pytest

from qwenpaw.kernel import (
    ModelTransportRecoveryMode,
)
from qwenpaw.providers.model_transport import ModelTransportEvidenceHasher


def test_transport_evidence_is_keyed_and_domain_separated() -> None:
    first = ModelTransportEvidenceHasher(b"a" * 32)
    second = ModelTransportEvidenceHasher(b"b" * 32)

    response_digest = first.digest("response_identity", "same-value")

    assert response_digest.startswith("hmac-sha256:")
    assert response_digest != first.digest("route", "same-value")
    assert response_digest != second.digest(
        "response_identity",
        "same-value",
    )


def test_transport_evidence_preserves_only_hmacs_and_lengths() -> None:
    hasher = ModelTransportEvidenceHasher(b"a" * 32)

    evidence = hasher.build_evidence(
        candidate_mode=ModelTransportRecoveryMode.INLINE_RESUME,
        expected_response_identity="response-private",
        actual_response_identity="response-private",
        expected_prefix=b"private model output",
        actual_prefix=b"private model output",
        expected_route="route-private",
        actual_route="route-private",
    )
    serialized = evidence.model_dump_json()

    assert evidence.expected_prefix_bytes == 20
    assert "response-private" not in serialized
    assert "private model output" not in serialized
    assert "route-private" not in serialized


def test_transport_evidence_rejects_short_key_and_unpaired_route() -> None:
    with pytest.raises(ValueError, match="too short"):
        ModelTransportEvidenceHasher(b"short")

    hasher = ModelTransportEvidenceHasher(b"a" * 32)
    with pytest.raises(ValueError, match="must be paired"):
        hasher.build_evidence(
            candidate_mode=ModelTransportRecoveryMode.HTTP_FALLBACK,
            expected_response_identity="response",
            actual_response_identity="response",
            expected_prefix=b"prefix",
            actual_prefix=b"prefix",
            expected_route="route",
        )


def test_transport_evidence_rejects_non_resume_candidate() -> None:
    hasher = ModelTransportEvidenceHasher(b"a" * 32)

    with pytest.raises(ValueError, match="not resumable"):
        hasher.build_evidence(
            candidate_mode=(
                ModelTransportRecoveryMode.DURABLE_CONTEXT_REBUILD
            ),
            expected_response_identity="response",
            actual_response_identity="response",
            expected_prefix=b"prefix",
            actual_prefix=b"prefix",
        )
