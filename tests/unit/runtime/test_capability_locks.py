# -*- coding: utf-8 -*-
"""Tests for immutable Lite capability lock persistence."""

# pylint: disable=protected-access

from __future__ import annotations

import pytest
from pydantic import ValidationError

from qwenpaw.capabilities import GenerationRegistry
from qwenpaw.runtime.assembly import RuntimeAssemblyFactory
from qwenpaw.runtime.capability_locks import (
    CapabilityLockCompiler,
    CapabilityLockConflictError,
    FilesystemCapabilityLockStore,
)


@pytest.mark.asyncio
async def test_compiler_rejects_scope_from_another_registry_epoch(
    tmp_path,
) -> None:
    assembly = await RuntimeAssemblyFactory(GenerationRegistry()).open(
        agent_id="default",
        chat_id="chat-lock-epoch-mismatch",
        session_id="chat-lock-epoch-mismatch",
        root_agent_id="default",
        root_session_id="chat-lock-epoch-mismatch",
        workspace_dir=tmp_path,
    )
    mismatched = assembly.scope.model_copy(
        update={"registry_epoch_id": None},
    )

    with pytest.raises(ValueError, match="registry epoch mismatch"):
        CapabilityLockCompiler.compile(mismatched, assembly._lease)

    await assembly.close()


@pytest.mark.asyncio
async def test_store_is_idempotent_and_rejects_conflicting_lock(
    tmp_path,
) -> None:
    assembly = await RuntimeAssemblyFactory(GenerationRegistry()).open(
        agent_id="default",
        chat_id="chat-lock-conflict",
        session_id="chat-lock-conflict",
        root_agent_id="default",
        root_session_id="chat-lock-conflict",
        workspace_dir=tmp_path,
    )
    store = FilesystemCapabilityLockStore(tmp_path)
    [manifest] = await store.list_for_chat("chat-lock-conflict")

    await store.append(manifest)
    conflicting = manifest.model_copy(
        update={"manifest_hash": f"sha256:{'f' * 64}"},
    )
    with pytest.raises(
        CapabilityLockConflictError,
        match="different capability lock",
    ):
        await store.append(conflicting)

    assert await store.list_for_chat(
        "chat-lock-conflict",
    ) == [manifest]
    await assembly.close()


@pytest.mark.asyncio
async def test_store_rejects_tampered_manifest_hash(tmp_path) -> None:
    assembly = await RuntimeAssemblyFactory(GenerationRegistry()).open(
        agent_id="default",
        chat_id="chat-lock-tamper",
        session_id="chat-lock-tamper",
        root_agent_id="default",
        root_session_id="chat-lock-tamper",
        workspace_dir=tmp_path,
    )
    store = FilesystemCapabilityLockStore(tmp_path)
    [manifest] = await store.list_for_chat("chat-lock-tamper")
    payload = manifest.model_dump(mode="json")
    payload["registry_generation"] += 1

    with pytest.raises(
        ValidationError,
        match="manifest hash is invalid",
    ):
        type(manifest).model_validate(payload)

    payload = manifest.model_dump(mode="json")
    payload["registry_epoch_id"] = "00000000-0000-0000-0000-000000000001"
    with pytest.raises(
        ValidationError,
        match="manifest hash is invalid",
    ):
        type(manifest).model_validate(payload)

    await assembly.close()


@pytest.mark.asyncio
async def test_manifest_reads_legacy_identity_without_changing_hash(
    tmp_path,
) -> None:
    assembly = await RuntimeAssemblyFactory(GenerationRegistry()).open(
        agent_id="default",
        chat_id="chat-lock-legacy",
        session_id="chat-lock-legacy",
        root_agent_id="default",
        root_session_id="chat-lock-legacy",
        workspace_dir=tmp_path,
    )
    store = FilesystemCapabilityLockStore(tmp_path)
    [manifest] = await store.list_for_chat("chat-lock-legacy")
    payload = manifest.model_dump(mode="json")
    payload["conversation_id"] = payload.pop("chat_id")

    restored = type(manifest).model_validate(payload)

    assert restored == manifest
    assert restored.chat_id == "chat-lock-legacy"
    assert restored.conversation_id == "chat-lock-legacy"
    assert restored.manifest_hash == manifest.manifest_hash
    assert "chat_id" in restored.model_json_schema()["properties"]
    assert "conversation_id" not in restored.model_json_schema()["properties"]
    with pytest.raises(ValidationError, match="must identify one Chat"):
        type(manifest).model_validate(
            {
                **manifest.model_dump(mode="json"),
                "conversation_id": "chat-other",
            },
        )
    await assembly.close()
