# -*- coding: utf-8 -*-
"""Tests for Invocation-bound Outcome Host assembly."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from qwenpaw.kernel import (
    CapabilityProviderKind,
    ConversationOutcomeRequest,
    ConversationOutcomeStatus,
    InvocationScope,
    OutcomeProducerRegistration,
)
from qwenpaw.runtime.driver_providers import ProviderDriverHost
from qwenpaw.runtime.outcome_hosts import (
    provider_outcome_host,
    workspace_outcome_broker,
)
from qwenpaw.runtime.tool_providers import ProviderToolHost


def _scope(workspace_dir) -> InvocationScope:
    return InvocationScope(
        invocation_id=uuid4(),
        correlation_id=uuid4(),
        agent_id="default",
        conversation_id="chat-1",
        session_id="legacy-session",
        root_agent_id="default",
        root_session_id="legacy-root",
        workspace_dir=str(workspace_dir),
        registry_generation=7,
    )


@pytest.mark.asyncio
async def test_bound_host_owns_identity_and_generation(tmp_path) -> None:
    workspace = SimpleNamespace(workspace_dir=tmp_path)
    scope = _scope(tmp_path)
    host = provider_outcome_host(
        workspace,
        scope,
        producer_id="qwenpaw.system.workspace-tools",
        provider_kind=CapabilityProviderKind.SYSTEM,
    )
    assert host is not None

    outcome = await host.declare(
        ConversationOutcomeRequest(
            status=ConversationOutcomeStatus.PARTIAL,
            summary="The long-running request still has open work.",
        ),
    )

    assert outcome.agent_id == scope.agent_id
    assert outcome.conversation_id == scope.conversation_id
    assert outcome.correlation_id == scope.correlation_id
    assert outcome.invocation_id == scope.invocation_id
    assert outcome.registry_generation == scope.registry_generation
    assert outcome.producer_id == "qwenpaw.system.workspace-tools"


@pytest.mark.asyncio
async def test_plugin_host_pins_admission_across_hot_unregistration(
    tmp_path,
) -> None:
    workspace = SimpleNamespace(workspace_dir=tmp_path)
    scope = _scope(tmp_path)
    plugin_id = "example.plugin.tools"

    denied = provider_outcome_host(
        workspace,
        scope,
        producer_id=plugin_id,
        provider_kind=CapabilityProviderKind.PLUGIN,
    )
    assert denied is None

    broker = workspace_outcome_broker(workspace)
    broker.register(
        OutcomeProducerRegistration(
            producer_id=plugin_id,
            provider_kind=CapabilityProviderKind.PLUGIN,
        ),
    )
    admitted = provider_outcome_host(
        workspace,
        scope,
        producer_id=plugin_id,
        provider_kind=CapabilityProviderKind.PLUGIN,
    )
    assert admitted is not None

    tool_host = ProviderToolHost(plugin_id, {}, None, {}, None, admitted)
    driver_host = ProviderDriverHost(
        provider_id=plugin_id,
        outcomes=admitted,
    )
    assert tool_host.outcome_host() is admitted
    assert driver_host.outcome_host() is admitted

    broker.unregister(plugin_id)
    assert provider_outcome_host(
        workspace,
        scope,
        producer_id=plugin_id,
        provider_kind=CapabilityProviderKind.PLUGIN,
    ) is None
    outcome = await admitted.declare(
        ConversationOutcomeRequest(
            status=ConversationOutcomeStatus.PARTIAL,
            summary="The pinned Invocation may finish after replacement.",
        ),
    )
    assert outcome.registry_generation == 7


def test_workspace_broker_is_singleton(tmp_path) -> None:
    workspace = SimpleNamespace(workspace_dir=tmp_path)

    first = workspace_outcome_broker(workspace)
    second = workspace_outcome_broker(workspace)

    assert second is first
    assert workspace.conversation_outcome_store is not None
