# -*- coding: utf-8 -*-
"""Content-addressed local artifact storage for QwenPaw Lite."""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import JsonValue

from ..kernel.models import (
    ArtifactRef,
    EvidenceRef,
    RunnerSignal,
)
from ..kernel.ports import ArtifactStore


class ArtifactIntegrityError(IOError):
    """Raised when durable artifact bytes do not match their reference."""


class FilesystemArtifactStore:
    """Persist large payloads outside the ledger by SHA-256 digest."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def _path(self, digest: str) -> Path:
        return self._root / "sha256" / digest[:2] / digest

    def _put_sync(self, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        target = self._path(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            return digest
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{digest}.",
            dir=target.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(file_descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return digest

    async def put(
        self,
        *,
        kind: str,
        media_type: str,
        content: bytes,
        metadata: dict[str, JsonValue] | None = None,
    ) -> ArtifactRef:
        """Persist bytes without blocking the event loop."""
        digest = await asyncio.to_thread(self._put_sync, content)
        return ArtifactRef(
            kind=kind,
            uri=f"qwenpaw-artifact://sha256/{digest}",
            media_type=media_type,
            content_hash=f"sha256:{digest}",
            size_bytes=len(content),
            metadata=metadata or {},
        )

    async def read(self, artifact: ArtifactRef) -> bytes:
        """Read and verify bytes addressed only by the trusted digest."""
        digest = artifact.content_hash.removeprefix("sha256:")
        content = await asyncio.to_thread(self._path(digest).read_bytes)
        actual = hashlib.sha256(content).hexdigest()
        if actual != digest:
            raise ArtifactIntegrityError(
                f"artifact digest mismatch: {artifact.artifact_id}",
            )
        return content


def lite_artifact_store(project_dir: Path) -> ArtifactStore:
    """Return the Lite content store for one authorized project."""
    return FilesystemArtifactStore(
        project_dir / ".qwenpaw" / "lite" / "artifacts",
    )


def artifact_filename(metadata: dict[str, Any], artifact_id: UUID) -> str:
    """Return a safe display name without trusting path fragments."""
    candidate = str(metadata.get("name", "")).replace("\\", "/")
    filename = candidate.rsplit("/", maxsplit=1)[-1].strip()
    if filename in {"", ".", ".."}:
        return f"artifact-{artifact_id}"
    return filename


class StoredArtifactEmitter:
    """Create canonical artifact signals over an injected content store."""

    def __init__(self, store: ArtifactStore, producer: str) -> None:
        if not producer.strip():
            raise ValueError("producer cannot be empty")
        self._store = store
        self._producer = producer

    async def emit(
        self,
        *,
        kind: str,
        media_type: str,
        content: bytes,
        name: str | None = None,
        evidence_claim: str | None = None,
        metadata: dict[str, JsonValue] | None = None,
        step_id: UUID | None = None,
        cause_event_id: UUID | None = None,
        correlation_id: UUID | None = None,
    ) -> RunnerSignal:
        """Store bytes and bind optional evidence to the stored reference."""
        artifact_metadata = dict(metadata or {})
        if name is not None:
            artifact_metadata["name"] = name
        artifact = await self._store.put(
            kind=kind,
            media_type=media_type,
            content=content,
            metadata=artifact_metadata,
        )
        evidence = (
            (
                EvidenceRef(
                    artifact_id=artifact.artifact_id,
                    claim=evidence_claim,
                    producer=self._producer,
                ),
            )
            if evidence_claim is not None
            else ()
        )
        return RunnerSignal(
            event_type="artifact.produced",
            step_id=step_id,
            cause_event_id=cause_event_id,
            correlation_id=correlation_id,
            payload={"name": name} if name is not None else {},
            artifact_refs=(artifact,),
            evidence_refs=evidence,
        )
