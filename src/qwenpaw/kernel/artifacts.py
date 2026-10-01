# -*- coding: utf-8 -*-
"""Conversation-owned Artifact and Evidence registry contracts."""

from __future__ import annotations

from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from .models import (
    ArtifactRef,
    EvidenceRef,
    KernelModel,
    NonEmptyStr,
)


class ConversationArtifactRecord(KernelModel):
    """Immutable Chat ownership record for one Artifact/Evidence pair."""

    record_id: UUID
    conversation_id: NonEmptyStr
    artifact: ArtifactRef
    evidence: EvidenceRef
    invocation_id: UUID | None = None
    correlation_id: UUID | None = None
    registry_generation: int | None = Field(default=None, ge=1)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_evidence_artifact(self) -> "ConversationArtifactRecord":
        """Require Evidence to reference the owned Artifact exactly."""
        if self.evidence.artifact_id != self.artifact.artifact_id:
            raise ValueError("conversation evidence artifact mismatch")
        return self


__all__ = ["ConversationArtifactRecord"]
