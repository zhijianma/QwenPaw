# -*- coding: utf-8 -*-
"""Transport-neutral validation of Artifact references in Chat input."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ...kernel import ArtifactRef, EvidenceRef
from ...tasks.conversation_artifacts import (
    ConversationArtifactReceiptError,
    conversation_artifact_receipts,
)


class ConversationInputArtifactError(ValueError):
    """Raised when a Chat input carries incomplete or forged references."""


async def claim_conversation_artifacts(
    workspace: Any,
    chat_id: str,
    content_parts: list[Any],
) -> None:
    """Validate receipts and replace references with canonical data."""
    receipts = conversation_artifact_receipts(Path(workspace.workspace_dir))
    for part in content_parts:
        if isinstance(part, dict):
            raw_artifact = part.get("artifact_ref")
            raw_evidence = part.get("evidence_ref")
            receipt_id = part.get("artifact_receipt")
        else:
            raw_artifact = getattr(part, "artifact_ref", None)
            raw_evidence = getattr(part, "evidence_ref", None)
            receipt_id = getattr(part, "artifact_receipt", None)
        supplied = (
            raw_artifact is not None,
            raw_evidence is not None,
            bool(receipt_id),
        )
        if not any(supplied):
            continue
        if not all(supplied):
            raise ConversationInputArtifactError(
                "Incomplete conversation artifact reference",
            )
        try:
            artifact = ArtifactRef.model_validate(raw_artifact)
            evidence = EvidenceRef.model_validate(raw_evidence)
            if evidence.artifact_id != artifact.artifact_id:
                raise ValueError("artifact and evidence identity mismatch")
            canonical_artifact, canonical_evidence = await receipts.claim(
                receipt_id=str(receipt_id),
                chat_id=chat_id,
                artifact_id=artifact.artifact_id,
                evidence_id=evidence.evidence_id,
            )
        except (
            ConversationArtifactReceiptError,
            ValidationError,
            ValueError,
        ) as exc:
            raise ConversationInputArtifactError(
                "Invalid conversation artifact reference",
            ) from exc
        values = {
            "artifact_ref": canonical_artifact.model_dump(mode="json"),
            "evidence_ref": canonical_evidence.model_dump(mode="json"),
            "artifact_receipt": str(receipt_id),
        }
        if isinstance(part, dict):
            part.update(values)
        else:
            for key, value in values.items():
                setattr(part, key, value)


__all__ = [
    "ConversationInputArtifactError",
    "claim_conversation_artifacts",
]
