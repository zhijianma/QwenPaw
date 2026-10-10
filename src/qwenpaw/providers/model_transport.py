# -*- coding: utf-8 -*-
"""Host-owned helpers for privacy-safe model transport validation."""

from __future__ import annotations

import hashlib
import hmac
from secrets import token_bytes
from typing import Any, AsyncIterator, Callable, Literal

from ..kernel.models import (
    ModelTransportContract,
    ModelTransportRecoveryDecision,
    ModelTransportRecoveryMode,
    ModelTransportResumeEvidence,
    ModelTransportValidationReason,
    evaluate_model_transport_recovery,
)

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
        candidate_mode: ModelTransportRecoveryMode,
        expected_response_identity: str | bytes,
        actual_response_identity: str | bytes,
        expected_prefix: bytes,
        actual_prefix: bytes,
        expected_route: str | bytes | None = None,
        actual_route: str | bytes | None = None,
    ) -> ModelTransportResumeEvidence:
        """Build ephemeral evidence for the common Kernel validator."""
        if candidate_mode not in {
            ModelTransportRecoveryMode.INLINE_RESUME,
            ModelTransportRecoveryMode.HTTP_FALLBACK,
        }:
            raise ValueError("transport recovery candidate is not resumable")
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


class ModelTransportResumeError(RuntimeError):
    """Raised when a Provider stream cannot prove cursor continuity."""


def _response_identity(event: Any) -> str | None:
    response = getattr(event, "response", None)
    response_id = getattr(response, "id", None)
    return response_id if isinstance(response_id, str) else None


def _sequence_number(event: Any) -> int | None:
    sequence = getattr(event, "sequence_number", None)
    return sequence if isinstance(sequence, int) else None


def _is_retryable_stream_error(error: BaseException) -> bool:
    """Limit in-place recovery to connection-level transport failures."""
    import httpx
    import openai

    return isinstance(
        error,
        (
            httpx.TransportError,
            openai.APIConnectionError,
            openai.APITimeoutError,
        ),
    )


class ResumableOpenAIResponseStream:
    """Resume one stored background Response from its sequence cursor."""

    def __init__(
        self,
        stream: Any,
        *,
        contract: ModelTransportContract,
        on_recovered: Callable[[ModelTransportRecoveryDecision], None],
        max_resumes: int = 2,
        evidence_hasher: ModelTransportEvidenceHasher | None = None,
    ) -> None:
        self._stream = stream
        self._contract = contract
        self._on_recovered = on_recovered
        self._max_resumes = max_resumes
        self._hasher = evidence_hasher or ModelTransportEvidenceHasher()

    def _observe_event(
        self,
        event: Any,
        *,
        response_id: str | None,
        last_sequence: int | None,
        resumed_after: int | None,
    ) -> tuple[str | None, int | None, int | None]:
        """Validate one event and return the updated stream boundary."""
        event_identity = _response_identity(event)
        if response_id is None and event_identity is not None:
            response_id = event_identity
        elif (
            event_identity is not None
            and response_id is not None
            and event_identity != response_id
        ):
            raise ModelTransportResumeError("response identity changed")

        sequence = _sequence_number(event)
        if (
            sequence is not None
            and last_sequence is not None
            and sequence != last_sequence + 1
        ):
            raise ModelTransportResumeError(
                "response stream sequence is not contiguous",
            )
        if resumed_after is not None:
            if sequence != resumed_after + 1:
                raise ModelTransportResumeError(
                    "resumed stream did not continue after cursor",
                )
            if response_id is None:
                raise ModelTransportResumeError(
                    "resumed stream has no response identity",
                )
            boundary = f"{response_id}:{resumed_after}".encode("utf-8")
            evidence = self._hasher.build_evidence(
                candidate_mode=ModelTransportRecoveryMode.INLINE_RESUME,
                expected_response_identity=response_id,
                actual_response_identity=response_id,
                expected_prefix=boundary,
                actual_prefix=boundary,
            )
            decision = evaluate_model_transport_recovery(
                self._contract,
                evidence,
            )
            if (
                decision.mode is not ModelTransportRecoveryMode.INLINE_RESUME
                or decision.reason
                is not ModelTransportValidationReason.VERIFIED
            ):
                raise ModelTransportResumeError(
                    "resumed stream evidence was rejected",
                )
            self._on_recovered(decision)
            resumed_after = None
        if sequence is not None:
            last_sequence = sequence
        return response_id, last_sequence, resumed_after

    async def __aiter__(self) -> AsyncIterator[Any]:
        current = self._stream
        response_id: str | None = None
        last_sequence: int | None = None
        resume_count = 0
        resumed_after: int | None = None
        while True:
            try:
                async for event in current:
                    response_id, last_sequence, resumed_after = (
                        self._observe_event(
                            event,
                            response_id=response_id,
                            last_sequence=last_sequence,
                            resumed_after=resumed_after,
                        )
                    )
                    yield event
                return
            except BaseException as error:
                if (
                    not _is_retryable_stream_error(error)
                    or response_id is None
                    or last_sequence is None
                    or resume_count >= self._max_resumes
                ):
                    raise
                resume_count += 1
                await current.close()
                client = getattr(current, "_client", None)
                responses = getattr(client, "responses", None)
                retrieve = getattr(responses, "retrieve", None)
                if not callable(retrieve):
                    raise ModelTransportResumeError(
                        "OpenAI response stream cannot be resumed",
                    ) from error
                resumed_after = last_sequence
                current = await retrieve(
                    response_id,
                    stream=True,
                    starting_after=last_sequence,
                )


__all__ = [
    "ModelTransportEvidenceHasher",
    "ModelTransportResumeError",
    "ResumableOpenAIResponseStream",
    "TransportEvidenceDomain",
]
