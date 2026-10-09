# -*- coding: utf-8 -*-
"""Tests for Chat-first runtime assembly."""

# pylint: disable=protected-access

from types import SimpleNamespace
from uuid import UUID

import pytest

from qwenpaw.capabilities import GenerationRegistry
from qwenpaw.kernel import CapabilityLockManifest
from qwenpaw.kernel.invocation import (
    CapabilitySelection,
    CapabilitySelectionOverrides,
    DEFAULT_AGENT_FACTORY_ID,
    DEFAULT_AGENT_MODE_PROVIDER_ID,
    DEFAULT_COMMAND_PROVIDER_ID,
    DEFAULT_DRIVER_PROVIDER_ID,
    DEFAULT_HOOK_PROVIDER_ID,
    DEFAULT_STOP_GATE_PROVIDER_ID,
    DEFAULT_MEMORY_PROVIDER_ID,
    DEFAULT_PROMPT_PROVIDER_ID,
    DEFAULT_TOOL_PROVIDER_ID,
    InvocationScope,
)
from qwenpaw.kernel.models import (
    CapabilityBundle,
    CapabilityContribution,
    CapabilityProviderKind,
    EnvironmentContract,
    EnvironmentMount,
    EnvironmentMountAccess,
    EnvironmentNetworkMode,
    EnvironmentResolutionStatus,
)
from qwenpaw.runtime.assembly import (
    CapabilityUnavailableError,
    RuntimeAssemblyFactory,
    capability_registry_for,
)
from qwenpaw.runtime.capability_locks import (
    FilesystemCapabilityLockStore,
)
from qwenpaw.runtime.builder import AgentBuilder
from qwenpaw.runtime.environments import (
    EnvironmentContractUnsatisfiedError,
)
from qwenpaw.runtime.runtime import Runtime


def test_capability_registry_for_returns_host_owned_registry() -> None:
    registry = GenerationRegistry()
    workspace = SimpleNamespace(capability_registry=registry)

    assert capability_registry_for(workspace) is registry


@pytest.mark.parametrize(
    "workspace",
    [
        SimpleNamespace(),
        SimpleNamespace(capability_registry=object()),
    ],
)
def test_capability_registry_for_fails_closed_without_shared_registry(
    workspace: SimpleNamespace,
) -> None:
    with pytest.raises(
        CapabilityUnavailableError,
        match="no shared capability registry",
    ):
        capability_registry_for(workspace)

    assert not isinstance(
        getattr(workspace, "capability_registry", None),
        GenerationRegistry,
    )


@pytest.mark.asyncio
async def test_open_resolves_system_agent_factory_without_task_ids(
    tmp_path,
) -> None:
    registry = GenerationRegistry()
    assembly = await RuntimeAssemblyFactory(registry).open(
        agent_id="default",
        session_id="chat-session",
        root_agent_id="default",
        root_session_id="chat-session",
        workspace_dir=tmp_path,
    )

    factory = assembly.require(DEFAULT_AGENT_FACTORY_ID, "agent.factory")
    tool_provider = assembly.require(
        DEFAULT_TOOL_PROVIDER_ID,
        "tool.provider",
    )
    memory_provider = assembly.require(
        DEFAULT_MEMORY_PROVIDER_ID,
        "memory.provider",
    )
    mode_provider = assembly.require(
        DEFAULT_AGENT_MODE_PROVIDER_ID,
        "agent.mode.provider",
    )
    prompt_provider = assembly.require(
        DEFAULT_PROMPT_PROVIDER_ID,
        "prompt.provider",
    )
    driver_provider = assembly.require(
        DEFAULT_DRIVER_PROVIDER_ID,
        "driver.provider",
    )
    command_provider = assembly.require(
        DEFAULT_COMMAND_PROVIDER_ID,
        "command.provider",
    )
    hook_provider = assembly.require(
        DEFAULT_HOOK_PROVIDER_ID,
        "hook.provider",
    )
    stop_gate_provider = assembly.require(
        DEFAULT_STOP_GATE_PROVIDER_ID,
        "loop.gate.provider",
    )

    assert factory.factory_id == DEFAULT_AGENT_FACTORY_ID
    assert tool_provider.provider_id == DEFAULT_TOOL_PROVIDER_ID
    assert memory_provider.provider_id == DEFAULT_MEMORY_PROVIDER_ID
    assert mode_provider.provider_id == DEFAULT_AGENT_MODE_PROVIDER_ID
    assert prompt_provider.provider_id == DEFAULT_PROMPT_PROVIDER_ID
    assert driver_provider.provider_id == DEFAULT_DRIVER_PROVIDER_ID
    assert command_provider.provider_id == DEFAULT_COMMAND_PROVIDER_ID
    assert hook_provider.provider_id == DEFAULT_HOOK_PROVIDER_ID
    assert stop_gate_provider.provider_id == DEFAULT_STOP_GATE_PROVIDER_ID
    assert assembly.scope.registry_generation == 10
    assert assembly.scope.registry_epoch_id == registry.registry_epoch_id
    assert assembly.scope.capability_lock_id is not None
    assert assembly.scope.capability_lock_hash is not None
    assert assembly.scope.environment_contract is not None
    assert assembly.scope.environment_resolution is not None
    assert (
        assembly.scope.environment_resolution.status
        is EnvironmentResolutionStatus.SATISFIED
    )
    assert not hasattr(assembly.scope, "task_id")
    [lock_path] = list(
        (tmp_path / ".qwenpaw" / "lite" / "capability-locks").glob(
            "*/*/lock.json",
        ),
    )
    lock = CapabilityLockManifest.model_validate_json(
        lock_path.read_text(encoding="utf-8"),
    )
    assert lock.lock_id == assembly.scope.capability_lock_id
    assert lock.manifest_hash == assembly.scope.capability_lock_hash
    assert lock.registry_epoch_id == registry.registry_epoch_id
    assert tuple(item.capability_id for item in lock.releases) == (
        assembly.scope.capability_ids
    )
    assert lock_path.stat().st_mode & 0o777 == 0o600
    await assembly.close()


@pytest.mark.asyncio
async def test_environment_failure_does_not_create_missing_workspace(
    tmp_path,
) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(
        EnvironmentContractUnsatisfiedError,
        match="environment.workspace.missing",
    ):
        await RuntimeAssemblyFactory(GenerationRegistry()).open(
            agent_id="default",
            session_id="missing-workspace",
            root_agent_id="default",
            root_session_id="missing-workspace",
            workspace_dir=missing,
        )

    assert not missing.exists()


@pytest.mark.asyncio
async def test_environment_failure_is_durable_and_releases_generation(
    tmp_path,
) -> None:
    registry = GenerationRegistry()
    factory = RuntimeAssemblyFactory(registry)
    await factory.prepare()
    rejected_generation = registry.generation
    contract = EnvironmentContract(
        contract_id="example.environment.no-network",
        workspace=EnvironmentMount(
            source=str(tmp_path),
            target="workspace",
            access=EnvironmentMountAccess.READ_WRITE,
        ),
        network_mode=EnvironmentNetworkMode.DENY,
    )

    with pytest.raises(
        EnvironmentContractUnsatisfiedError,
        match="environment.network.unsupported",
    ):
        await factory.open(
            agent_id="default",
            conversation_id="chat-environment-failure",
            session_id="environment-failure",
            root_agent_id="default",
            root_session_id="environment-failure",
            workspace_dir=tmp_path,
            environment_contract=contract,
        )

    records = list(
        (tmp_path / ".qwenpaw" / "lite" / "environments").glob(
            "*/*/environment.json",
        ),
    )
    assert len(records) == 1
    await registry.activate_bundle(
        _plugin_tool_bundle("1.0.0"),
        lambda _: _PluginToolProvider("1.0.0"),
    )
    with pytest.raises(LookupError, match=str(rejected_generation)):
        await registry.pin(rejected_generation)


@pytest.mark.asyncio
async def test_open_reuses_the_published_system_bundle(tmp_path) -> None:
    registry = GenerationRegistry()
    first = await RuntimeAssemblyFactory(registry).open(
        agent_id="default",
        session_id="one",
        root_agent_id="default",
        root_session_id="one",
        workspace_dir=tmp_path,
    )
    second = await RuntimeAssemblyFactory(registry).open(
        agent_id="default",
        session_id="two",
        root_agent_id="default",
        root_session_id="two",
        workspace_dir=tmp_path,
    )

    assert first.scope.registry_generation == 10
    assert second.scope.registry_generation == 10
    await first.close()
    await second.close()


class _PluginToolProvider:
    provider_id = "example.tools.provider"

    def __init__(self, version: str) -> None:
        self.version = version

    async def health_check(self) -> bool:
        return True

    async def list_tools(self, scope, selection, host):
        del scope, selection, host
        return ()


class _PluginDriverProvider:
    provider_id = "example.drivers.provider"

    async def health_check(self) -> bool:
        return True

    async def open(self, scope, host):
        return SimpleNamespace(scope=scope, host=host)


def _plugin_tool_bundle(version: str) -> CapabilityBundle:
    return CapabilityBundle(
        provider_id="example.tools",
        provider_kind=CapabilityProviderKind.PLUGIN,
        version=version,
        contributions=(
            CapabilityContribution(
                contribution_id="provider",
                slot="tool.provider",
                entrypoint="example:provider",
            ),
        ),
    )


def _plugin_driver_bundle() -> CapabilityBundle:
    return CapabilityBundle(
        provider_id="example.drivers",
        provider_kind=CapabilityProviderKind.PLUGIN,
        version="1.0.0",
        contributions=(
            CapabilityContribution(
                contribution_id="provider",
                slot="driver.provider",
                entrypoint="example:driver_provider",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_plugin_tool_provider_replacement_keeps_old_invocation(
    tmp_path,
) -> None:
    registry = GenerationRegistry()
    await registry.activate_bundle(
        _plugin_tool_bundle("1.0.0"),
        lambda _: _PluginToolProvider("1.0.0"),
    )
    selection = CapabilitySelection(
        tool_provider_ids=("example.tools.provider",),
    )
    old = await RuntimeAssemblyFactory(registry).open(
        agent_id="default",
        conversation_id="chat-lock",
        session_id="old",
        root_agent_id="default",
        root_session_id="old",
        workspace_dir=tmp_path,
        selection=selection,
    )

    await registry.activate_bundle(
        _plugin_tool_bundle("2.0.0"),
        lambda _: _PluginToolProvider("2.0.0"),
    )
    new = await RuntimeAssemblyFactory(registry).open(
        agent_id="default",
        conversation_id="chat-lock",
        session_id="new",
        root_agent_id="default",
        root_session_id="new",
        workspace_dir=tmp_path,
        selection=selection,
    )
    resumed = await RuntimeAssemblyFactory(registry).open(
        agent_id="default",
        conversation_id="chat-lock",
        session_id="resumed",
        root_agent_id="default",
        root_session_id="resumed",
        workspace_dir=tmp_path,
        selection=selection,
        registry_generation=old.scope.registry_generation,
    )

    assert old.require("example.tools.provider", "tool.provider").version == (
        "1.0.0"
    )
    assert new.require("example.tools.provider", "tool.provider").version == (
        "2.0.0"
    )
    assert (
        resumed.require(
            "example.tools.provider",
            "tool.provider",
        ).version
        == "1.0.0"
    )
    locks = await FilesystemCapabilityLockStore(
        tmp_path,
    ).list_for_conversation("chat-lock")
    by_invocation = {item.invocation_id: item for item in locks}
    old_lock = by_invocation[old.scope.invocation_id]
    new_lock = by_invocation[new.scope.invocation_id]
    resumed_lock = by_invocation[resumed.scope.invocation_id]

    def plugin_release(lock):
        return next(
            item
            for item in lock.releases
            if item.capability_id == "example.tools.provider"
        )

    assert plugin_release(old_lock).version == "1.0.0"
    assert plugin_release(new_lock).version == "2.0.0"
    assert plugin_release(resumed_lock).version == "1.0.0"
    assert plugin_release(old_lock).descriptor_hash != (
        plugin_release(new_lock).descriptor_hash
    )
    assert old.scope.capability_lock_hash == old_lock.manifest_hash
    old_generation = old.scope.registry_generation
    await old.close()
    await resumed.close()
    await new.close()

    with pytest.raises(LookupError, match=str(old_generation)):
        await RuntimeAssemblyFactory(registry).open(
            agent_id="default",
            session_id="expired",
            root_agent_id="default",
            root_session_id="expired",
            workspace_dir=tmp_path,
            selection=selection,
            registry_generation=old_generation,
        )


@pytest.mark.asyncio
async def test_new_invocation_auto_selects_installed_tool_provider(
    tmp_path,
) -> None:
    registry = GenerationRegistry()
    await registry.activate_bundle(
        _plugin_tool_bundle("1.0.0"),
        lambda _: _PluginToolProvider("1.0.0"),
    )

    assembly = await RuntimeAssemblyFactory(registry).open(
        agent_id="default",
        conversation_id="chat-auto-plugin",
        session_id="chat",
        root_agent_id="default",
        root_session_id="chat",
        workspace_dir=tmp_path,
    )

    assert assembly.scope.selection.tool_provider_ids == (
        "example.tools.provider",
        DEFAULT_TOOL_PROVIDER_ID,
    )
    [lock] = await FilesystemCapabilityLockStore(
        tmp_path,
    ).list_for_conversation("chat-auto-plugin")
    provider_kinds = {
        item.provider_kind for item in lock.releases
    }
    assert provider_kinds == {
        CapabilityProviderKind.SYSTEM,
        CapabilityProviderKind.PLUGIN,
    }
    await assembly.close()


@pytest.mark.asyncio
async def test_driver_override_preserves_auto_slots(tmp_path) -> None:
    registry = GenerationRegistry()
    await registry.activate_bundle(
        _plugin_tool_bundle("1.0.0"),
        lambda _: _PluginToolProvider("1.0.0"),
    )
    await registry.activate_bundle(
        _plugin_driver_bundle(),
        lambda _: _PluginDriverProvider(),
    )

    assembly = await RuntimeAssemblyFactory(registry).open(
        agent_id="default",
        session_id="chat",
        root_agent_id="default",
        root_session_id="chat",
        workspace_dir=tmp_path,
        selection_overrides=CapabilitySelectionOverrides(
            driver_provider_id="example.drivers.provider",
        ),
    )

    assert assembly.scope.selection.driver_provider_id == (
        "example.drivers.provider"
    )
    assert assembly.scope.selection.tool_provider_ids == (
        "example.tools.provider",
        DEFAULT_TOOL_PROVIDER_ID,
    )
    assert (
        assembly.require(
            "example.drivers.provider",
            "driver.provider",
        ).provider_id
        == "example.drivers.provider"
    )
    await assembly.close()


@pytest.mark.asyncio
async def test_disabled_optional_slot_removes_scalar_provider(
    tmp_path,
) -> None:
    overrides = CapabilitySelectionOverrides(
        disabled_optional_slots=("driver.provider",),
    )

    assembly = await RuntimeAssemblyFactory(GenerationRegistry()).open(
        agent_id="default",
        session_id="chat",
        root_agent_id="default",
        root_session_id="chat",
        workspace_dir=tmp_path,
        selection_overrides=overrides,
    )

    assert assembly.scope.selection.driver_provider_id is None
    await assembly.close()


@pytest.mark.asyncio
async def test_missing_driver_override_releases_generation(tmp_path) -> None:
    registry = GenerationRegistry()
    factory = RuntimeAssemblyFactory(registry)
    await factory.prepare()
    rejected_generation = registry.generation

    with pytest.raises(CapabilityUnavailableError, match="missing.driver"):
        await factory.open(
            agent_id="default",
            session_id="invalid",
            root_agent_id="default",
            root_session_id="invalid",
            workspace_dir=tmp_path,
            selection_overrides=CapabilitySelectionOverrides(
                driver_provider_id="missing.driver",
            ),
        )

    await registry.activate_bundle(
        _plugin_driver_bundle(),
        lambda _: _PluginDriverProvider(),
    )
    with pytest.raises(LookupError, match=str(rejected_generation)):
        await registry.pin(rejected_generation)


@pytest.mark.asyncio
async def test_null_required_provider_keeps_automatic_selection(
    tmp_path,
) -> None:
    assembly = await RuntimeAssemblyFactory(GenerationRegistry()).open(
        agent_id="default",
        session_id="chat",
        root_agent_id="default",
        root_session_id="chat",
        workspace_dir=tmp_path,
        selection_overrides=CapabilitySelectionOverrides.model_validate(
            {"agent_factory_id": None},
        ),
    )

    assert assembly.scope.selection.agent_factory_id == (
        DEFAULT_AGENT_FACTORY_ID
    )
    await assembly.close()


def test_profile_override_rejects_selected_and_disabled_slot() -> None:
    with pytest.raises(ValueError, match="selected and disabled"):
        CapabilitySelectionOverrides(
            driver_provider_id="example.drivers.provider",
            disabled_optional_slots=("driver.provider",),
        )


@pytest.mark.asyncio
async def test_explicit_selection_and_profile_override_are_exclusive(
    tmp_path,
) -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        await RuntimeAssemblyFactory(GenerationRegistry()).open(
            agent_id="default",
            session_id="invalid",
            root_agent_id="default",
            root_session_id="invalid",
            workspace_dir=tmp_path,
            selection=CapabilitySelection(),
            selection_overrides=CapabilitySelectionOverrides(),
        )


@pytest.mark.asyncio
async def test_selection_fails_closed_and_releases_its_generation(
    tmp_path,
) -> None:
    registry = GenerationRegistry()
    factory = RuntimeAssemblyFactory(registry)
    await factory.prepare()
    rejected_generation = registry.generation

    with pytest.raises(CapabilityUnavailableError, match="missing.provider"):
        await factory.open(
            agent_id="default",
            session_id="invalid",
            root_agent_id="default",
            root_session_id="invalid",
            workspace_dir=tmp_path,
            selection=CapabilitySelection(
                tool_provider_ids=("missing.provider",),
            ),
        )

    await registry.activate_bundle(
        _plugin_tool_bundle("1.0.0"),
        lambda _: _PluginToolProvider("1.0.0"),
    )
    with pytest.raises(LookupError, match=str(rejected_generation)):
        await registry.pin(rejected_generation)


@pytest.mark.asyncio
async def test_selection_rejects_capability_from_the_wrong_slot(
    tmp_path,
) -> None:
    registry = GenerationRegistry()

    with pytest.raises(CapabilityUnavailableError, match="expected"):
        await RuntimeAssemblyFactory(registry).open(
            agent_id="default",
            session_id="wrong-slot",
            root_agent_id="default",
            root_session_id="wrong-slot",
            workspace_dir=tmp_path,
            selection=CapabilitySelection(
                agent_factory_id=DEFAULT_TOOL_PROVIDER_ID,
            ),
        )


def test_task_generation_hint_requires_internal_broker() -> None:
    internal = type(
        "Request",
        (),
        {
            "request_context": {
                "durable_task": True,
                "_task_approval_broker": object(),
                "os_registry_generation": 12,
            },
        },
    )()
    spoofed = type(
        "Request",
        (),
        {
            "request_context": {
                "durable_task": True,
                "os_registry_generation": 12,
            },
        },
    )()

    assert Runtime._task_registry_generation(internal) == 12
    assert Runtime._task_registry_generation(spoofed) is None


def test_task_causal_identity_requires_internal_broker() -> None:
    invocation_id = UUID("00000000-0000-0000-0000-000000000311")
    correlation_id = UUID("00000000-0000-0000-0000-000000000312")
    internal = SimpleNamespace(
        request_context={
            "durable_task": True,
            "_task_approval_broker": object(),
            "os_invocation_id": str(invocation_id),
            "os_correlation_id": str(correlation_id),
        },
    )
    spoofed = SimpleNamespace(
        request_context={
            "durable_task": True,
            "os_invocation_id": str(invocation_id),
            "os_correlation_id": str(correlation_id),
        },
    )

    assert Runtime._task_causal_identity(internal) == (
        invocation_id,
        correlation_id,
    )
    assert Runtime._task_causal_identity(spoofed) == (None, None)


def test_task_causal_identity_rejects_malformed_internal_bridge() -> None:
    request = SimpleNamespace(
        request_context={
            "durable_task": True,
            "_task_approval_broker": object(),
            "os_invocation_id": "invalid",
            "os_correlation_id": "invalid",
        },
    )

    with pytest.raises(ValueError, match="invalid causal identity"):
        Runtime._task_causal_identity(request)


@pytest.mark.asyncio
async def test_open_preserves_task_causal_identity(tmp_path) -> None:
    invocation_id = UUID("00000000-0000-0000-0000-000000000313")
    correlation_id = UUID("00000000-0000-0000-0000-000000000314")
    assembly = await RuntimeAssemblyFactory(GenerationRegistry()).open(
        agent_id="default",
        session_id="task-session",
        root_agent_id="default",
        root_session_id="task-session",
        workspace_dir=tmp_path,
        invocation_id=invocation_id,
        correlation_id=correlation_id,
    )

    assert assembly.scope.invocation_id == invocation_id
    assert assembly.scope.correlation_id == correlation_id
    await assembly.close()


def test_runtime_overwrites_spoofed_causal_identity() -> None:
    invocation_id = UUID("00000000-0000-0000-0000-000000000321")
    scope = InvocationScope(
        invocation_id=invocation_id,
        agent_id="default",
        conversation_id="chat-spec-1",
        session_id="chat",
        root_agent_id="default",
        root_session_id="chat",
        workspace_dir="/tmp/qwenpaw-workspace",
        registry_generation=1,
    )
    context = SimpleNamespace(
        request=SimpleNamespace(
            request_context={
                "os_invocation_id": "spoofed",
                "os_correlation_id": "spoofed",
            },
        ),
        invocation_scope=scope,
        session_id="chat",
        agent_id="default",
        root_session_id="chat",
        root_agent_id="default",
        workspace_dir="/tmp/qwenpaw-workspace",
        mode_state={},
    )

    request_context = AgentBuilder._build_request_context(context)

    assert request_context["os_invocation_id"] == str(invocation_id)
    assert request_context["os_correlation_id"] == str(invocation_id)


def test_runtime_injects_trusted_interaction_broker() -> None:
    invocation_id = UUID("00000000-0000-0000-0000-000000000322")
    service = object()
    compatibility = object()
    scope = InvocationScope(
        invocation_id=invocation_id,
        agent_id="default",
        conversation_id="chat-spec-1",
        session_id="chat",
        root_agent_id="default",
        root_session_id="chat",
        workspace_dir="/tmp/qwenpaw-workspace",
        registry_generation=1,
    )
    context = SimpleNamespace(
        request=SimpleNamespace(
            request_context={
                "os_conversation_id": "forged-chat",
                "_interaction_service": "forged",
                "_interaction_broker": "forged",
                "_legacy_approval_compatibility": "forged",
                "_steering_session": "forged",
            },
        ),
        invocation_scope=scope,
        session_id="chat",
        agent_id="default",
        root_session_id="chat",
        root_agent_id="default",
        workspace_dir="/tmp/qwenpaw-workspace",
        mode_state={},
        extras={
            "interaction_service": service,
            "legacy_approval_compatibility": compatibility,
        },
    )

    request_context = AgentBuilder._build_request_context(context)

    broker = request_context["_interaction_broker"]
    assert request_context["_interaction_service"] is service
    assert request_context["_legacy_approval_compatibility"] is compatibility
    assert broker.service is service
    assert broker.conversation_id == "chat-spec-1"
    assert broker.invocation_id == invocation_id
    assert "_steering_session" not in request_context


def test_runtime_does_not_promote_session_to_conversation() -> None:
    scope = InvocationScope(
        agent_id="default",
        session_id="transport-session",
        root_agent_id="default",
        root_session_id="transport-session",
        workspace_dir="/tmp/qwenpaw-workspace",
        registry_generation=1,
    )
    context = SimpleNamespace(
        request=SimpleNamespace(
            request_context={"os_conversation_id": "forged-chat"},
        ),
        invocation_scope=scope,
        session_id="transport-session",
        agent_id="default",
        root_session_id="transport-session",
        root_agent_id="default",
        workspace_dir="/tmp/qwenpaw-workspace",
        mode_state={},
        extras={},
    )

    request_context = AgentBuilder._build_request_context(context)

    assert scope.conversation_id is None
    assert "os_conversation_id" not in request_context
