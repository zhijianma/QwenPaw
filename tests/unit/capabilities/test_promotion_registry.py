# -*- coding: utf-8 -*-
"""Registry transaction tests for evaluated capability promotions."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from qwenpaw.capabilities import (
    ActivationError,
    GenerationRegistry,
    ProviderDeactivationError,
    ReleaseRollbackError,
)
from qwenpaw.capabilities.promotions import (
    FilesystemCapabilityPromotionEvidenceStore,
    build_capability_promotion_assessment,
)
from qwenpaw.kernel import (
    CapabilityBundle,
    CapabilityCheckOutcome,
    CapabilityContribution,
    CapabilityEvaluationDecision,
    CapabilityPromotionCandidate,
    CapabilityPromotionAction,
    CapabilityPromotionEvent,
    CapabilityPromotionPhase,
    CapabilityProviderKind,
    CapabilityReleaseTag,
)


class _Factory:
    def __init__(self, factory_id: str) -> None:
        self.factory_id = factory_id

    async def health_check(self) -> bool:
        return True

    async def build(self, context, app_services):
        return context, app_services


class _UnhealthyFactory(_Factory):
    async def health_check(self) -> bool:
        return False


class _RecordingJournal:
    def __init__(self) -> None:
        self.events: list[CapabilityPromotionEvent] = []
        self.fail_phase: CapabilityPromotionPhase | None = None

    async def append(self, event: CapabilityPromotionEvent) -> None:
        if event.phase is self.fail_phase:
            raise OSError("journal unavailable")
        self.events.append(event)

    async def list_events(
        self,
        *,
        provider_id: str | None = None,
        limit: int = 100,
    ) -> Sequence[CapabilityPromotionEvent]:
        events = self.events
        if provider_id is not None:
            events = [
                event
                for event in events
                if event.candidate.provider_id == provider_id
            ]
        return tuple(reversed(events[-limit:]))


class _FailingEvidenceStore:
    async def append(self, _bundle) -> None:
        raise OSError("evidence store unavailable")

    async def get(self, _bundle_id):
        return None

    async def list_for_candidate(self, _candidate_id, *, limit=100):
        del limit
        return ()


class _DenyGate:
    async def evaluate(
        self,
        candidate: CapabilityPromotionCandidate,
        release: CapabilityReleaseTag,
        scenario_evidence=(),
    ):
        return build_capability_promotion_assessment(
            candidate,
            release,
            evaluator_id="tests.deny-gate",
            decision=CapabilityEvaluationDecision.DENY,
            checks=(
                (
                    "scenario.external-write",
                    CapabilityCheckOutcome.FAILED,
                ),
            ),
            additional_evidence=tuple(scenario_evidence),
        )


def _bundle(version: str = "1.0.0") -> CapabilityBundle:
    return CapabilityBundle(
        provider_id="qwenpaw.system.test",
        provider_kind=CapabilityProviderKind.SYSTEM,
        version=version,
        contributions=(
            CapabilityContribution(
                contribution_id="factory",
                slot="agent.factory",
                entrypoint="tests:factory",
            ),
        ),
    )


def _factory(_declaration: CapabilityContribution) -> _Factory:
    return _Factory("qwenpaw.system.test.factory")


@pytest.mark.asyncio
async def test_successful_activation_records_prepared_and_committed() -> None:
    journal = _RecordingJournal()
    registry = GenerationRegistry(promotion_journal=journal)

    snapshot = await registry.activate_bundle(_bundle(), _factory)

    assert snapshot.generation == 2
    assert [event.phase for event in journal.events] == [
        CapabilityPromotionPhase.PREPARED,
        CapabilityPromotionPhase.COMMITTED,
    ]
    assert journal.events[0].operation_id == journal.events[1].operation_id
    assert journal.events[1].target_release == registry.stable_release(
        "qwenpaw.system.test",
    )
    evaluation = journal.events[1].evaluation
    assert evaluation is not None
    assert evaluation.evidence_bundle_id is not None
    [bundle] = await registry.promotion_evidence(
        evaluation.candidate_id,
    )
    assert bundle.bundle_id == evaluation.evidence_bundle_id
    assert {item.check_id for item in bundle.evidence} == {
        "contract.schema",
        "contract.implementation",
        "contract.health",
        "promotion.operator-authorized",
        "promotion.risk.high",
    }
    authorization = next(
        item
        for item in bundle.evidence
        if item.check_id == "promotion.operator-authorized"
    )
    assert authorization.outcome is CapabilityCheckOutcome.NOT_APPLICABLE


@pytest.mark.asyncio
async def test_operator_authorized_promotion_records_passed_evidence() -> None:
    journal = _RecordingJournal()
    registry = GenerationRegistry(promotion_journal=journal)

    await registry.activate_bundle(
        _bundle(),
        _factory,
        operator_authorized=True,
    )

    [bundle] = await registry.promotion_evidence(
        journal.events[-1].candidate.candidate_id,
    )
    authorization = next(
        item
        for item in bundle.evidence
        if item.check_id == "promotion.operator-authorized"
    )
    assert authorization.outcome is CapabilityCheckOutcome.PASSED


@pytest.mark.asyncio
async def test_missing_promotion_evidence_prevents_publication() -> None:
    journal = _RecordingJournal()
    registry = GenerationRegistry(
        promotion_journal=journal,
        promotion_evidence_store=_FailingEvidenceStore(),
    )

    with pytest.raises(ActivationError, match="promotion evidence failed"):
        await registry.activate_bundle(_bundle(), _factory)

    assert registry.generation == 1
    assert registry.stable_release("qwenpaw.system.test") is None
    assert not journal.events


@pytest.mark.asyncio
async def test_repeated_candidate_reuses_durable_evidence_bundle(
    tmp_path,
) -> None:
    journal = _RecordingJournal()
    evidence_store = FilesystemCapabilityPromotionEvidenceStore(tmp_path)
    registry = GenerationRegistry(
        promotion_journal=journal,
        promotion_evidence_store=evidence_store,
    )

    first = await registry.activate_bundle(_bundle(), _factory)
    second = await registry.activate_bundle(_bundle(), _factory)
    candidate_id = journal.events[-1].candidate.candidate_id
    bundles = await registry.promotion_evidence(candidate_id)

    assert first.generation == 2
    assert second.generation == 3
    assert len(bundles) == 1


@pytest.mark.asyncio
async def test_failed_staging_records_denial_without_publication() -> None:
    journal = _RecordingJournal()
    registry = GenerationRegistry(promotion_journal=journal)

    with pytest.raises(ActivationError, match="health check failed"):
        await registry.activate_bundle(
            _bundle(),
            lambda _declaration: _UnhealthyFactory(
                "qwenpaw.system.test.factory",
            ),
        )

    assert registry.generation == 1
    assert registry.stable_release("qwenpaw.system.test") is None
    assert [event.phase for event in journal.events] == [
        CapabilityPromotionPhase.REJECTED,
    ]
    assert journal.events[0].reason_code == "contract.health"


@pytest.mark.asyncio
async def test_prepare_failure_leaves_generation_unchanged() -> None:
    journal = _RecordingJournal()
    journal.fail_phase = CapabilityPromotionPhase.PREPARED
    registry = GenerationRegistry(promotion_journal=journal)

    with pytest.raises(ActivationError, match="prepare failed"):
        await registry.activate_bundle(_bundle(), _factory)

    assert registry.generation == 1
    assert registry.stable_release("qwenpaw.system.test") is None
    assert not journal.events


@pytest.mark.asyncio
async def test_commit_failure_restores_previous_release_and_aborts() -> None:
    journal = _RecordingJournal()
    registry = GenerationRegistry(promotion_journal=journal)
    await registry.activate_bundle(_bundle(), _factory)
    original = registry.stable_release("qwenpaw.system.test")
    journal.events.clear()
    journal.fail_phase = CapabilityPromotionPhase.COMMITTED

    with pytest.raises(ActivationError, match="commit failed"):
        await registry.activate_bundle(_bundle("2.0.0"), _factory)

    assert registry.generation == 2
    assert registry.stable_release("qwenpaw.system.test") == original
    assert [event.phase for event in journal.events] == [
        CapabilityPromotionPhase.PREPARED,
        CapabilityPromotionPhase.ABORTED,
    ]
    assert registry.retained_generations() == (2,)


@pytest.mark.asyncio
async def test_denied_evaluation_records_rejection_and_keeps_stable() -> None:
    journal = _RecordingJournal()
    registry = GenerationRegistry(
        promotion_journal=journal,
        promotion_gate=_DenyGate(),
    )

    with pytest.raises(ActivationError, match="did not allow"):
        await registry.activate_bundle(_bundle(), _factory)

    assert registry.generation == 1
    assert [event.phase for event in journal.events] == [
        CapabilityPromotionPhase.REJECTED,
    ]
    assert journal.events[0].evaluation is not None
    assert (
        journal.events[0].evaluation.decision
        is CapabilityEvaluationDecision.DENY
    )


@pytest.mark.asyncio
async def test_successful_rollback_records_one_wal_operation() -> None:
    journal = _RecordingJournal()
    registry = GenerationRegistry(promotion_journal=journal)
    await registry.activate_bundle(_bundle(), _factory)
    await registry.activate_bundle(_bundle("2.0.0"), _factory)
    promoted = registry.stable_release("qwenpaw.system.test")
    assert promoted is not None
    journal.events.clear()

    snapshot = await registry.rollback_provider(
        "qwenpaw.system.test",
        expected_release_hash=promoted.release_hash,
    )

    assert snapshot.generation == 4
    descriptor = registry.current_descriptor(
        "qwenpaw.system.test.factory",
    )
    assert descriptor is not None
    assert descriptor.version == "1.0.0"
    assert [event.action for event in journal.events] == [
        CapabilityPromotionAction.ROLLBACK,
        CapabilityPromotionAction.ROLLBACK,
    ]
    assert [event.phase for event in journal.events] == [
        CapabilityPromotionPhase.PREPARED,
        CapabilityPromotionPhase.COMMITTED,
    ]
    assert all(
        event.registry_epoch_id == registry.registry_epoch_id
        for event in journal.events
    )


@pytest.mark.asyncio
async def test_rollback_commit_failure_restores_fence_for_retry() -> None:
    journal = _RecordingJournal()
    registry = GenerationRegistry(promotion_journal=journal)
    await registry.activate_bundle(_bundle(), _factory)
    await registry.activate_bundle(_bundle("2.0.0"), _factory)
    promoted = registry.stable_release("qwenpaw.system.test")
    assert promoted is not None
    journal.events.clear()
    journal.fail_phase = CapabilityPromotionPhase.COMMITTED

    with pytest.raises(ReleaseRollbackError, match="commit failed"):
        await registry.rollback_provider(
            "qwenpaw.system.test",
            expected_release_hash=promoted.release_hash,
        )

    assert registry.generation == 3
    assert registry.stable_release("qwenpaw.system.test") == promoted
    assert [event.phase for event in journal.events] == [
        CapabilityPromotionPhase.PREPARED,
        CapabilityPromotionPhase.ABORTED,
    ]
    journal.fail_phase = None
    retried = await registry.rollback_provider(
        "qwenpaw.system.test",
        expected_release_hash=promoted.release_hash,
    )
    assert retried.generation == 4


@pytest.mark.asyncio
async def test_successful_deactivation_records_one_wal_operation() -> None:
    journal = _RecordingJournal()
    registry = GenerationRegistry(promotion_journal=journal)
    await registry.activate_bundle(_bundle(), _factory)
    release = registry.stable_release("qwenpaw.system.test")
    assert release is not None
    journal.events.clear()

    snapshot = await registry.deactivate_provider(
        "qwenpaw.system.test",
    )

    assert snapshot.generation == 3
    assert (
        registry.current_descriptor(
            "qwenpaw.system.test.factory",
        )
        is None
    )
    assert registry.stable_release("qwenpaw.system.test") is None
    assert [event.action for event in journal.events] == [
        CapabilityPromotionAction.DEACTIVATE,
        CapabilityPromotionAction.DEACTIVATE,
    ]
    assert [event.phase for event in journal.events] == [
        CapabilityPromotionPhase.PREPARED,
        CapabilityPromotionPhase.COMMITTED,
    ]
    assert all(event.target_release is None for event in journal.events)
    assert all(
        event.previous_release_hash == release.release_hash
        for event in journal.events
    )
    evidence_bundles = await registry.promotion_evidence(
        journal.events[-1].candidate.candidate_id,
    )
    authorization_evidence = [
        evidence
        for bundle in evidence_bundles
        for evidence in bundle.evidence
        if evidence.check_id == "deactivate.operator-authorized"
    ]
    assert len(authorization_evidence) == 1
    assert (
        authorization_evidence[0].outcome
        is CapabilityCheckOutcome.NOT_APPLICABLE
    )


@pytest.mark.asyncio
async def test_operator_authorized_deactivation_records_evidence() -> None:
    journal = _RecordingJournal()
    registry = GenerationRegistry(promotion_journal=journal)
    await registry.activate_bundle(_bundle(), _factory)
    journal.events.clear()

    await registry.deactivate_provider(
        "qwenpaw.system.test",
        operator_authorized=True,
    )

    evidence_bundles = await registry.promotion_evidence(
        journal.events[-1].candidate.candidate_id,
    )
    authorization_evidence = [
        evidence
        for bundle in evidence_bundles
        for evidence in bundle.evidence
        if evidence.check_id == "deactivate.operator-authorized"
    ]
    assert len(authorization_evidence) == 1
    assert authorization_evidence[0].outcome is CapabilityCheckOutcome.PASSED


@pytest.mark.asyncio
async def test_deactivation_prepare_failure_keeps_provider_active() -> None:
    journal = _RecordingJournal()
    registry = GenerationRegistry(promotion_journal=journal)
    await registry.activate_bundle(_bundle(), _factory)
    release = registry.stable_release("qwenpaw.system.test")
    journal.events.clear()
    journal.fail_phase = CapabilityPromotionPhase.PREPARED

    with pytest.raises(ProviderDeactivationError, match="prepare failed"):
        await registry.deactivate_provider("qwenpaw.system.test")

    assert registry.generation == 2
    assert registry.stable_release("qwenpaw.system.test") == release
    assert (
        registry.current_descriptor(
            "qwenpaw.system.test.factory",
        )
        is not None
    )
    assert not journal.events


@pytest.mark.asyncio
async def test_deactivation_commit_failure_restores_rollback_fence() -> None:
    journal = _RecordingJournal()
    registry = GenerationRegistry(promotion_journal=journal)
    await registry.activate_bundle(_bundle(), _factory)
    await registry.activate_bundle(_bundle("2.0.0"), _factory)
    release = registry.stable_release("qwenpaw.system.test")
    assert release is not None
    journal.events.clear()
    journal.fail_phase = CapabilityPromotionPhase.COMMITTED

    with pytest.raises(ProviderDeactivationError, match="commit failed"):
        await registry.deactivate_provider("qwenpaw.system.test")

    assert registry.generation == 3
    assert registry.stable_release("qwenpaw.system.test") == release
    descriptor = registry.current_descriptor(
        "qwenpaw.system.test.factory",
    )
    assert descriptor is not None
    assert descriptor.version == "2.0.0"
    assert [event.phase for event in journal.events] == [
        CapabilityPromotionPhase.PREPARED,
        CapabilityPromotionPhase.ABORTED,
    ]
    journal.fail_phase = None
    rolled_back = await registry.rollback_provider(
        "qwenpaw.system.test",
        expected_release_hash=release.release_hash,
    )
    assert rolled_back.generation == 4


@pytest.mark.asyncio
async def test_missing_provider_deactivation_is_idempotent() -> None:
    journal = _RecordingJournal()
    registry = GenerationRegistry(promotion_journal=journal)

    snapshot = await registry.deactivate_provider("qwenpaw.system.missing")

    assert snapshot.generation == 1
    assert not journal.events
