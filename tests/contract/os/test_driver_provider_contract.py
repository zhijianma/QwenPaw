# -*- coding: utf-8 -*-
"""Shared behavioral contract for system and plugin Driver Providers."""

import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import qwenpaw.app.approvals as approvals_package
from qwenpaw.app.approvals.service import ApprovalService
from qwenpaw.capabilities.system_drivers import WorkspaceDriverProvider
from qwenpaw.drivers.adapters.agentscope_tool import (
    adapt_driver_definitions,
)
from qwenpaw.interactions import InteractionService
from qwenpaw.kernel.interactions import (
    InteractionResponse,
)
from qwenpaw.kernel.invocation import (
    DEFAULT_DRIVER_PROVIDER_ID,
    CapabilitySelection,
    InvocationScope,
)
from qwenpaw.kernel.models import (
    ActorRef,
    ActorType,
    DriverApprovalRequest,
    DriverToolDefinition,
    PromptFragment,
)
from qwenpaw.plugins.architecture import PluginManifest
from qwenpaw.plugins.generations import GenerationRegistry
from qwenpaw.plugins.sdk import DriverHost, DriverProvider, DriverSession
from qwenpaw.runtime.assembly import RuntimeAssemblyFactory
from qwenpaw.runtime.builder import AgentBuilder
from qwenpaw.runtime.driver_providers import (
    ProviderDriverHost,
    validate_driver_session,
)
from qwenpaw.runtime.prompt_contributors import DriverPolicyHintContributor


_VERSIONED_PROVIDER_ID = "contract-driver.versioned"


async def _system_invoke(payload: dict[str, Any]) -> object:
    return payload


class _SystemDriverHost:
    async def load(
        self,
    ) -> tuple[list[DriverToolDefinition], list[PromptFragment]]:
        return [
            DriverToolDefinition(
                provider_id=DEFAULT_DRIVER_PROVIDER_ID,
                capability_id="driver://system/tools/read#invoke",
                name="driver_contract_read",
                invoke=_system_invoke,
            ),
        ], [
            PromptFragment(
                fragment_id=(f"{DEFAULT_DRIVER_PROVIDER_ID}.contract"),
                content="System Driver contract guidance.",
            ),
        ]

    async def require_approval(
        self,
        request: DriverApprovalRequest,
    ) -> None:
        del request

    def config_snapshot(self) -> dict[str, Any]:
        return {}

    def credential(self, alias: str) -> None:
        del alias


def _scope(workspace_dir: Path) -> InvocationScope:
    return InvocationScope(
        agent_id="agent-contract",
        conversation_id="chat-contract",
        session_id="transport-contract",
        root_agent_id="agent-contract",
        root_session_id="transport-contract",
        workspace_dir=str(workspace_dir),
        registry_generation=31,
    )


def _plugin_root() -> Path:
    return (
        Path(__file__).parents[3]
        / "examples"
        / "plugins"
        / "runtime-provider-kit"
    )


class _VersionedDriverSession:
    provider_id = _VERSIONED_PROVIDER_ID

    def __init__(self, version: str) -> None:
        self.version = version

    def list_tools(self) -> tuple[DriverToolDefinition, ...]:
        async def invoke(_payload: dict[str, Any]) -> object:
            return {"version": self.version}

        return (
            DriverToolDefinition(
                provider_id=self.provider_id,
                capability_id="driver://contract/tools/version#invoke",
                name="driver_contract_version",
                invoke=invoke,
            ),
        )

    def prompt_fragments(self) -> tuple[PromptFragment, ...]:
        return (
            PromptFragment(
                fragment_id=f"{self.provider_id}.version",
                content=f"Driver version {self.version}.",
            ),
        )

    async def close(self) -> None:
        return None


class _VersionedDriverProvider:
    provider_id = _VERSIONED_PROVIDER_ID

    def __init__(self, version: str) -> None:
        self.version = version

    async def health_check(self) -> bool:
        return True

    async def open(
        self,
        scope: InvocationScope,
        host: DriverHost,
    ) -> _VersionedDriverSession:
        del scope, host
        return _VersionedDriverSession(self.version)


def _versioned_manifest(version: str) -> PluginManifest:
    return PluginManifest.from_dict(
        {
            "schema_version": "qwenpaw.plugin.v2",
            "id": "contract-driver",
            "name": "Contract Driver",
            "version": version,
            "restart_policy": "hot",
            "contributions": [
                {
                    "id": "versioned",
                    "slot": "driver.provider",
                    "entrypoint": "contract_driver:create_provider",
                },
            ],
        },
    )


def _driver_context(assembly: Any) -> SimpleNamespace:
    return SimpleNamespace(
        extras={"runtime_assembly": assembly},
        invocation_scope=assembly.scope,
        workspace=SimpleNamespace(
            driver_manager=None,
            config=SimpleNamespace(
                capability_configs={},
                capability_credential_refs={},
            ),
        ),
    )


@pytest.mark.asyncio
async def test_system_and_plugin_drivers_share_behavioral_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_root = _plugin_root()
    monkeypatch.syspath_prepend(str(plugin_root))
    driver_module = importlib.import_module("runtime_provider_kit.driver")
    scope = _scope(tmp_path / "contract-project")
    plugin_host = ProviderDriverHost(
        provider_id="runtime-provider-kit.example-driver",
        provider_config={"echo_prefix": "contract:"},
    )
    providers_and_hosts = (
        (WorkspaceDriverProvider(), _SystemDriverHost()),
        (driver_module.create_provider(), plugin_host),
    )

    for provider, host in providers_and_hosts:
        assert isinstance(provider, DriverProvider)
        assert isinstance(host, DriverHost)
        assert await provider.health_check() is True
        session = await provider.open(scope, host)
        assert isinstance(session, DriverSession)
        definitions, fragments = validate_driver_session(
            session,
            provider.provider_id,
        )
        assert definitions
        assert fragments
        await session.close()

    assert not hasattr(plugin_host, "manager")
    assert not hasattr(plugin_host, "request_context")
    assert not hasattr(plugin_host, "load")


@pytest.mark.asyncio
async def test_plugin_driver_flows_through_builder_prompt_and_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_root = _plugin_root()
    monkeypatch.syspath_prepend(str(plugin_root))
    driver_module = importlib.import_module("runtime_provider_kit.driver")
    manifest_data = json.loads(
        (plugin_root / "plugin.json").read_text(encoding="utf-8"),
    )
    manifest_data["contributions"] = [
        item
        for item in manifest_data["contributions"]
        if item["slot"] == "driver.provider"
    ]
    registry = GenerationRegistry()
    await registry.activate(
        PluginManifest.from_dict(manifest_data),
        lambda _declaration: driver_module.create_provider(),
    )
    provider_id = "runtime-provider-kit.example-driver"
    workspace_dir = tmp_path / "contract-project"
    workspace_dir.mkdir()
    assembly = await RuntimeAssemblyFactory(registry).open(
        agent_id="agent-contract",
        conversation_id="chat-contract",
        session_id="transport-contract",
        root_agent_id="agent-contract",
        root_session_id="transport-contract",
        workspace_dir=workspace_dir,
        selection=CapabilitySelection(driver_provider_id=provider_id),
    )
    approvals = ApprovalService()
    interactions = InteractionService(tmp_path / "interactions.sqlite3")
    monkeypatch.setattr(
        approvals_package,
        "get_approval_service",
        lambda: approvals,
    )
    request_context = {
        "agent_id": "agent-contract",
        "session_id": "transport-contract",
        "root_agent_id": "agent-contract",
        "root_session_id": "transport-contract",
        "user_id": "local-user",
        "channel": "console",
        "os_conversation_id": "chat-contract",
        "os_invocation_id": str(assembly.scope.invocation_id),
        "_interaction_service": interactions,
    }
    context = SimpleNamespace(
        extras={"runtime_assembly": assembly},
        invocation_scope=assembly.scope,
        workspace=SimpleNamespace(
            driver_manager=None,
            config=SimpleNamespace(
                capability_configs={provider_id: {"echo_prefix": "safe:"}},
                capability_credential_refs={},
            ),
        ),
    )
    session = None

    try:
        # pylint: disable=protected-access
        session = await AgentBuilder()._open_driver_session(
            context,
            request_context,
        )
        # pylint: enable=protected-access
        context.extras["driver_session"] = session
        definitions, fragments = validate_driver_session(
            session,
            provider_id,
        )
        context.extras["driver_prompt_hints"] = [
            fragment.content for fragment in fragments
        ]
        assert DriverPolicyHintContributor().contribute_sync(context) == (
            "The example Driver evaluates policy before invoking "
            "driver_echo."
        )

        tool = adapt_driver_definitions(list(definitions))[0]
        invocation = asyncio.create_task(tool(text="approved"))
        opened = ()
        for _ in range(30):
            opened = await interactions.list_open(
                agent_id="agent-contract",
                conversation_id="chat-contract",
            )
            if opened:
                break
            await asyncio.sleep(0)
        assert len(opened) == 1
        assert opened[0].metadata["source"] == "driver_policy"
        await interactions.resolve(
            InteractionResponse(
                interaction_id=opened[0].interaction_id,
                idempotency_key="approve-driver-contract",
                expected_revision=opened[0].revision,
                actor=ActorRef(type=ActorType.USER, id="local-user"),
                selected_option_ids=("approve_exact",),
            ),
        )
        result = await invocation
        assert result.content[0].text == (
            '{\n  "echo": "safe:approved",\n'
            '  "credential_configured": false\n}'
        )
        assert assembly.scope.registry_generation == registry.generation
    finally:
        if session is not None:
            await session.close()
        await assembly.close()


@pytest.mark.asyncio
async def test_driver_hot_replacement_keeps_open_session_behavior(
    tmp_path: Path,
) -> None:
    registry = GenerationRegistry()
    await registry.activate(
        _versioned_manifest("1.0.0"),
        lambda _declaration: _VersionedDriverProvider("1.0.0"),
    )
    factory = RuntimeAssemblyFactory(registry)
    selection = CapabilitySelection(
        driver_provider_id=_VERSIONED_PROVIDER_ID,
    )
    old_assembly = await factory.open(
        agent_id="agent-contract",
        conversation_id="chat-old",
        session_id="transport-old",
        root_agent_id="agent-contract",
        root_session_id="transport-old",
        workspace_dir=tmp_path,
        selection=selection,
    )
    builder = AgentBuilder()
    # pylint: disable=protected-access
    old_session = await builder._open_driver_session(
        _driver_context(old_assembly),
        {"agent_id": "agent-contract"},
    )

    await registry.activate(
        _versioned_manifest("2.0.0"),
        lambda _declaration: _VersionedDriverProvider("2.0.0"),
    )
    new_assembly = await factory.open(
        agent_id="agent-contract",
        conversation_id="chat-new",
        session_id="transport-new",
        root_agent_id="agent-contract",
        root_session_id="transport-new",
        workspace_dir=tmp_path,
        selection=selection,
    )
    new_session = await builder._open_driver_session(
        _driver_context(new_assembly),
        {"agent_id": "agent-contract"},
    )
    # pylint: enable=protected-access

    try:
        old_definitions, _ = validate_driver_session(
            old_session,
            _VERSIONED_PROVIDER_ID,
        )
        new_definitions, _ = validate_driver_session(
            new_session,
            _VERSIONED_PROVIDER_ID,
        )
        old_result = await old_definitions[0].invoke({})
        new_result = await new_definitions[0].invoke({})
        assert old_result == {"version": "1.0.0"}
        assert new_result == {"version": "2.0.0"}
        assert old_assembly.scope.registry_generation < (
            new_assembly.scope.registry_generation
        )
    finally:
        await old_session.close()
        await new_session.close()
        await old_assembly.close()
        await new_assembly.close()
