# -*- coding: utf-8 -*-
"""Durable ownership receipts for artifacts uploaded before Chat creation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..kernel.artifacts import ConversationArtifactRecord
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
    invocation_id: UUID | None = None
    correlation_id: UUID | None = None
    registry_generation: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_evidence_artifact(self) -> "_Receipt":
        if self.evidence.artifact_id != self.artifact.artifact_id:
            raise ValueError("conversation evidence artifact mismatch")
        return self


class ConversationArtifactReceiptStore:
    """Bind one genuine upload to at most one persisted Chat."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._history_lock = asyncio.Lock()
        self._history_mtime_ns: int | None = None
        self._history_cache: tuple[ConversationArtifactRecord, ...] = ()

    def _path(self, receipt_id: UUID) -> Path:
        return self._root / "receipts" / f"{receipt_id}.json"

    def _invalidate_history(self) -> None:
        self._history_mtime_ns = None

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
        self._invalidate_history()
        return str(receipt.receipt_id)

    async def create_owned(
        self,
        artifact: ArtifactRef,
        evidence: EvidenceRef,
        *,
        chat_id: str,
        invocation_id: UUID | None = None,
        correlation_id: UUID | None = None,
        registry_generation: int | None = None,
    ) -> str:
        """Persist a host-generated artifact already owned by one Chat."""
        if not chat_id.strip():
            raise ValueError("chat_id cannot be empty")
        receipt = _Receipt(
            receipt_id=uuid4(),
            artifact=artifact,
            evidence=evidence,
            chat_id=chat_id,
            invocation_id=invocation_id,
            correlation_id=correlation_id,
            registry_generation=registry_generation,
        )
        path = self._path(receipt.receipt_id)
        async with get_path_lock(path):
            await write_json_atomic_async(
                path,
                receipt.model_dump(mode="json"),
            )
        self._invalidate_history()
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
                self._invalidate_history()
        return receipt.artifact, receipt.evidence

    @staticmethod
    def _record(receipt: _Receipt) -> ConversationArtifactRecord | None:
        if receipt.chat_id is None:
            return None
        return ConversationArtifactRecord(
            record_id=receipt.receipt_id,
            chat_id=receipt.chat_id,
            artifact=receipt.artifact,
            evidence=receipt.evidence,
            invocation_id=receipt.invocation_id,
            correlation_id=receipt.correlation_id,
            registry_generation=receipt.registry_generation,
            created_at=receipt.evidence.captured_at,
        )

    async def _directory_mtime_ns(self) -> int:
        directory = self._root / "receipts"
        try:
            stat = await asyncio.to_thread(directory.stat)
        except FileNotFoundError:
            return 0
        return stat.st_mtime_ns

    async def _load_history(
        self,
    ) -> tuple[ConversationArtifactRecord, ...]:
        mtime_ns = await self._directory_mtime_ns()
        if self._history_mtime_ns == mtime_ns:
            return self._history_cache
        async with self._history_lock:
            mtime_ns = await self._directory_mtime_ns()
            if self._history_mtime_ns == mtime_ns:
                return self._history_cache
            directory = self._root / "receipts"
            paths = await asyncio.to_thread(
                lambda: tuple(sorted(directory.glob("*.json"))),
            )
            receipts = await asyncio.gather(
                *(read_json_async(path) for path in paths),
            )
            records = tuple(
                record
                for payload in receipts
                if (
                    record := self._record(
                        _Receipt.model_validate(payload),
                    )
                )
                is not None
            )
            self._history_cache = tuple(
                sorted(
                    records,
                    key=lambda item: (
                        item.created_at,
                        str(item.record_id),
                    ),
                    reverse=True,
                ),
            )
            self._history_mtime_ns = mtime_ns
            return self._history_cache

    async def list_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 100,
    ) -> tuple[ConversationArtifactRecord, ...]:
        """List newest owned Artifact/Evidence records for one Chat."""
        if not conversation_id.strip():
            raise ValueError("conversation_id cannot be empty")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        records = await self._load_history()
        return tuple(
            item
            for item in records
            if item.chat_id == conversation_id
        )[:limit]

    async def scan_for_conversation(
        self,
        conversation_id: str,
    ) -> tuple[ConversationArtifactRecord, ...]:
        """Scan all owned records for observation-index reconstruction."""
        if not conversation_id.strip():
            raise ValueError("conversation_id cannot be empty")
        records = await self._load_history()
        return tuple(
            item
            for item in records
            if item.chat_id == conversation_id
        )

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
