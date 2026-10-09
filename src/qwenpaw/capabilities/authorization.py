# -*- coding: utf-8 -*-
"""Host-owned risk policy for exact capability candidate promotion."""

from __future__ import annotations

from collections.abc import Iterable

from ..kernel.models import CapabilityBundle
from ..kernel.promotion_authorization import (
    CapabilityPromotionAuthorization,
    CapabilityPromotionAuthorizationStatus,
    CapabilityPromotionOrigin,
    CapabilityPromotionRisk,
)
from ..kernel.releases import CapabilityPromotionCandidate
from ..kernel.slots import slot_contract

_RISK_ORDER = {
    CapabilityPromotionRisk.LOW: 0,
    CapabilityPromotionRisk.MEDIUM: 1,
    CapabilityPromotionRisk.HIGH: 2,
}


class CapabilityPromotionAuthorizationRequired(RuntimeError):
    """Require an operator grant bound to an exact candidate."""

    def __init__(
        self,
        candidate: CapabilityPromotionCandidate,
        *,
        risk: CapabilityPromotionRisk,
        capability_ids: tuple[str, ...],
    ) -> None:
        self.provider_id = candidate.provider_id
        self.candidate_id = str(candidate.candidate_id)
        self.candidate_hash = candidate.candidate_hash
        self.risk = risk
        self.capability_ids = capability_ids
        super().__init__(
            f"promotion authorization required for "
            f"'{candidate.provider_id}'",
        )

    def response_detail(self) -> dict[str, object]:
        """Return a content-safe exact-candidate challenge."""
        return {
            "code": "capability_promotion_authorization_required",
            "provider_id": self.provider_id,
            "candidate_id": self.candidate_id,
            "candidate_hash": self.candidate_hash,
            "risk": self.risk.value,
            "capability_ids": list(self.capability_ids),
        }


def promotion_risk_for_slots(
    slots: Iterable[str],
) -> CapabilityPromotionRisk:
    """Return the highest declared risk without trusting the provider."""
    risks = tuple(
        CapabilityPromotionRisk(slot_contract(slot).promotion_risk)
        for slot in slots
    )
    return max(
        risks,
        key=_RISK_ORDER.__getitem__,
        default=CapabilityPromotionRisk.LOW,
    )


def authorize_capability_promotion(
    bundle: CapabilityBundle,
    candidate: CapabilityPromotionCandidate,
    *,
    origin: CapabilityPromotionOrigin,
    confirmed_candidate_hash: str | None = None,
) -> CapabilityPromotionAuthorization:
    """Apply one policy to system and plugin promotion ingress."""
    if candidate.provider_id != bundle.provider_id:
        raise ValueError("promotion candidate provider mismatch")
    risk = promotion_risk_for_slots(item.slot for item in bundle.contributions)
    capability_ids = tuple(
        f"{bundle.provider_id}.{item.contribution_id}"
        for item in bundle.contributions
    )
    if (
        origin is CapabilityPromotionOrigin.OPERATOR_REQUEST
        and risk is not CapabilityPromotionRisk.LOW
        and confirmed_candidate_hash != candidate.candidate_hash
    ):
        raise CapabilityPromotionAuthorizationRequired(
            candidate,
            risk=risk,
            capability_ids=capability_ids,
        )
    status = CapabilityPromotionAuthorizationStatus.NOT_REQUIRED
    if (
        origin is CapabilityPromotionOrigin.OPERATOR_REQUEST
        and risk is not CapabilityPromotionRisk.LOW
    ):
        status = CapabilityPromotionAuthorizationStatus.GRANTED
    return CapabilityPromotionAuthorization(
        candidate_id=candidate.candidate_id,
        candidate_hash=candidate.candidate_hash,
        provider_id=candidate.provider_id,
        origin=origin,
        risk=risk,
        status=status,
    )


def validate_promotion_authorization(
    bundle: CapabilityBundle,
    candidate: CapabilityPromotionCandidate,
    authorization: CapabilityPromotionAuthorization,
) -> CapabilityPromotionAuthorization:
    """Re-evaluate risk and exact identity at the publication boundary."""
    expected = authorize_capability_promotion(
        bundle,
        candidate,
        origin=authorization.origin,
        confirmed_candidate_hash=(
            authorization.candidate_hash
            if authorization.status
            is CapabilityPromotionAuthorizationStatus.GRANTED
            else None
        ),
    )
    if authorization != expected:
        raise ValueError("promotion authorization does not match candidate")
    return authorization


__all__ = [
    "CapabilityPromotionAuthorizationRequired",
    "authorize_capability_promotion",
    "promotion_risk_for_slots",
    "validate_promotion_authorization",
]
