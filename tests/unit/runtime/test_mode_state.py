# -*- coding: utf-8 -*-
"""Tests for Host-owned namespaced Agent Mode state."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from qwenpaw.kernel import (
    AgentModeState,
    AgentModeStateConflictError,
    InvocationScope,
    MAX_MODE_STATE_BYTES,
)
from qwenpaw.runtime.mode_providers import (
    ProviderAgentModeHost,
    WorkspaceAgentModeHost,
    bind_agent_mode_state,
)
from qwenpaw.runtime.mode_state import SQLiteAgentModeStateStore


def _scope(
    tmp_path,
    *,
    generation=1,
    conversation_id="chat-a",
    registry_epoch_id=None,
):
    return InvocationScope(
        agent_id="default",
        conversation_id=conversation_id,
        session_id="transport-a",
        root_agent_id="default",
        root_session_id="transport-a",
        workspace_dir=str(tmp_path),
        registry_epoch_id=registry_epoch_id or uuid4(),
        registry_generation=generation,
    )


@pytest.mark.asyncio
async def test_mode_state_store_enforces_cas_and_namespace_isolation(
    tmp_path,
) -> None:
    store = SQLiteAgentModeStateStore(tmp_path / "mode-state.db")
    first = await store.write(
        AgentModeState(
            provider_id="example.mode-a",
            agent_id="default",
            conversation_id="chat-a",
            value={"step": 1},
            writer_registry_epoch_id=uuid4(),
            writer_generation=1,
        ),
        expected_revision=0,
    )
    assert first.revision == 1
    assert (
        await store.read(
            provider_id="example.mode-a",
            agent_id="default",
            conversation_id="chat-a",
            state_key="default",
        )
        == first
    )
    assert (
        await store.read(
            provider_id="example.mode-b",
            agent_id="default",
            conversation_id="chat-a",
            state_key="default",
        )
        is None
    )
    for owner in (
        {
            "provider_id": "example.mode-a",
            "agent_id": "other",
            "conversation_id": "chat-a",
            "state_key": "default",
        },
        {
            "provider_id": "example.mode-a",
            "agent_id": "default",
            "conversation_id": "chat-b",
            "state_key": "default",
        },
        {
            "provider_id": "example.mode-a",
            "agent_id": "default",
            "conversation_id": "chat-a",
            "state_key": "other",
        },
    ):
        assert await store.read(**owner) is None

    with pytest.raises(AgentModeStateConflictError):
        await store.write(
            first.model_copy(update={"value": {"step": 2}}),
            expected_revision=0,
        )


@pytest.mark.asyncio
async def test_mode_host_survives_provider_generation_replacement(
    tmp_path,
) -> None:
    registry_epoch_id = uuid4()
    first_host = ProviderAgentModeHost(
        {},
        bind_agent_mode_state(
            "example.mode",
            _scope(
                tmp_path,
                generation=1,
                registry_epoch_id=registry_epoch_id,
            ),
        ),
    )
    first = await first_host.write_state(
        {"step": 1},
        expected_revision=0,
    )
    replacement_host = ProviderAgentModeHost(
        {},
        bind_agent_mode_state(
            "example.mode",
            _scope(
                tmp_path,
                generation=2,
                registry_epoch_id=registry_epoch_id,
            ),
        ),
    )
    restored = await replacement_host.read_state()
    assert restored == first
    second = await replacement_host.write_state(
        {"step": 2},
        expected_revision=restored.revision,
        state_schema_version=2,
    )
    assert second.revision == 2
    assert second.writer_generation == 2
    assert second.state_schema_version == 2

    with pytest.raises(AgentModeStateConflictError):
        await first_host.write_state(
            {"step": 3},
            expected_revision=second.revision,
        )

    restarted_host = ProviderAgentModeHost(
        {},
        bind_agent_mode_state(
            "example.mode",
            _scope(tmp_path, generation=1),
        ),
    )
    restarted = await restarted_host.write_state(
        {"step": 3},
        expected_revision=second.revision,
    )
    assert restarted.revision == 3
    assert restarted.writer_registry_epoch_id != registry_epoch_id


@pytest.mark.asyncio
async def test_system_and_plugin_hosts_use_isolated_namespaces(
    tmp_path,
) -> None:
    scope = _scope(tmp_path)
    system = WorkspaceAgentModeHost.capture(
        SimpleNamespace(
            workspace=SimpleNamespace(
                plugins=SimpleNamespace(modes=[]),
            ),
        ),
        state=bind_agent_mode_state("qwenpaw.system.modes", scope),
    )
    plugin = ProviderAgentModeHost(
        {},
        bind_agent_mode_state("example.mode", scope),
    )
    await system.write_state({"owner": "system"}, expected_revision=0)
    await plugin.write_state({"owner": "plugin"}, expected_revision=0)

    assert (await system.read_state()).value == {"owner": "system"}
    assert (await plugin.read_state()).value == {"owner": "plugin"}


@pytest.mark.asyncio
async def test_mode_host_requires_stable_chat_identity(tmp_path) -> None:
    scope = _scope(tmp_path).model_copy(update={"conversation_id": None})
    host = ProviderAgentModeHost(
        {},
        bind_agent_mode_state("example.mode", scope),
    )

    with pytest.raises(RuntimeError, match="ChatSpec.id"):
        await host.write_state({}, expected_revision=0)

    no_epoch = ProviderAgentModeHost(
        {},
        bind_agent_mode_state(
            "example.mode",
            _scope(tmp_path).model_copy(
                update={"registry_epoch_id": None},
            ),
        ),
    )
    with pytest.raises(RuntimeError, match="registry epoch"):
        await no_epoch.write_state({}, expected_revision=0)


def test_mode_state_rejects_oversized_payload() -> None:
    with pytest.raises(ValueError, match="exceeds"):
        AgentModeState(
            provider_id="example.mode",
            agent_id="default",
            conversation_id="chat-a",
            value={"data": "x" * MAX_MODE_STATE_BYTES},
            writer_registry_epoch_id=uuid4(),
            writer_generation=1,
        )
