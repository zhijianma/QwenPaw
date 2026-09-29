# -*- coding: utf-8 -*-
"""Tests for content-addressed Lite artifact storage."""

from pathlib import Path
from uuid import uuid4

import pytest

from qwenpaw.tasks.artifacts import (
    ArtifactIntegrityError,
    FilesystemArtifactStore,
    StoredArtifactEmitter,
)


@pytest.mark.asyncio
async def test_large_payload_round_trips_outside_ledger(
    tmp_path: Path,
) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    content = b"large-output" * 10_000

    artifact = await store.put(
        kind="tool.output",
        media_type="application/octet-stream",
        content=content,
    )

    assert artifact.size_bytes == len(content)
    assert artifact.uri.startswith("qwenpaw-artifact://sha256/")
    assert await store.read(artifact) == content


@pytest.mark.asyncio
async def test_artifact_read_detects_tampering(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    artifact = await store.put(
        kind="tool.output",
        media_type="text/plain",
        content=b"trusted",
    )
    digest = artifact.content_hash.removeprefix("sha256:")
    path = tmp_path / "artifacts" / "sha256" / digest[:2] / digest
    path.write_bytes(b"tampered")

    with pytest.raises(ArtifactIntegrityError):
        await store.read(artifact)


@pytest.mark.asyncio
async def test_emitter_binds_content_artifact_and_evidence(
    tmp_path: Path,
) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    emitter = StoredArtifactEmitter(store, "plugin.example.runner")
    step_id = uuid4()
    cause_event_id = uuid4()
    correlation_id = uuid4()

    signal = await emitter.emit(
        kind="report",
        media_type="text/markdown",
        content=b"# Result\n",
        name="result.md",
        evidence_claim="Generated report",
        metadata={"revision": 2},
        step_id=step_id,
        cause_event_id=cause_event_id,
        correlation_id=correlation_id,
    )

    artifact = signal.artifact_refs[0]
    assert signal.event_type == "artifact.produced"
    assert signal.payload == {"name": "result.md"}
    assert artifact.metadata == {"revision": 2, "name": "result.md"}
    assert signal.evidence_refs[0].artifact_id == artifact.artifact_id
    assert signal.evidence_refs[0].producer == "plugin.example.runner"
    assert signal.step_id == step_id
    assert signal.cause_event_id == cause_event_id
    assert signal.correlation_id == correlation_id
    assert await store.read(artifact) == b"# Result\n"
