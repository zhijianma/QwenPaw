# -*- coding: utf-8 -*-
"""Conversation-owned Artifact and Evidence registry contracts."""

from __future__ import annotations

from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from .models import (
    ArtifactRecord,
    ArtifactRef,
    EvidenceRef,
    EvidenceRecord,
    KernelModel,
    NonEmptyStr,
    VerificationRecord,
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


class ConversationTaskResultRecords(KernelModel):
    """Task-owned result facts associated with one ChatSpec identity."""

    conversation_id: NonEmptyStr
    artifacts: tuple[ArtifactRecord, ...] = ()
    evidence: tuple[EvidenceRecord, ...] = ()
    verifications: tuple[VerificationRecord, ...] = ()

    @model_validator(mode="after")
    def validate_references(self) -> "ConversationTaskResultRecords":
        """Require evidence and verification references to resolve locally."""
        artifact_ids = {
            record.artifact.artifact_id for record in self.artifacts
        }
        evidence_ids = {
            record.evidence.evidence_id for record in self.evidence
        }
        if any(
            record.evidence.artifact_id not in artifact_ids
            for record in self.evidence
        ):
            raise ValueError("task evidence references an unknown artifact")
        for record in self.verifications:
            result = record.verification
            if not set(result.artifact_ids) <= artifact_ids:
                raise ValueError(
                    "task verification references an unknown artifact",
                )
            if not set(result.evidence_ids) <= evidence_ids:
                raise ValueError(
                    "task verification references unknown evidence",
                )
        return self


__all__ = [
    "ConversationArtifactRecord",
    "ConversationTaskResultRecords",
]
