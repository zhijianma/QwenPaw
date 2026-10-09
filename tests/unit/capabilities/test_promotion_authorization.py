# -*- coding: utf-8 -*-
"""Risk-based authorization tests shared by every provider origin."""

from __future__ import annotations

import pytest

from qwenpaw.capabilities import (
    CapabilityPromotionAuthorizationRequired,
    GenerationRegistry,
    authorize_capability_promotion,
)
from qwenpaw.kernel import (
    CapabilityBundle,
    CapabilityContribution,
    CapabilityPromotionAuthorizationStatus,
    CapabilityPromotionCandidate,
    CapabilityPromotionOrigin,
    CapabilityPromotionRisk,
    CapabilityProviderKind,
)


class _Factory:
    factory_id = "qwenpaw.system.authorization-test.factory"

    async def health_check(self) -> bool:
        return True

    async def build(self, context, app_services):
        return context, app_services


def _bundle(
    *,
    version: str = "1.0.0",
    slot: str = "agent.factory",
) -> CapabilityBundle:
    return CapabilityBundle(
        provider_id="qwenpaw.system.authorization-test",
        provider_kind=CapabilityProviderKind.SYSTEM,
        version=version,
        contributions=(
            CapabilityContribution(
                contribution_id="factory",
                slot=slot,
                entrypoint="tests:factory",
            ),
        ),
    )


def _candidate(bundle: CapabilityBundle) -> CapabilityPromotionCandidate:
    return CapabilityPromotionCandidate.create(
        provider_id=bundle.provider_id,
        provider_kind=bundle.provider_kind,
        version=bundle.version,
        bundle_payload=bundle.model_dump(mode="json"),
    )


def test_low_risk_operator_candidate_does_not_require_human_grant() -> None:
    bundle = _bundle(slot="ui.settings")
    candidate = _candidate(bundle)

    authorization = authorize_capability_promotion(
        bundle,
        candidate,
        origin=CapabilityPromotionOrigin.OPERATOR_REQUEST,
    )

    assert authorization.risk is CapabilityPromotionRisk.LOW
    assert (
        authorization.status
        is CapabilityPromotionAuthorizationStatus.NOT_REQUIRED
    )


@pytest.mark.asyncio
async def test_high_risk_system_promotion_challenges_before_factory() -> None:
    registry = GenerationRegistry()
    calls = 0

    def factory(_declaration):
        nonlocal calls
        calls += 1
        return _Factory()

    with pytest.raises(
        CapabilityPromotionAuthorizationRequired,
    ) as challenge:
        await registry.activate_bundle(
            _bundle(),
            factory,
            promotion_origin=CapabilityPromotionOrigin.OPERATOR_REQUEST,
        )

    assert challenge.value.risk is CapabilityPromotionRisk.HIGH
    assert calls == 0
    assert registry.generation == 1


@pytest.mark.asyncio
async def test_exact_high_risk_system_candidate_can_be_promoted() -> None:
    registry = GenerationRegistry()
    bundle = _bundle()
    candidate = _candidate(bundle)

    snapshot = await registry.activate_bundle(
        bundle,
        lambda _declaration: _Factory(),
        promotion_origin=CapabilityPromotionOrigin.OPERATOR_REQUEST,
        confirmed_candidate_hash=candidate.candidate_hash,
    )

    assert snapshot.generation == 2
    [evidence_bundle] = await registry.promotion_evidence(
        candidate.candidate_id,
    )
    outcomes = {
        item.check_id: item.outcome.value for item in evidence_bundle.evidence
    }
    assert outcomes["promotion.operator-authorized"] == "passed"
    assert outcomes["promotion.risk.high"] == "passed"


@pytest.mark.asyncio
async def test_stale_candidate_confirmation_fails_before_factory() -> None:
    registry = GenerationRegistry()
    calls = 0

    def factory(_declaration):
        nonlocal calls
        calls += 1
        return _Factory()

    stale = _candidate(_bundle(version="0.9.0"))
    with pytest.raises(CapabilityPromotionAuthorizationRequired):
        await registry.activate_bundle(
            _bundle(),
            factory,
            promotion_origin=CapabilityPromotionOrigin.OPERATOR_REQUEST,
            confirmed_candidate_hash=stale.candidate_hash,
        )

    assert calls == 0
    assert registry.generation == 1
