# -*- coding: utf-8 -*-
"""Durable ownership receipts for artifacts uploaded before Chat creation."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from ..kernel.models import ArtifactRef, EvidenceRef
from ..utils.io_utils import (
    get_path_lock,
    read_json_async,
    write_json_atomic_async,
)


class ConversationArtifactReceiptError(ValueError):
    """Base error for invalid or conflicting conversation ownership."""


class ConversationArtifactReceiptNotFoundError(
    ConversationArtifactReceiptError,
):
    """Raised when an upload receipt does not exist."""


class ConversationArtifactOwnershipError(ConversationArtifactReceiptError):
    """Raised when a receipt belongs to another conversation."""


class _Receipt(BaseModel):
    """Private durable receipt; not part of the public Kernel contract."""

    model_config = ConfigDict(extra="forbid")

    receipt_id: UUID
    artifact: ArtifactRef
    evidence: EvidenceRef
    chat_id: str | None = None


class ConversationArtifactReceiptStore:
    """Bind one genuine upload to at most one persisted Chat."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def _path(self, receipt_id: UUID) -> Path:
        return self._root / "receipts" / f"{receipt_id}.json"

    @staticmethod
    def _parse_id(receipt_id: str) -> UUID:
        try:
            return UUID(receipt_id)
        except (TypeError, ValueError, AttributeError) as exc:
            raise ConversationArtifactReceiptNotFoundError(
                "conversation artifact receipt does not exist",
            ) from exc

    async def create(
        self,
        artifact: ArtifactRef,
        evidence: EvidenceRef,
    ) -> str:
        """Persist one unclaimed upload receipt and return its opaque ID."""
        receipt = _Receipt(
            receipt_id=uuid4(),
            artifact=artifact,
            evidence=evidence,
        )
        path = self._path(receipt.receipt_id)
        async with get_path_lock(path):
            await write_json_atomic_async(
                path,
                receipt.model_dump(mode="json"),
            )
        return str(receipt.receipt_id)

    async def create_owned(
        self,
        artifact: ArtifactRef,
        evidence: EvidenceRef,
        *,
        chat_id: str,
    ) -> str:
        """Persist a host-generated artifact already owned by one Chat."""
        if not chat_id.strip():
            raise ValueError("chat_id cannot be empty")
        receipt = _Receipt(
            receipt_id=uuid4(),
            artifact=artifact,
            evidence=evidence,
            chat_id=chat_id,
        )
        path = self._path(receipt.receipt_id)
        async with get_path_lock(path):
            await write_json_atomic_async(
                path,
                receipt.model_dump(mode="json"),
            )
        return str(receipt.receipt_id)

    async def _read(self, receipt_id: UUID) -> _Receipt:
        try:
            payload = await read_json_async(self._path(receipt_id))
        except FileNotFoundError as exc:
            raise ConversationArtifactReceiptNotFoundError(
                "conversation artifact receipt does not exist",
            ) from exc
        return _Receipt.model_validate(payload)

    async def claim(
        self,
        *,
        receipt_id: str,
        chat_id: str,
        artifact_id: UUID,
        evidence_id: UUID,
    ) -> tuple[ArtifactRef, EvidenceRef]:
        """Atomically bind a genuine receipt to one Chat."""
        parsed_id = self._parse_id(receipt_id)
        path = self._path(parsed_id)
        async with get_path_lock(path):
            receipt = await self._read(parsed_id)
            if (
                receipt.artifact.artifact_id != artifact_id
                or receipt.evidence.evidence_id != evidence_id
            ):
                raise ConversationArtifactReceiptError(
                    "conversation artifact receipt identity mismatch",
                )
            if receipt.chat_id not in {None, chat_id}:
                raise ConversationArtifactOwnershipError(
                    "conversation artifact belongs to another chat",
                )
            if receipt.chat_id is None:
                receipt = receipt.model_copy(update={"chat_id": chat_id})
                await write_json_atomic_async(
                    path,
                    receipt.model_dump(mode="json"),
                )
        return receipt.artifact, receipt.evidence

    async def resolve(
        self,
        *,
        receipt_id: str,
        chat_id: str,
        artifact_id: UUID,
        inherited_chat_ids: tuple[str, ...] = (),
    ) -> tuple[ArtifactRef, EvidenceRef]:
        """Resolve a receipt owned by this Chat or a trusted Fork ancestor."""
        receipt = await self._read(self._parse_id(receipt_id))
        allowed_chat_ids = {chat_id, *inherited_chat_ids}
        if receipt.chat_id not in allowed_chat_ids:
            raise ConversationArtifactOwnershipError(
                "conversation artifact is not owned by this chat",
            )
        if receipt.artifact.artifact_id != artifact_id:
            raise ConversationArtifactReceiptError(
                "conversation artifact receipt identity mismatch",
            )
        return receipt.artifact, receipt.evidence


def conversation_artifact_receipts(
    workspace_dir: Path,
) -> ConversationArtifactReceiptStore:
    """Return the durable receipt store for one workspace."""
    return ConversationArtifactReceiptStore(
        Path(workspace_dir) / ".qwenpaw" / "lite" / "conversation-artifacts",
    )


__all__ = [
    "ConversationArtifactOwnershipError",
    "ConversationArtifactReceiptError",
    "ConversationArtifactReceiptNotFoundError",
    "ConversationArtifactReceiptStore",
    "conversation_artifact_receipts",
]
