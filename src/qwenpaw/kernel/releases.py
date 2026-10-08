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

    @model_validator(mode="after")
    def validate_evidence_ids(self) -> Self:
        """Reject ambiguous duplicate evidence references."""
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("promotion check evidence IDs must be unique")
        return self


class CapabilityPromotionEvidence(KernelModel):
    """Content-safe proof produced for one candidate check."""

    schema_id: Literal["qwenpaw.capability-promotion-evidence.v1"] = Field(
        default="qwenpaw.capability-promotion-evidence.v1",
        alias="schema",
    )
    evidence_id: UUID
    candidate_id: UUID
    candidate_hash: Sha256Digest
    check_id: NamespacedId
    producer_id: NamespacedId
    outcome: CapabilityCheckOutcome
    capability_ids: tuple[NamespacedId, ...] = ()
    evidence_hash: Sha256Digest
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @staticmethod
    def calculate_identity(
        *,
        candidate_id: UUID,
        candidate_hash: str,
        check_id: str,
        producer_id: str,
        outcome: CapabilityCheckOutcome,
        capability_ids: tuple[str, ...],
    ) -> tuple[UUID, str, tuple[str, ...]]:
        """Calculate canonical evidence identity without constructing it."""
        ordered = tuple(sorted(capability_ids))
        identity = {
            "candidate_id": str(candidate_id),
            "candidate_hash": candidate_hash,
            "check_id": check_id,
            "producer_id": producer_id,
            "outcome": outcome.value,
            "capability_ids": ordered,
        }
        encoded = json.dumps(
            identity,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        evidence_hash = f"sha256:{digest}"
        return (
            uuid5(candidate_id, evidence_hash),
            evidence_hash,
            ordered,
        )

    @classmethod
    def create(
        cls,
        *,
        candidate: CapabilityPromotionCandidate,
        check_id: str,
        producer_id: str,
        outcome: CapabilityCheckOutcome,
        capability_ids: tuple[str, ...],
    ) -> Self:
        """Create deterministic evidence identity without result payloads."""
        evidence_id, evidence_hash, ordered = cls.calculate_identity(
            candidate_id=candidate.candidate_id,
            candidate_hash=candidate.candidate_hash,
            check_id=check_id,
            producer_id=producer_id,
            outcome=outcome,
            capability_ids=capability_ids,
        )
        return cls(
            evidence_id=evidence_id,
            candidate_id=candidate.candidate_id,
            candidate_hash=candidate.candidate_hash,
            check_id=check_id,
            producer_id=producer_id,
            outcome=outcome,
            capability_ids=ordered,
            evidence_hash=evidence_hash,
        )

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        """Bind evidence identity and hash to content-safe facts."""
        evidence_id, evidence_hash, ordered = self.calculate_identity(
            candidate_id=self.candidate_id,
            candidate_hash=self.candidate_hash,
            check_id=self.check_id,
            producer_id=self.producer_id,
            outcome=self.outcome,
            capability_ids=self.capability_ids,
        )
        if self.capability_ids != ordered:
            raise ValueError("promotion evidence capabilities are unstable")
        if self.evidence_id != evidence_id:
            raise ValueError("capability promotion evidence ID is invalid")
        if self.evidence_hash != evidence_hash:
            raise ValueError("capability promotion evidence hash is invalid")
        if len(self.capability_ids) != len(set(self.capability_ids)):
            raise ValueError("promotion evidence capabilities must be unique")
        return self


class CapabilityPromotionEvidenceBundle(KernelModel):
    """Immutable evidence set supporting one candidate evaluation."""

    schema_id: Literal[
        "qwenpaw.capability-promotion-evidence-bundle.v1"
    ] = Field(
        default="qwenpaw.capability-promotion-evidence-bundle.v1",
        alias="schema",
    )
    bundle_id: UUID
    candidate_id: UUID
    candidate_hash: Sha256Digest
    evaluator_id: NamespacedId
    evidence: tuple[CapabilityPromotionEvidence, ...] = Field(min_length=1)
    bundle_hash: Sha256Digest
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @staticmethod
    def calculate_identity(
        *,
        candidate_id: UUID,
        candidate_hash: str,
        evaluator_id: str,
        evidence: tuple[CapabilityPromotionEvidence, ...],
    ) -> tuple[UUID, str]:
        """Calculate canonical bundle identity without constructing it."""
        identity = {
            "candidate_id": str(candidate_id),
            "candidate_hash": candidate_hash,
            "evaluator_id": evaluator_id,
            "evidence": [
                item.model_dump(mode="json", exclude={"created_at"})
                for item in evidence
            ],
        }
        encoded = json.dumps(
            identity,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        bundle_hash = f"sha256:{digest}"
        return uuid5(candidate_id, bundle_hash), bundle_hash

    @classmethod
    def create(
        cls,
        *,
        candidate: CapabilityPromotionCandidate,
        evaluator_id: str,
        evidence: tuple[CapabilityPromotionEvidence, ...],
    ) -> Self:
        """Create one deterministic bundle over immutable evidence."""
        bundle_id, bundle_hash = cls.calculate_identity(
            candidate_id=candidate.candidate_id,
            candidate_hash=candidate.candidate_hash,
            evaluator_id=evaluator_id,
            evidence=evidence,
        )
        return cls(
            bundle_id=bundle_id,
            candidate_id=candidate.candidate_id,
            candidate_hash=candidate.candidate_hash,
            evaluator_id=evaluator_id,
            evidence=evidence,
            bundle_hash=bundle_hash,
        )

    @model_validator(mode="after")
    def validate_bundle(self) -> Self:
        """Reject cross-candidate, duplicate, or tampered evidence."""
        evidence_ids = tuple(item.evidence_id for item in self.evidence)
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("promotion bundle evidence IDs must be unique")
        if any(
            item.candidate_id != self.candidate_id
            or item.candidate_hash != self.candidate_hash
            for item in self.evidence
        ):
            raise ValueError("promotion bundle evidence candidate mismatch")
        bundle_id, bundle_hash = self.calculate_identity(
            candidate_id=self.candidate_id,
            candidate_hash=self.candidate_hash,
            evaluator_id=self.evaluator_id,
            evidence=self.evidence,
        )
        if self.bundle_id != bundle_id:
            raise ValueError("capability evidence bundle ID is invalid")
        if self.bundle_hash != bundle_hash:
            raise ValueError("capability evidence bundle hash is invalid")
        return self


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
    evidence_bundle_id: UUID | None = None
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


class CapabilityPromotionAssessment(KernelModel):
    """Evaluation paired with the exact evidence that supports it."""

    evaluation: CapabilityPromotionEvaluation
    evidence_bundle: CapabilityPromotionEvidenceBundle

    @model_validator(mode="after")
    def validate_assessment(self) -> Self:
        """Require complete, candidate-bound evidence for every check."""
        evaluation = self.evaluation
        bundle = self.evidence_bundle
        if evaluation.evidence_bundle_id != bundle.bundle_id:
            raise ValueError("promotion assessment bundle ID mismatch")
        if evaluation.evaluator_id != bundle.evaluator_id:
            raise ValueError("promotion assessment evaluator mismatch")
        if (
            evaluation.candidate_id != bundle.candidate_id
            or evaluation.candidate_hash != bundle.candidate_hash
        ):
            raise ValueError("promotion assessment candidate mismatch")
        evidence_by_id = {
            item.evidence_id: item for item in bundle.evidence
        }
        referenced: set[UUID] = set()
        for check in evaluation.checks:
            if not check.evidence_ids:
                raise ValueError("promotion checks require evidence")
            for evidence_id in check.evidence_ids:
                evidence = evidence_by_id.get(evidence_id)
                if evidence is None:
                    raise ValueError("promotion check evidence is missing")
                if evidence.check_id != check.check_id:
                    raise ValueError("promotion check evidence mismatch")
                if evidence.outcome is not check.outcome:
                    raise ValueError("promotion evidence outcome mismatch")
                referenced.add(evidence_id)
        if referenced != set(evidence_by_id):
            raise ValueError("promotion bundle contains unreferenced evidence")
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
    def calculate_event_hash(
        *,
        include_nested_none: bool = False,
        **values: object,
    ) -> str:
        """Hash every event field except its stored integrity digest."""
        if values.get("registry_epoch_id") is None:
            values.pop("registry_epoch_id", None)
        payload = {
            key: (
                value.model_dump(
                    mode="json",
                    exclude_none=not include_nested_none,
                )
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
            legacy_hash = None
            if (
                self.evaluation is not None
                and self.evaluation.evidence_bundle_id is None
            ):
                legacy_hash = self.calculate_event_hash(
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
                    include_nested_none=True,
                )
            if self.event_hash != legacy_hash:
                raise ValueError(
                    "capability promotion event hash is invalid",
                )
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
    "CapabilityPromotionAssessment",
    "CapabilityPromotionCandidate",
    "CapabilityPromotionCheck",
    "CapabilityPromotionEvidence",
    "CapabilityPromotionEvidenceBundle",
    "CapabilityPromotionEvaluation",
    "CapabilityPromotionEvent",
    "CapabilityPromotionPhase",
    "CapabilityReleaseTag",
]
