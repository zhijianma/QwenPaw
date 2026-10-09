# -*- coding: utf-8 -*-
"""Provider-neutral authorization facts for capability promotion."""

from __future__ import annotations

from enum import Enum
from typing import Literal, Self
from uuid import UUID, NAMESPACE_URL, uuid5

from pydantic import Field, model_validator

from .capability_locks import Sha256Digest
from .models import KernelModel, NamespacedId


class CapabilityPromotionOrigin(str, Enum):
    """Ingress that requested publication of one candidate."""

    INTERNAL_BOOTSTRAP = "internal_bootstrap"
    INTERNAL_RECOVERY = "internal_recovery"
    OPERATOR_REQUEST = "operator_request"


class CapabilityPromotionRisk(str, Enum):
    """Highest declared risk among candidate contribution slots."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CapabilityPromotionAuthorizationStatus(str, Enum):
    """Human-authorization disposition for an exact candidate."""

    NOT_REQUIRED = "not_required"
    GRANTED = "granted"


class CapabilityPromotionAuthorization(KernelModel):
    """Immutable authorization fact bound to one candidate hash."""

    schema_id: Literal[
        "qwenpaw.capability-promotion-authorization.v1"
    ] = Field(
        default="qwenpaw.capability-promotion-authorization.v1",
        alias="schema",
    )
    candidate_id: UUID
    candidate_hash: Sha256Digest
    provider_id: NamespacedId
    origin: CapabilityPromotionOrigin
    risk: CapabilityPromotionRisk
    status: CapabilityPromotionAuthorizationStatus

    @model_validator(mode="after")
    def validate_authorization(self) -> Self:
        """Reject ambiguous grants and identities detached from content."""
        expected = uuid5(NAMESPACE_URL, self.candidate_hash)
        if self.candidate_id != expected:
            raise ValueError("promotion authorization candidate is invalid")
        if self.origin is CapabilityPromotionOrigin.OPERATOR_REQUEST:
            if (
                self.risk is not CapabilityPromotionRisk.LOW
                and self.status
                is not CapabilityPromotionAuthorizationStatus.GRANTED
            ):
                raise ValueError(
                    "medium or high risk operator promotion requires grant",
                )
        elif (
            self.status
            is not CapabilityPromotionAuthorizationStatus.NOT_REQUIRED
        ):
            raise ValueError(
                "internal promotion cannot claim operator authorization",
            )
        return self


__all__ = [
    "CapabilityPromotionAuthorization",
    "CapabilityPromotionAuthorizationStatus",
    "CapabilityPromotionOrigin",
    "CapabilityPromotionRisk",
]
