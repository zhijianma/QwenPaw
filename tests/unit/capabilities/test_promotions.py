# -*- coding: utf-8 -*-
"""Tests for durable capability promotion domain evidence."""

from __future__ import annotations

import json
import stat
from uuid import uuid4

import pytest
from pydantic import ValidationError

from qwenpaw.capabilities.promotions import (
    CapabilityPromotionConflictError,
    FilesystemCapabilityPromotionJournal,
)
from qwenpaw.kernel import (
    CapabilityCheckOutcome,
    CapabilityEvaluationDecision,
    CapabilityPromotionAction,
    CapabilityPromotionCandidate,
    CapabilityPromotionCheck,
    CapabilityPromotionEvaluation,
    CapabilityPromotionEvent,
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
