# -*- coding: utf-8 -*-
"""Tests for durable Chat artifact ownership receipts."""

import hashlib
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from qwenpaw.app.routers.console import _claim_conversation_artifacts
from qwenpaw.kernel.models import ArtifactRef, EvidenceRef
from qwenpaw.schemas import FileContent
from qwenpaw.tasks.conversation_artifacts import (
    ConversationArtifactOwnershipError,
    ConversationArtifactReceiptError,
    conversation_artifact_receipts,
)


def _artifact(content: bytes = b"hello") -> ArtifactRef:
    digest = hashlib.sha256(content).hexdigest()
    return ArtifactRef(
        kind="chat.attachment",
        uri=f"qwenpaw-artifact://sha256/{digest}",
        media_type="text/plain",
        content_hash=f"sha256:{digest}",
        size_bytes=len(content),
        metadata={"name": "hello.txt"},
    )


@pytest.mark.asyncio
async def test_receipt_claim_is_idempotent_for_owner(tmp_path) -> None:
    store = conversation_artifact_receipts(tmp_path)
    artifact = _artifact()
    evidence = EvidenceRef(
        artifact_id=artifact.artifact_id,
        claim="Uploaded attachment",
        producer="test",
    )
    receipt_id = await store.create(artifact, evidence)

    first = await store.claim(
        receipt_id=receipt_id,
        chat_id="chat-a",
        artifact_id=artifact.artifact_id,
        evidence_id=evidence.evidence_id,
    )
    second = await store.claim(
        receipt_id=receipt_id,
        chat_id="chat-a",
        artifact_id=artifact.artifact_id,
        evidence_id=evidence.evidence_id,
    )

    assert first == second == (artifact, evidence)
    assert await store.resolve(
        receipt_id=receipt_id,
        chat_id="chat-a",
        artifact_id=artifact.artifact_id,
    ) == (artifact, evidence)


@pytest.mark.asyncio
async def test_receipt_rejects_cross_chat_and_forged_identity(
    tmp_path,
) -> None:
    store = conversation_artifact_receipts(tmp_path)
    artifact = _artifact()
    evidence = EvidenceRef(
        artifact_id=artifact.artifact_id,
        claim="Uploaded attachment",
        producer="test",
    )
    receipt_id = await store.create(artifact, evidence)
    await store.claim(
        receipt_id=receipt_id,
        chat_id="chat-a",
        artifact_id=artifact.artifact_id,
        evidence_id=evidence.evidence_id,
    )

    with pytest.raises(ConversationArtifactOwnershipError):
        await store.claim(
            receipt_id=receipt_id,
            chat_id="chat-b",
            artifact_id=artifact.artifact_id,
            evidence_id=evidence.evidence_id,
        )
    with pytest.raises(ConversationArtifactReceiptError):
        await store.claim(
            receipt_id=receipt_id,
            chat_id="chat-a",
            artifact_id=_artifact(b"forged").artifact_id,
            evidence_id=evidence.evidence_id,
        )


@pytest.mark.asyncio
async def test_receipt_allows_read_only_fork_lineage(tmp_path) -> None:
    store = conversation_artifact_receipts(tmp_path)
    artifact = _artifact()
    evidence = EvidenceRef(
        artifact_id=artifact.artifact_id,
        claim="Uploaded attachment",
        producer="test",
    )
    receipt_id = await store.create(artifact, evidence)
    await store.claim(
        receipt_id=receipt_id,
        chat_id="chat-parent",
        artifact_id=artifact.artifact_id,
        evidence_id=evidence.evidence_id,
    )

    inherited = await store.resolve(
        receipt_id=receipt_id,
        chat_id="chat-child",
        artifact_id=artifact.artifact_id,
        inherited_chat_ids=("chat-parent",),
    )

    assert inherited == (artifact, evidence)
    with pytest.raises(ConversationArtifactOwnershipError):
        await store.resolve(
            receipt_id=receipt_id,
            chat_id="unrelated-chat",
            artifact_id=artifact.artifact_id,
            inherited_chat_ids=("another-chat",),
        )


@pytest.mark.asyncio
async def test_console_claim_canonicalizes_and_rejects_partial_link(
    tmp_path,
) -> None:
    store = conversation_artifact_receipts(tmp_path)
    artifact = _artifact()
    evidence = EvidenceRef(
        artifact_id=artifact.artifact_id,
        claim="Uploaded attachment",
        producer="test",
    )
    receipt_id = await store.create(artifact, evidence)
    content = FileContent(
        file_url="hello.txt",
        artifact_ref=artifact.model_dump(mode="json"),
        evidence_ref=evidence.model_dump(mode="json"),
        artifact_receipt=receipt_id,
    )

    await _claim_conversation_artifacts(
        SimpleNamespace(workspace_dir=tmp_path),
        "chat-a",
        [content],
    )

    assert content.artifact_ref == artifact.model_dump(mode="json")
    assert content.evidence_ref == evidence.model_dump(mode="json")
    with pytest.raises(HTTPException) as exc_info:
        await _claim_conversation_artifacts(
            SimpleNamespace(workspace_dir=tmp_path),
            "chat-a",
            [FileContent(file_url="partial.txt", artifact_ref={})],
        )
    assert exc_info.value.status_code == 400
