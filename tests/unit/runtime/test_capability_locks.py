# -*- coding: utf-8 -*-
"""Tests for immutable Lite capability lock persistence."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from qwenpaw.capabilities import GenerationRegistry
from qwenpaw.runtime.assembly import RuntimeAssemblyFactory
from qwenpaw.runtime.capability_locks import (
    CapabilityLockConflictError,
    FilesystemCapabilityLockStore,
)


@pytest.mark.asyncio
async def test_store_is_idempotent_and_rejects_conflicting_lock(
    tmp_path,
) -> None:
    assembly = await RuntimeAssemblyFactory(GenerationRegistry()).open(
        agent_id="default",
        conversation_id="chat-lock-conflict",
        session_id="chat-lock-conflict",
        root_agent_id="default",
        root_session_id="chat-lock-conflict",
        workspace_dir=tmp_path,
    )
    store = FilesystemCapabilityLockStore(tmp_path)
    [manifest] = await store.list_for_conversation("chat-lock-conflict")

    await store.append(manifest)
    conflicting = manifest.model_copy(
        update={"manifest_hash": f"sha256:{'f' * 64}"},
    )
    with pytest.raises(
        CapabilityLockConflictError,
        match="different capability lock",
    ):
        await store.append(conflicting)

    assert await store.list_for_conversation(
        "chat-lock-conflict",
    ) == [manifest]
    await assembly.close()


@pytest.mark.asyncio
async def test_store_rejects_tampered_manifest_hash(tmp_path) -> None:
    assembly = await RuntimeAssemblyFactory(GenerationRegistry()).open(
        agent_id="default",
        conversation_id="chat-lock-tamper",
        session_id="chat-lock-tamper",
        root_agent_id="default",
        root_session_id="chat-lock-tamper",
        workspace_dir=tmp_path,
    )
    store = FilesystemCapabilityLockStore(tmp_path)
    [manifest] = await store.list_for_conversation("chat-lock-tamper")
    payload = manifest.model_dump(mode="json")
    payload["registry_generation"] += 1

    with pytest.raises(
        ValidationError,
        match="manifest hash is invalid",
    ):
        type(manifest).model_validate(payload)

    await assembly.close()
