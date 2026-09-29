# -*- coding: utf-8 -*-
"""Tests for task-independent Driver Provider assembly."""

# pylint: disable=protected-access

import asyncio
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest

import qwenpaw.app.approvals as approvals_package
from qwenpaw.app.approvals.service import ApprovalService
from qwenpaw.capabilities import GenerationRegistry
from qwenpaw.capabilities.system_drivers import WorkspaceDriverProvider
from qwenpaw.kernel.invocation import CapabilitySelection, InvocationScope
from qwenpaw.kernel.invocation import DEFAULT_DRIVER_PROVIDER_ID
from qwenpaw.kernel.models import (
    CapabilityBundle,
    CapabilityContribution,
    CapabilityProviderKind,
    DriverApprovalRequest,
    DriverToolDefinition,
    PromptFragment,
)
from qwenpaw.kernel.driver import DriverApprovalRejectedError
from qwenpaw.kernel.interactions import (
    InteractionRequest,
    InteractionResponse,
)
from qwenpaw.kernel.models import ActorRef, ActorType
from qwenpaw.interactions import InteractionService
from qwenpaw.runtime.assembly import RuntimeAssemblyFactory
from qwenpaw.runtime.builder import AgentBuilder
from qwenpaw.runtime.driver_providers import (
    ProviderDriverHost,
    WorkspaceDriverHost,
    validate_driver_session,
)
from qwenpaw.drivers.adapters.agentscope_tool import (
    adapt_driver_definitions,
)
from qwenpaw.drivers.credentials.types import CredentialRecord


def _scope() -> InvocationScope:
    return InvocationScope(
        agent_id="default",
        session_id="chat",
        root_agent_id="default",
        root_session_id="chat",
        workspace_dir="/tmp/qwenpaw-workspace",
        registry_generation=7,
    )


class _DriverHost:
    async def load(
        self,
    ) -> tuple[list[DriverToolDefinition], list[PromptFragment]]:
        async def invoke(payload: dict[str, object]) -> object:
            return payload

        return [
            DriverToolDefinition(
                provider_id=DEFAULT_DRIVER_PROVIDER_ID,
                capability_id="driver://example/tools/read#invoke",
                name="driver_read",
                invoke=invoke,
            ),
        ], [
            PromptFragment(
                fragment_id=(f"{DEFAULT_DRIVER_PROVIDER_ID}.policy-hint"),
                content="driver policy hint",
            ),
        ]


@pytest.mark.asyncio
async def test_workspace_driver_provider_loads_one_invocation_snapshot() -> (
    None
):
    session = await WorkspaceDriverProvider().open(_scope(), _DriverHost())

    definitions, fragments = validate_driver_session(
        session,
        DEFAULT_DRIVER_PROVIDER_ID,
    )
    tools = adapt_driver_definitions(list(definitions))

    assert len(tools) == 1
    result = await tools[0](path="README.md")
    assert "README.md" in result.content[0].text
    assert session.prompt_fragments()[0].content == "driver policy hint"
    assert fragments == session.prompt_fragments()
    await session.close()


@pytest.mark.asyncio
async def test_builder_opens_driver_from_pinned_assembly(monkeypatch) -> None:
    registry = GenerationRegistry()
    assembly = await RuntimeAssemblyFactory(registry).open(
        agent_id="default",
        session_id="chat",
        root_agent_id="default",
        root_session_id="chat",
        workspace_dir="/tmp/qwenpaw-workspace",
    )
    manager = object()
    captured: list[tuple[object, dict[str, object]]] = []

    async def build_definitions(driver_manager, request_context):
        captured.append((driver_manager, request_context))
        return [], [
            PromptFragment(
                fragment_id=(f"{DEFAULT_DRIVER_PROVIDER_ID}.policy-hint"),
                content="policy hint",
            ),
        ]

    monkeypatch.setattr(
        "qwenpaw.drivers.adapters.agentscope_tool.build_driver_definitions",
        build_definitions,
    )
    context = SimpleNamespace(
        extras={"runtime_assembly": assembly},
        invocation_scope=assembly.scope,
        workspace=SimpleNamespace(driver_manager=manager),
    )

    # pylint: disable=protected-access
    session = await AgentBuilder()._open_driver_session(
        context,
        {"source": "console"},
    )
    # pylint: enable=protected-access

    assert session.prompt_fragments()[0].content == "policy hint"
    assert captured == [(manager, {"source": "console"})]
    await assembly.close()


class _CredentialStore:
    async def get(self, ref: str) -> CredentialRecord:
        if ref != "credential:example-driver":
            raise KeyError(ref)
        return CredentialRecord(
            ref=ref,
            kind="static",
            public={"account": "developer"},
            secrets={"token": "secret-token"},
        )


class _CapturedDriverSession:
    provider_id = "example.drivers.provider"

    def list_tools(self):
        return ()

    def prompt_fragments(self):
        return ()

    async def close(self) -> None:
        return None


class _CapturingDriverProvider:
    provider_id = "example.drivers.provider"

    def __init__(self) -> None:
        self.host = None

    async def health_check(self) -> bool:
        return True

    async def open(self, _scope, host):
        self.host = host
        return _CapturedDriverSession()


@pytest.mark.asyncio
async def test_builder_supplies_config_and_credential() -> None:
    registry = GenerationRegistry()
    provider = _CapturingDriverProvider()
    await registry.activate_bundle(
        _plugin_driver_bundle("1.0.0"),
        lambda _: provider,
    )
    assembly = await RuntimeAssemblyFactory(registry).open(
        agent_id="default",
        session_id="chat",
        root_agent_id="default",
        root_session_id="chat",
        workspace_dir="/tmp/qwenpaw-workspace",
        selection=CapabilitySelection(
            driver_provider_id="example.drivers.provider",
        ),
    )
    context = SimpleNamespace(
        extras={"runtime_assembly": assembly},
        invocation_scope=assembly.scope,
        workspace=SimpleNamespace(
            driver_manager=SimpleNamespace(
                credential_store=_CredentialStore(),
            ),
            config=SimpleNamespace(
                capability_configs={
                    "example.drivers.provider": {"echo_prefix": "safe:"},
                },
                capability_credential_refs={
                    "example.drivers.provider": {
                        "service": "credential:example-driver",
                    },
                },
            ),
        ),
    )

    session = await AgentBuilder()._open_driver_session(
        context,
        {"source": "console"},
    )

    assert provider.host is not None
    assert isinstance(provider.host, ProviderDriverHost)
    assert not hasattr(provider.host, "manager")
    assert not hasattr(provider.host, "request_context")
    assert not hasattr(provider.host, "load")
    config = provider.host.config_snapshot()
    config["echo_prefix"] = "mutated"
    assert provider.host.config_snapshot() == {"echo_prefix": "safe:"}
    handle = provider.host.credential("service")
    assert handle is not None
    assert await handle.public_values() == {"account": "developer"}
    assert await handle.read_secret("token") == "secret-token"
    assert "secret-token" not in repr(handle)
    assert "credential:example-driver" not in repr(handle)
    assert provider.host.credential("foreign") is None
    await session.close()
    await assembly.close()


@pytest.mark.asyncio
async def test_builder_rejects_config_before_provider_open() -> None:
    registry = GenerationRegistry()
    provider = _CapturingDriverProvider()
    await registry.activate_bundle(
        _plugin_driver_bundle("1.0.0"),
        lambda _: provider,
    )
    assembly = await RuntimeAssemblyFactory(registry).open(
        agent_id="default",
        session_id="chat",
        root_agent_id="default",
        root_session_id="chat",
        workspace_dir="/tmp/qwenpaw-workspace",
        selection=CapabilitySelection(
            driver_provider_id="example.drivers.provider",
        ),
    )
    context = SimpleNamespace(
        extras={"runtime_assembly": assembly},
        invocation_scope=assembly.scope,
        workspace=SimpleNamespace(
            driver_manager=None,
            config=SimpleNamespace(
                capability_configs={
                    "example.drivers.provider": {"echo_prefix": 42},
                },
                capability_credential_refs={},
            ),
        ),
    )

    with pytest.raises(ValueError, match="does not match config_schema"):
        await AgentBuilder()._open_driver_session(
            context,
            {},
        )

    assert provider.host is None
    await assembly.close()


def test_driver_session_rejects_foreign_tool_ownership() -> None:
    async def invoke(payload: dict[str, object]) -> object:
        return payload

    session = SimpleNamespace(
        provider_id="example.driver",
        list_tools=lambda: (
            DriverToolDefinition(
                provider_id="foreign.driver",
                capability_id="foreign-tool",
                name="foreign_tool",
                invoke=invoke,
            ),
        ),
        prompt_fragments=lambda: (),
    )

    with pytest.raises(ValueError, match="foreign ownership"):
        validate_driver_session(session, "example.driver")


def _approval_context(
    interactions: InteractionService,
) -> dict[str, object]:
    return {
        "agent_id": "default",
        "session_id": "console:driver-plugin",
        "root_session_id": "console:driver-plugin",
        "root_agent_id": "default",
        "user_id": "local-user",
        "channel": "console",
        "os_conversation_id": "driver-plugin-chat",
        "os_invocation_id": ("00000000-0000-0000-0000-000000000303"),
        "_interaction_service": interactions,
    }


@pytest.mark.asyncio
async def test_plugin_driver_approval_uses_unified_interaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approvals = ApprovalService()
    interactions = InteractionService(tmp_path / "interactions.sqlite3")
    monkeypatch.setattr(
        approvals_package,
        "get_approval_service",
        lambda: approvals,
    )
    provider_id = "example.driver"
    host = WorkspaceDriverHost(
        None,
        _approval_context(interactions),
        provider_id,
    )
    approval_task = asyncio.create_task(
        host.require_approval(
            DriverApprovalRequest(
                provider_id=provider_id,
                capability_id="driver://example/tools/write#invoke",
                tool_name="driver_write",
                redacted_arguments={
                    "path": "result.md",
                    "api_key": "must-not-persist",
                },
            ),
        ),
    )

    opened: Sequence[InteractionRequest] = ()
    for _ in range(30):
        opened = await interactions.list_open(
            agent_id="default",
            conversation_id="driver-plugin-chat",
        )
        if opened:
            break
        await asyncio.sleep(0)

    assert len(opened) == 1
    assert opened[0].metadata["source"] == "driver_policy"
    assert opened[0].metadata["arguments"]["api_key"] == "[REDACTED]"
    await interactions.resolve(
        InteractionResponse(
            interaction_id=opened[0].interaction_id,
            idempotency_key="approve-plugin-driver",
            expected_revision=opened[0].revision,
            actor=ActorRef(type=ActorType.USER, id="local-user"),
            selected_option_ids=("approve_exact",),
        ),
    )
    await approval_task


@pytest.mark.asyncio
async def test_plugin_driver_rejection_uses_public_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approvals = ApprovalService()
    interactions = InteractionService(tmp_path / "interactions.sqlite3")
    monkeypatch.setattr(
        approvals_package,
        "get_approval_service",
        lambda: approvals,
    )
    provider_id = "example.driver"
    host = WorkspaceDriverHost(
        None,
        _approval_context(interactions),
        provider_id,
    )
    approval_task = asyncio.create_task(
        host.require_approval(
            DriverApprovalRequest(
                provider_id=provider_id,
                capability_id="driver://example/tools/write#invoke",
                tool_name="driver_write",
            ),
        ),
    )
    opened: Sequence[InteractionRequest] = ()
    for _ in range(30):
        opened = await interactions.list_open(
            agent_id="default",
            conversation_id="driver-plugin-chat",
        )
        if opened:
            break
        await asyncio.sleep(0)
    assert len(opened) == 1
    await interactions.resolve(
        InteractionResponse(
            interaction_id=opened[0].interaction_id,
            idempotency_key="deny-plugin-driver",
            expected_revision=opened[0].revision,
            actor=ActorRef(type=ActorType.USER, id="local-user"),
            selected_option_ids=("deny",),
        ),
    )

    with pytest.raises(
        DriverApprovalRejectedError,
        match="User approval decision was denied",
    ):
        await approval_task


class _PluginDriverProvider:
    provider_id = "example.drivers.provider"

    def __init__(self, version: str) -> None:
        self.version = version

    async def health_check(self) -> bool:
        return True

    async def open(self, scope, host):
        return SimpleNamespace(scope=scope, host=host)


def _plugin_driver_bundle(version: str) -> CapabilityBundle:
    return CapabilityBundle(
        provider_id="example.drivers",
        provider_kind=CapabilityProviderKind.PLUGIN,
        version=version,
        contributions=(
            CapabilityContribution(
                contribution_id="provider",
                slot="driver.provider",
                entrypoint="example:driver_provider",
                config_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "echo_prefix": {"type": "string"},
                    },
                },
            ),
        ),
    )


@pytest.mark.asyncio
async def test_plugin_driver_replacement_keeps_old_invocation() -> None:
    registry = GenerationRegistry()
    await registry.activate_bundle(
        _plugin_driver_bundle("1.0.0"),
        lambda _: _PluginDriverProvider("1.0.0"),
    )
    selection = CapabilitySelection(
        driver_provider_id="example.drivers.provider",
    )
    old = await RuntimeAssemblyFactory(registry).open(
        agent_id="default",
        session_id="old",
        root_agent_id="default",
        root_session_id="old",
        workspace_dir="/tmp/qwenpaw-workspace",
        selection=selection,
    )
    await registry.activate_bundle(
        _plugin_driver_bundle("2.0.0"),
        lambda _: _PluginDriverProvider("2.0.0"),
    )
    new = await RuntimeAssemblyFactory(registry).open(
        agent_id="default",
        session_id="new",
        root_agent_id="default",
        root_session_id="new",
        workspace_dir="/tmp/qwenpaw-workspace",
        selection=selection,
    )

    old_provider = old.require(
        "example.drivers.provider",
        "driver.provider",
    )
    new_provider = new.require(
        "example.drivers.provider",
        "driver.provider",
    )
    assert old_provider.version == "1.0.0"
    assert new_provider.version == "2.0.0"
    await old.close()
    await new.close()
