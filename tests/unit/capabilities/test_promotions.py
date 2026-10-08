# -*- coding: utf-8 -*-
"""Tests for durable capability promotion domain evidence."""

from __future__ import annotations

import hashlib
import json
import stat
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from qwenpaw.capabilities.promotions import (
    CapabilityPromotionConflictError,
    FilesystemCapabilityPromotionEvidenceStore,
    FilesystemCapabilityPromotionJournal,
    build_capability_promotion_assessment,
    capability_promotion_evidence_artifact,
    capability_promotion_evidence_artifact_content,
)
from qwenpaw.kernel import (
    CapabilityCheckOutcome,
    CapabilityEvaluationDecision,
    CapabilityPromotionAction,
    CapabilityPromotionAssessment,
    CapabilityPromotionCandidate,
    CapabilityPromotionCheck,
    CapabilityPromotionEvaluation,
    CapabilityPromotionEvent,
    CapabilityPromotionEvidenceBundle,
    CapabilityPromotionPhase,
    CapabilityProviderKind,
    CapabilityReleaseTag,
)


def _candidate() -> CapabilityPromotionCandidate:
    return CapabilityPromotionCandidate.create(
        provider_id="example.provider",
        provider_kind=CapabilityProviderKind.PLUGIN,
        version="1.0.0",
        bundle_payload={
            "provider_id": "example.provider",
            "version": "1.0.0",
            "contributions": [],
        },
    )


def _evaluation(
    candidate: CapabilityPromotionCandidate,
    *,
    decision: CapabilityEvaluationDecision = (
        CapabilityEvaluationDecision.ALLOW
    ),
    outcome: CapabilityCheckOutcome = CapabilityCheckOutcome.PASSED,
) -> CapabilityPromotionEvaluation:
    return CapabilityPromotionEvaluation(
        candidate_id=candidate.candidate_id,
        candidate_hash=candidate.candidate_hash,
        evaluator_id="qwenpaw.contract-gate",
        decision=decision,
        checks=(
            CapabilityPromotionCheck(
                check_id="contract.activation",
                outcome=outcome,
            ),
        ),
    )


def _event(
    operation_id=None,
    *,
    phase: CapabilityPromotionPhase = CapabilityPromotionPhase.PREPARED,
    reason_code: str | None = None,
) -> CapabilityPromotionEvent:
    candidate = _candidate()
    release = CapabilityReleaseTag.create(
        provider_id=candidate.provider_id,
        provider_kind=candidate.provider_kind,
        version=candidate.version,
        promoted_generation=2,
        descriptors=(),
    )
    return CapabilityPromotionEvent.create(
        operation_id=operation_id or uuid4(),
        registry_epoch_id=uuid4(),
        action=CapabilityPromotionAction.PROMOTE,
        phase=phase,
        candidate=candidate,
        evaluation=_evaluation(candidate),
        from_generation=1,
        target_generation=2,
        previous_release_hash=None,
        target_release=release,
        reason_code=reason_code,
    )


def _assessment() -> CapabilityPromotionAssessment:
    candidate = _candidate()
    release = CapabilityReleaseTag.create(
        provider_id=candidate.provider_id,
        provider_kind=candidate.provider_kind,
        version=candidate.version,
        promoted_generation=2,
        descriptors=(),
    )
    return build_capability_promotion_assessment(
        candidate,
        release,
        evaluator_id="qwenpaw.contract-gate",
        decision=CapabilityEvaluationDecision.ALLOW,
        checks=(("contract.schema", CapabilityCheckOutcome.PASSED),),
    )


def test_promotion_evidence_artifact_is_canonical_and_content_safe() -> None:
    bundle = _assessment().evidence_bundle
    content = capability_promotion_evidence_artifact_content(bundle)
    artifact = capability_promotion_evidence_artifact(bundle)
    payload = json.loads(content)

    assert artifact.kind == "capability.promotion-evidence"
    assert artifact.uri.endswith(str(bundle.bundle_id))
    assert artifact.content_hash == (
        f"sha256:{hashlib.sha256(content).hexdigest()}"
    )
    assert artifact.size_bytes == len(content)
    assert payload["bundle_id"] == str(bundle.bundle_id)
    assert "created_at" not in payload
    assert all("created_at" not in item for item in payload["evidence"])
    assert "implementation" not in content.decode("utf-8")

    changed_times = bundle.model_copy(
        update={
            "created_at": datetime(2030, 1, 1, tzinfo=timezone.utc),
            "evidence": tuple(
                item.model_copy(
                    update={
                        "created_at": datetime(
                            2030,
                            1,
                            2,
                            tzinfo=timezone.utc,
                        ),
                    },
                )
                for item in bundle.evidence
            ),
        },
    )
    assert capability_promotion_evidence_artifact(changed_times) == artifact
    assert (
        capability_promotion_evidence_artifact_content(changed_times)
        == content
    )


@pytest.mark.asyncio
async def test_evidence_store_is_append_once_queryable_and_owner_only(
    tmp_path,
) -> None:
    store = FilesystemCapabilityPromotionEvidenceStore(tmp_path)
    bundle = _assessment().evidence_bundle

    await store.append(bundle)
    await store.append(bundle)
    persisted = await store.get(bundle.bundle_id)
    listed = await store.list_for_candidate(bundle.candidate_id)
    path = (
        tmp_path
        / "lite"
        / "capability-promotion-evidence"
        / f"{bundle.bundle_id}.json"
    )

    assert persisted == bundle
    assert listed == [bundle]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_assessment_rejects_tampered_or_cross_check_evidence() -> None:
    assessment = _assessment()
    payload = assessment.model_dump(mode="json")
    payload["evidence_bundle"]["evidence"][0]["evidence_hash"] = (
        f"sha256:{'0' * 64}"
    )

    with pytest.raises(ValidationError, match="evidence hash"):
        CapabilityPromotionAssessment.model_validate(payload)

    payload = assessment.model_dump(mode="json")
    payload["evaluation"]["checks"][0]["check_id"] = "contract.health"
    with pytest.raises(ValidationError, match="evidence mismatch"):
        CapabilityPromotionAssessment.model_validate(payload)


def test_bundle_rejects_cross_candidate_evidence() -> None:
    assessment = _assessment()
    payload = assessment.evidence_bundle.model_dump(mode="json")
    payload["candidate_id"] = str(uuid4())

    with pytest.raises(ValidationError, match="candidate mismatch"):
        CapabilityPromotionEvidenceBundle.model_validate(payload)


@pytest.mark.asyncio
async def test_journal_is_append_once_idempotent_and_owner_only(
    tmp_path,
) -> None:
    journal = FilesystemCapabilityPromotionJournal(tmp_path)
    event = _event()

    await journal.append(event)
    await journal.append(event)
    events = await journal.list_events()
    path = (
        tmp_path
        / "lite"
        / "capability-promotions"
        / str(event.operation_id)
        / "prepared.json"
    )

    assert events == [event]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_journal_rejects_conflicting_operation_phase(tmp_path) -> None:
    journal = FilesystemCapabilityPromotionJournal(tmp_path)
    operation_id = uuid4()
    await journal.append(_event(operation_id))

    with pytest.raises(CapabilityPromotionConflictError):
        await journal.append(
            _event(
                operation_id,
                reason_code="promotion.changed",
            ),
        )


@pytest.mark.asyncio
async def test_journal_detects_tampered_event(tmp_path) -> None:
    journal = FilesystemCapabilityPromotionJournal(tmp_path)
    event = _event()
    await journal.append(event)
    path = (
        tmp_path
        / "lite"
        / "capability-promotions"
        / str(event.operation_id)
        / "prepared.json"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["from_generation"] = 9
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValidationError, match="promotion event hash"):
        await journal.list_events()


def test_denied_evaluation_requires_failed_check() -> None:
    candidate = _candidate()

    with pytest.raises(ValidationError, match="requires a failed check"):
        _evaluation(
            candidate,
            decision=CapabilityEvaluationDecision.DENY,
        )


def test_rejected_event_requires_denied_evaluation() -> None:
    event = _event()
    payload = event.model_dump(mode="json")
    payload["phase"] = CapabilityPromotionPhase.REJECTED.value

    with pytest.raises(ValidationError, match="rejected promotion"):
        CapabilityPromotionEvent.model_validate(payload)


def test_legacy_event_without_evidence_bundle_id_keeps_valid_hash() -> None:
    event = _event()
    payload = event.model_dump(mode="json")
    payload["evaluation"].pop("evidence_bundle_id")

    restored = CapabilityPromotionEvent.model_validate(payload)

    assert restored.evaluation is not None
    assert restored.evaluation.evidence_bundle_id is None
    assert restored.event_hash == event.event_hash


def test_transitional_event_with_hashed_null_bundle_remains_valid() -> None:
    event = _event()
    legacy_hash = CapabilityPromotionEvent.calculate_event_hash(
        event_id=event.event_id,
        operation_id=event.operation_id,
        registry_epoch_id=event.registry_epoch_id,
        action=event.action,
        phase=event.phase,
        candidate=event.candidate,
        evaluation=event.evaluation,
        from_generation=event.from_generation,
        target_generation=event.target_generation,
        previous_release_hash=event.previous_release_hash,
        target_release=event.target_release,
        reason_code=event.reason_code,
        occurred_at=event.occurred_at,
        include_nested_none=True,
    )
    payload = event.model_dump(mode="json")
    payload["event_hash"] = legacy_hash

    restored = CapabilityPromotionEvent.model_validate(payload)

    assert restored.event_hash == legacy_hash
