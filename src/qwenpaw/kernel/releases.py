# -*- coding: utf-8 -*-
"""Stable release identities for promoted capability providers."""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Literal, Self
from uuid import UUID, NAMESPACE_URL, uuid4, uuid5

from pydantic import AwareDatetime, Field, model_validator

from .capability_locks import CapabilityRelease, Sha256Digest
from .models import (
    CapabilityDescriptor,
    CapabilityProviderKind,
    KernelModel,
    NamespacedId,
    NonEmptyStr,
    utc_now,
)


class CapabilityEvaluationDecision(str, Enum):
    """Host decision produced by one candidate evaluation."""

    ALLOW = "allow"
    DENY = "deny"
    INDETERMINATE = "indeterminate"


class CapabilityCheckOutcome(str, Enum):
    """Outcome of one deterministic promotion check."""

    PASSED = "passed"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"


class CapabilityPromotionAction(str, Enum):
    """Registry mutation represented by one promotion operation."""

    PROMOTE = "promote"
    ROLLBACK = "rollback"
    DEACTIVATE = "deactivate"


class CapabilityPromotionPhase(str, Enum):
    """Durable WAL phase for one promotion operation."""

    PREPARED = "prepared"
    COMMITTED = "committed"
    REJECTED = "rejected"
    ABORTED = "aborted"


class CapabilityPromotionCandidate(KernelModel):
    """Content-safe identity of one provider bundle candidate."""

    schema_id: Literal["qwenpaw.capability-promotion-candidate.v1"] = Field(
        default="qwenpaw.capability-promotion-candidate.v1",
        alias="schema",
    )
    candidate_id: UUID
    provider_id: NamespacedId
    provider_kind: CapabilityProviderKind
    version: NonEmptyStr
    candidate_hash: Sha256Digest

    @classmethod
    def create(
        cls,
        *,
        provider_id: str,
        provider_kind: CapabilityProviderKind,
        version: str,
        bundle_payload: dict,
    ) -> Self:
        """Hash a validated public bundle without storing its payload."""
        encoded = json.dumps(
            bundle_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        candidate_hash = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
        return cls(
            candidate_id=uuid5(NAMESPACE_URL, candidate_hash),
            provider_id=provider_id,
            provider_kind=provider_kind,
            version=version,
            candidate_hash=candidate_hash,
        )

    @model_validator(mode="after")
    def validate_candidate_id(self) -> Self:
        """Bind candidate identity to its content hash."""
        expected = uuid5(NAMESPACE_URL, self.candidate_hash)
        if self.candidate_id != expected:
            raise ValueError("capability candidate identity is invalid")
        return self


class CapabilityPromotionCheck(KernelModel):
    """One structured, evidence-addressable promotion check."""

    schema_id: Literal["qwenpaw.capability-promotion-check.v1"] = Field(
        default="qwenpaw.capability-promotion-check.v1",
        alias="schema",
    )
    check_id: NamespacedId
    outcome: CapabilityCheckOutcome
    evidence_ids: tuple[UUID, ...] = ()


class CapabilityPromotionEvaluation(KernelModel):
    """Host-owned decision over one immutable candidate."""

    schema_id: Literal["qwenpaw.capability-promotion-evaluation.v1"] = Field(
        default="qwenpaw.capability-promotion-evaluation.v1",
        alias="schema",
    )
    evaluation_id: UUID = Field(default_factory=uuid4)
    candidate_id: UUID
    candidate_hash: Sha256Digest
    evaluator_id: NamespacedId
    decision: CapabilityEvaluationDecision
    checks: tuple[CapabilityPromotionCheck, ...] = Field(min_length=1)
    evaluated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        """Keep the aggregate decision consistent with check outcomes."""
        outcomes = {item.outcome for item in self.checks}
        identifiers = [item.check_id for item in self.checks]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("promotion check IDs must be unique")
        if self.decision is CapabilityEvaluationDecision.ALLOW:
            if CapabilityCheckOutcome.FAILED in outcomes:
                raise ValueError("allowed promotion cannot contain failures")
        elif self.decision is CapabilityEvaluationDecision.DENY:
            if CapabilityCheckOutcome.FAILED not in outcomes:
                raise ValueError("denied promotion requires a failed check")
        elif CapabilityCheckOutcome.FAILED in outcomes:
            raise ValueError("indeterminate promotion cannot contain failures")
        return self


class CapabilityPromotionEvent(KernelModel):
    """Append-only WAL evidence for one registry promotion phase."""

    schema_id: Literal["qwenpaw.capability-promotion-event.v1"] = Field(
        default="qwenpaw.capability-promotion-event.v1",
        alias="schema",
    )
    event_id: UUID
    operation_id: UUID
    registry_epoch_id: UUID | None = None
    action: CapabilityPromotionAction
    phase: CapabilityPromotionPhase
    candidate: CapabilityPromotionCandidate
    evaluation: CapabilityPromotionEvaluation | None = None
    from_generation: int = Field(ge=1)
    target_generation: int | None = Field(default=None, ge=2)
    previous_release_hash: Sha256Digest | None = None
    target_release: "CapabilityReleaseTag | None" = None
    reason_code: NamespacedId | None = None
    occurred_at: AwareDatetime = Field(default_factory=utc_now)
    event_hash: Sha256Digest

    @classmethod
    def create(
        cls,
        *,
        operation_id: UUID,
        registry_epoch_id: UUID | None,
        action: CapabilityPromotionAction,
        phase: CapabilityPromotionPhase,
        candidate: CapabilityPromotionCandidate,
        evaluation: CapabilityPromotionEvaluation | None,
        from_generation: int,
        target_generation: int | None,
        previous_release_hash: str | None,
        target_release: "CapabilityReleaseTag | None",
        reason_code: str | None = None,
    ) -> Self:
        """Create deterministic event identity with an integrity hash."""
        event_id = uuid5(operation_id, phase.value)
        occurred_at = utc_now()
        event_hash = cls.calculate_event_hash(
            event_id=event_id,
            operation_id=operation_id,
            registry_epoch_id=registry_epoch_id,
            action=action,
            phase=phase,
            candidate=candidate,
            evaluation=evaluation,
            from_generation=from_generation,
            target_generation=target_generation,
            previous_release_hash=previous_release_hash,
            target_release=target_release,
            reason_code=reason_code,
            occurred_at=occurred_at,
        )
        return cls(
            event_id=event_id,
            operation_id=operation_id,
            registry_epoch_id=registry_epoch_id,
            action=action,
            phase=phase,
            candidate=candidate,
            evaluation=evaluation,
            from_generation=from_generation,
            target_generation=target_generation,
            previous_release_hash=previous_release_hash,
            target_release=target_release,
            reason_code=reason_code,
            occurred_at=occurred_at,
            event_hash=event_hash,
        )

    @staticmethod
    def calculate_event_hash(**values: object) -> str:
        """Hash every event field except its stored integrity digest."""
        if values.get("registry_epoch_id") is None:
            values.pop("registry_epoch_id", None)
        payload = {
            key: (
                value.model_dump(mode="json")
                if isinstance(value, KernelModel)
                else value.value
                if isinstance(value, Enum)
                else str(value)
                if isinstance(value, UUID)
                else value.isoformat()
                if hasattr(value, "isoformat")
                else value
            )
            for key, value in values.items()
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    @model_validator(mode="after")
    def validate_event(self) -> Self:
        """Reject phase, candidate, and integrity inconsistencies."""
        if self.evaluation is not None and (
            self.candidate.candidate_id != self.evaluation.candidate_id
        ):
            raise ValueError("promotion evaluation candidate is inconsistent")
        if self.evaluation is not None and (
            self.evaluation.candidate_hash != self.candidate.candidate_hash
        ):
            raise ValueError("promotion evaluation hash is inconsistent")
        if self.phase is CapabilityPromotionPhase.REJECTED:
            if self.evaluation is None or (
                self.evaluation.decision
                is CapabilityEvaluationDecision.ALLOW
            ):
                raise ValueError("rejected promotion cannot be allowed")
        if self.phase in {
            CapabilityPromotionPhase.PREPARED,
            CapabilityPromotionPhase.COMMITTED,
        }:
            if self.evaluation is None or (
                self.evaluation.decision
                is not CapabilityEvaluationDecision.ALLOW
            ):
                raise ValueError("publishable promotion requires allowance")
            if self.target_generation is None:
                raise ValueError("publishable promotion requires generation")
        expected_event_id = uuid5(self.operation_id, self.phase.value)
        if self.event_id != expected_event_id:
            raise ValueError("promotion event identity is invalid")
        expected_hash = self.calculate_event_hash(
            event_id=self.event_id,
            operation_id=self.operation_id,
            registry_epoch_id=self.registry_epoch_id,
            action=self.action,
            phase=self.phase,
            candidate=self.candidate,
            evaluation=self.evaluation,
            from_generation=self.from_generation,
            target_generation=self.target_generation,
            previous_release_hash=self.previous_release_hash,
            target_release=self.target_release,
            reason_code=self.reason_code,
            occurred_at=self.occurred_at,
        )
        if self.event_hash != expected_hash:
            raise ValueError("capability promotion event hash is invalid")
        return self


class CapabilityReleaseTag(KernelModel):
    """Content-addressed provider release assigned to one channel."""

    schema_id: Literal["qwenpaw.capability-release-tag.v1"] = Field(
        default="qwenpaw.capability-release-tag.v1",
        alias="schema",
    )
    channel: Literal["stable"] = "stable"
    provider_id: NamespacedId
    provider_kind: CapabilityProviderKind
    version: NonEmptyStr
    promoted_generation: int = Field(ge=2)
    releases: tuple[CapabilityRelease, ...] = ()
    release_hash: Sha256Digest

    @property
    def capability_ids(self) -> tuple[str, ...]:
        """Return stable capability identities in release order."""
        return tuple(item.capability_id for item in self.releases)

    @classmethod
    def create(
        cls,
        *,
        provider_id: str,
        provider_kind: CapabilityProviderKind,
        version: str,
        promoted_generation: int,
        descriptors: tuple[CapabilityDescriptor, ...],
    ) -> Self:
        """Create one deterministic stable tag from promoted descriptors."""
        ordered = tuple(
            sorted(descriptors, key=lambda item: item.capability_id),
        )
        return cls(
            provider_id=provider_id,
            provider_kind=provider_kind,
            version=version,
            promoted_generation=promoted_generation,
            releases=tuple(
                CapabilityRelease(
                    capability_id=item.capability_id,
                    slot=item.slot,
                    provider_id=item.provider_id,
                    provider_kind=item.provider_kind,
                    version=item.version,
                    restart_policy=item.restart_policy,
                    descriptor_hash=cls._descriptor_hash(item),
                )
                for item in ordered
            ),
            release_hash=cls.calculate_release_hash(
                provider_id=provider_id,
                provider_kind=provider_kind,
                version=version,
                descriptors=ordered,
            ),
        )

    @staticmethod
    def calculate_release_hash(
        *,
        provider_id: str,
        provider_kind: CapabilityProviderKind,
        version: str,
        descriptors: tuple[CapabilityDescriptor, ...],
    ) -> str:
        """Hash the immutable provider release without generation state."""
        releases = tuple(
            CapabilityRelease(
                capability_id=item.capability_id,
                slot=item.slot,
                provider_id=item.provider_id,
                provider_kind=item.provider_kind,
                version=item.version,
                restart_policy=item.restart_policy,
                descriptor_hash=CapabilityReleaseTag._descriptor_hash(item),
            )
            for item in descriptors
        )
        return CapabilityReleaseTag._hash_releases(
            provider_id=provider_id,
            provider_kind=provider_kind,
            version=version,
            releases=releases,
        )

    @staticmethod
    def _descriptor_hash(descriptor: CapabilityDescriptor) -> str:
        encoded = json.dumps(
            descriptor.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    @staticmethod
    def _hash_releases(
        *,
        provider_id: str,
        provider_kind: CapabilityProviderKind,
        version: str,
        releases: tuple[CapabilityRelease, ...],
    ) -> str:
        payload = {
            "provider_id": provider_id,
            "provider_kind": provider_kind.value,
            "version": version,
            "releases": [
                item.model_dump(mode="json") for item in releases
            ],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        """Reject ambiguous capability ordering or provider identities."""
        capability_ids = self.capability_ids
        if tuple(sorted(capability_ids)) != capability_ids:
            raise ValueError("release capability IDs must be sorted")
        if len(set(capability_ids)) != len(capability_ids):
            raise ValueError("release capability IDs must be unique")
        prefix = f"{self.provider_id}."
        if any(
            not capability_id.startswith(prefix)
            for capability_id in capability_ids
        ):
            raise ValueError("release capability provider is inconsistent")
        if any(
            item.provider_id != self.provider_id
            or item.provider_kind is not self.provider_kind
            or item.version != self.version
            for item in self.releases
        ):
            raise ValueError("release descriptor identity is inconsistent")
        expected_hash = self._hash_releases(
            provider_id=self.provider_id,
            provider_kind=self.provider_kind,
            version=self.version,
            releases=self.releases,
        )
        if self.release_hash != expected_hash:
            raise ValueError("capability release tag hash is invalid")
        return self


CapabilityPromotionEvent.model_rebuild()

__all__ = [
    "CapabilityCheckOutcome",
    "CapabilityEvaluationDecision",
    "CapabilityPromotionAction",
    "CapabilityPromotionCandidate",
    "CapabilityPromotionCheck",
    "CapabilityPromotionEvaluation",
    "CapabilityPromotionEvent",
    "CapabilityPromotionPhase",
    "CapabilityReleaseTag",
]
