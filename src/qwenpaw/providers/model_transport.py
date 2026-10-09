# -*- coding: utf-8 -*-
"""Host-owned helpers for privacy-safe model transport validation."""

from __future__ import annotations

import hashlib
import hmac
from secrets import token_bytes
from typing import Literal

from ..kernel import ModelTransportRecoveryMode, ModelTransportResumeEvidence

TransportEvidenceDomain = Literal[
    "response_identity",
    "prefix",
    "route",
]


class ModelTransportEvidenceHasher:
    """Create non-persistent, domain-separated transport evidence digests."""

    def __init__(self, key: bytes | None = None) -> None:
        secret = key if key is not None else token_bytes(32)
        if len(secret) < 32:
            raise ValueError("transport evidence HMAC key is too short")
        self._key = bytes(secret)

    def digest(
        self,
        domain: TransportEvidenceDomain,
        value: str | bytes,
    ) -> str:
        """Return one domain-separated HMAC without retaining raw input."""
        payload = value.encode("utf-8") if isinstance(value, str) else value
        material = domain.encode("ascii") + b"\0" + payload
        digest = hmac.new(self._key, material, hashlib.sha256).hexdigest()
        return f"hmac-sha256:{digest}"

    def build_evidence(
        self,
        *,
        candidate_mode: Literal[
            ModelTransportRecoveryMode.INLINE_RESUME,
            ModelTransportRecoveryMode.HTTP_FALLBACK,
        ],
        expected_response_identity: str | bytes,
        actual_response_identity: str | bytes,
        expected_prefix: bytes,
        actual_prefix: bytes,
        expected_route: str | bytes | None = None,
        actual_route: str | bytes | None = None,
    ) -> ModelTransportResumeEvidence:
        """Build ephemeral evidence for the common Kernel validator."""
        if (expected_route is None) != (actual_route is None):
            raise ValueError("transport route evidence must be paired")
        return ModelTransportResumeEvidence(
            candidate_mode=candidate_mode,
            expected_response_identity_hash=self.digest(
                "response_identity",
                expected_response_identity,
            ),
            actual_response_identity_hash=self.digest(
                "response_identity",
                actual_response_identity,
            ),
            expected_prefix_hash=self.digest("prefix", expected_prefix),
            actual_prefix_hash=self.digest("prefix", actual_prefix),
            expected_prefix_bytes=len(expected_prefix),
            actual_prefix_bytes=len(actual_prefix),
            expected_route_hash=(
                self.digest("route", expected_route)
                if expected_route is not None
                else None
            ),
            actual_route_hash=(
                self.digest("route", actual_route)
                if actual_route is not None
                else None
            ),
        )


__all__ = ["ModelTransportEvidenceHasher", "TransportEvidenceDomain"]
