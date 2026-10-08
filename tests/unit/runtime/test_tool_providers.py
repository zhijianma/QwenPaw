# -*- coding: utf-8 -*-
"""Tests for the task-independent workspace Tool Provider adapter."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from qwenpaw.capabilities import GenerationRegistry
from qwenpaw.capabilities.system_tools import WorkspaceToolProvider
from qwenpaw.drivers.credentials.types import CredentialRecord
from qwenpaw.kernel.invocation import CapabilitySelection, InvocationScope
from qwenpaw.kernel.models import (
    ActionKind,
    CapabilityBundle,
    CapabilityContribution,
    CapabilityProviderKind,
    ToolDefinition,
    ToolEffect,
    ToolSelection,
)
from qwenpaw.runtime.assembly import RuntimeAssemblyFactory
from qwenpaw.runtime.actions import (
    FilesystemActionStore,
    RuntimeActionRecorder,
)
from qwenpaw.runtime.tool_providers import (
    ProviderToolHost,
    WorkspaceToolHost,
    tool_selection_from_request,
)
from qwenpaw.runtime.builder import AgentBuilder
from qwenpaw.runtime.provider_config import provider_execution_digest


def _sample_tool() -> None:
    """Provide one named callable for adapter assertions."""


class _LocalWorkspace:
    def __init__(self) -> None:
        self.governor: object | None = None
        self.kwargs: dict[str, Any] = {}

    def set_governor(self, governor: object) -> None:
        self.governor = governor

    async def list_tools(self, **kwargs: Any) -> list[Any]:
        self.kwargs = kwargs
        return [_sample_tool]


def _scope() -> InvocationScope:
    return InvocationScope(
        agent_id="default",
        session_id="chat",
        root_agent_id="default",
        root_session_id="chat",
        workspace_dir="/tmp/qwenpaw-workspace",
        registry_generation=3,
    )


@pytest.mark.asyncio
async def test_workspace_provider_uses_governed_invocation_host() -> None:
    local_workspace = _LocalWorkspace()
    governor = object()
    host = WorkspaceToolHost(
        local_workspace=local_workspace,
        agent_config=SimpleNamespace(),
        request_context={"source": "console"},
        governor=governor,
    )
    selection = ToolSelection(
        active_modes=("coding",),
        active_skills=("repo-reader",),
        enabled_features=("browser",),
    )

    tools = await WorkspaceToolProvider().list_tools(
        _scope(),
        selection,
        host,
    )

    assert tools == [_sample_tool]
    assert local_workspace.governor is governor
    assert local_workspace.kwargs["active_modes"] == {"coding"}
    assert local_workspace.kwargs["active_skills"] == {"repo-reader"}
    assert local_workspace.kwargs["enabled_features"] == {"browser"}


def test_tool_selection_preserves_deny_all_subagent_whitelist() -> None:
    selection = tool_selection_from_request(
        active_modes=["goal"],
        active_skills=[],
        enabled_features=[],
        request_context={"subagent_allowed_tools": []},
    )

    assert selection.active_modes == ("goal",)
    assert not selection.subagent_allowed_tools


class _RawToolProvider:
    provider_id = "example.raw-tools"

    async def list_tools(
        self,
        scope: InvocationScope,
        selection: ToolSelection,
        host: Any,
    ) -> list[Any]:
        del scope, selection, host
        return [_sample_tool]


@pytest.mark.asyncio
async def test_builder_guard_wraps_raw_provider_tools() -> None:
    context = SimpleNamespace(invocation_scope=_scope())
    # Exercise the provider boundary directly without assembling an agent.
    # pylint: disable=protected-access
    tools = await AgentBuilder()._collect_provider_tools(
        tool_providers=(_RawToolProvider(),),
        ctx=context,
        local_workspace=None,
        agent_config=SimpleNamespace(),
        request_context={"agent_id": "default"},
        governor=None,
        active_modes=(),
        active_skills=(),
        enabled_features=(),
    )
    # pylint: enable=protected-access

    assert len(tools) == 1
    assert callable(getattr(tools[0], "check_permissions", None))
    request_context = getattr(tools[0], "_qp_request_context")
    assert request_context["agent_id"] == "default"


def test_tool_host_exposes_interaction_broker_without_private_key() -> None:
    from qwenpaw.interactions import RuntimeInteractionBroker

    service = SimpleNamespace()
    broker = RuntimeInteractionBroker(
        service=service,
        agent_id="default",
        conversation_id="chat-spec-1",
        invocation_id=_scope().invocation_id,
    )
    host = WorkspaceToolHost(
        local_workspace=None,
        agent_config=SimpleNamespace(),
        request_context={"_interaction_broker": broker},
        governor=None,
    )

    assert host.interaction_broker() is broker


@pytest.mark.asyncio
async def test_workspace_provider_adds_real_ask_user_tool() -> None:
    interaction_id = _scope().invocation_id

    class _Broker:
        def __init__(self) -> None:
            self.has_deferred_user_input = False
            self.ask_user = AsyncMock()
            self.defer_user_input = AsyncMock(
                return_value=SimpleNamespace(
                    interaction_id=interaction_id,
                ),
            )
            self.suggest = AsyncMock(return_value=SimpleNamespace())

    broker = _Broker()
    host = WorkspaceToolHost(
        local_workspace=None,
        agent_config=SimpleNamespace(),
        request_context={"_interaction_broker": broker},
        governor=None,
    )

    tools = await WorkspaceToolProvider().list_tools(
        _scope(),
        ToolSelection(),
        host,
    )
    defined = {
        tool.name: tool for tool in tools if isinstance(tool, ToolDefinition)
    }
    ask_user = defined["ask_user"]
    answer = await ask_user.function(
        question="Which format?",
        reason="material_preference",
        choices=["Markdown", "HTML"],
    )

    assert "End this turn now" in answer
    assert str(interaction_id) in answer
    broker.defer_user_input.assert_awaited_once()
    request = broker.defer_user_input.await_args.kwargs
    assert request["prompt"] == "Which format?"
    assert request["reason"].value == "material_preference"
    assert [option.label for option in request["options"]] == [
        "Markdown",
        "HTML",
    ]

    result = await defined["suggest_user_action"].function(
        suggestion="Add a regression test.",
        actions=["Create test", "Open affected file"],
    )
    assert result == "Suggestion delivered without pausing the conversation."
    broker.suggest.assert_awaited_once()
    suggestion = broker.suggest.await_args.kwargs
    assert suggestion["prompt"] == "Add a regression test."
    assert [option.label for option in suggestion["options"]] == [
        "Create test",
        "Open affected file",
    ]


def _defined_provider_tool() -> None:
    """Provide a governed tool through the stable definition contract."""


class _DefinedToolProvider:
    provider_id = "example.defined-tools"

    async def list_tools(
        self,
        scope: InvocationScope,
        selection: ToolSelection,
        host: object,
    ) -> list[ToolDefinition]:
        del scope, selection, host
        return [
            ToolDefinition(
                function=_defined_provider_tool,
                name="_defined_provider_tool",
                tool_type="internal",
                action_kind=ActionKind.BROWSER,
            ),
        ]


class _CredentialStore:
    async def get(self, ref: str) -> CredentialRecord:
        if ref != "credential:tool-service":
            raise KeyError(ref)
        return CredentialRecord(
            ref=ref,
            kind="static",
            public={"account": "tool-user"},
            secrets={"token": "tool-secret"},
        )


class _ConfiguredToolProvider:
    provider_id = "example.config-tools.provider"

    def __init__(self) -> None:
        self.host: Any = None
        self.selection: ToolSelection | None = None

    async def health_check(self) -> bool:
        return True

    async def list_tools(
        self,
        scope: InvocationScope,
        selection: ToolSelection,
        host: Any,
    ) -> list[ToolDefinition]:
        del scope
        self.host = host
        self.selection = selection

        async def configured_tool() -> str:
            return str(host.config_snapshot().get("label", ""))

        return [
            ToolDefinition(
                function=configured_tool,
                name="configured_tool",
                tool_type="internal",
            ),
        ]


def _configured_tool_bundle() -> CapabilityBundle:
    return CapabilityBundle(
        provider_id="example.config-tools",
        provider_kind=CapabilityProviderKind.PLUGIN,
        version="1.0.0",
        contributions=(
            CapabilityContribution(
                contribution_id="provider",
                slot="tool.provider",
                entrypoint="example:configured_tools",
                config_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"label": {"type": "string"}},
                },
            ),
        ),
    )


@pytest.mark.asyncio
async def test_resolve_governed_action_tool_uses_exact_selection(
    tmp_path: Path,
) -> None:
    registry = GenerationRegistry()
    factory = RuntimeAssemblyFactory(registry)
    await factory.prepare()
    provider = _ConfiguredToolProvider()
    await registry.activate_bundle(
        _configured_tool_bundle(),
        lambda _: provider,
    )
    assembly = await factory.open(
        agent_id="default",
        conversation_id="chat-action-retry",
        session_id="chat-action-retry",
        root_agent_id="default",
        root_session_id="chat-action-retry",
        workspace_dir=tmp_path,
        selection=CapabilitySelection(
            tool_provider_ids=(provider.provider_id,),
        ),
        registry_generation=registry.generation,
    )
    recorder = RuntimeActionRecorder(
        assembly.scope,
        FilesystemActionStore(tmp_path),
    )
    request_context = {"_action_recorder": recorder}
    context = SimpleNamespace(
        invocation_scope=assembly.scope,
        extras={"runtime_assembly": assembly},
        workspace=SimpleNamespace(
            config=SimpleNamespace(
                capability_configs={provider.provider_id: {}},
                capability_credential_refs={},
            ),
            driver_manager=None,
            local_workspace=None,
        ),
    )
    selection = ToolSelection(
        active_modes=("coding",),
        active_skills=("review",),
        enabled_features=("example.retry",),
        explicit_enabled=("configured_tool",),
        explicit_disabled=("other_tool",),
        subagent_allowed_tools=("configured_tool",),
    )

    tool = await AgentBuilder().resolve_governed_action_tool(
        ctx=context,
        agent_config=SimpleNamespace(),
        request_context=request_context,
        provider_id=provider.provider_id,
        tool_name="configured_tool",
        tool_selection=selection,
        governor=None,
    )

    assert provider.selection == selection
    assert tool.name == "configured_tool"
    assert request_context["_tool_provider_owners"] == {
        "configured_tool": provider.provider_id,
    }
    assert request_context["_tool_provider_execution_digests"] == {
        provider.provider_id: provider_execution_digest({}, {}),
    }
    assert getattr(tool, "_qp_request_context") is request_context
    with pytest.raises(LookupError, match="resolved 0 matches"):
        await AgentBuilder().resolve_governed_action_tool(
            ctx=context,
            agent_config=SimpleNamespace(),
            request_context=request_context,
            provider_id=provider.provider_id,
            tool_name="missing_tool",
            tool_selection=selection,
            governor=None,
        )
    await assembly.close()


def test_provider_execution_digest_tracks_aliases_without_secrets() -> None:
    baseline = provider_execution_digest(
        {"endpoint": "stable"},
        {"service": "credential:one"},
    )

    assert baseline == provider_execution_digest(
        {"endpoint": "stable"},
        {"service": "credential:one"},
    )
    assert baseline != provider_execution_digest(
        {"endpoint": "changed"},
        {"service": "credential:one"},
    )
    assert baseline != provider_execution_digest(
        {"endpoint": "stable"},
        {"service": "credential:two"},
    )


@pytest.mark.asyncio
async def test_plugin_tool_host_is_minimal_configured_and_scoped() -> None:
    registry = GenerationRegistry()
    provider = _ConfiguredToolProvider()
    await registry.activate_bundle(
        _configured_tool_bundle(),
        lambda _: provider,
    )
    assembly = await RuntimeAssemblyFactory(registry).open(
        agent_id="default",
        session_id="transport-session",
        root_agent_id="default",
        root_session_id="transport-session",
        workspace_dir="/tmp/qwenpaw-workspace",
    )
    context = SimpleNamespace(
        invocation_scope=assembly.scope,
        extras={"runtime_assembly": assembly},
        workspace=SimpleNamespace(
            config=SimpleNamespace(
                capability_configs={
                    provider.provider_id: {"label": "configured"},
                },
                capability_credential_refs={
                    provider.provider_id: {
                        "service": "credential:tool-service",
                    },
                },
            ),
            driver_manager=SimpleNamespace(
                credential_store=_CredentialStore(),
            ),
        ),
    )

    # pylint: disable=protected-access
    tools = await AgentBuilder()._collect_provider_tools(
        tool_providers=(provider,),
        ctx=context,
        local_workspace=None,
        agent_config=SimpleNamespace(),
        request_context={},
        governor=None,
        active_modes=(),
        active_skills=(),
        enabled_features=(),
    )
    # pylint: enable=protected-access

    assert isinstance(provider.host, ProviderToolHost)
    assert provider.host.config_snapshot() == {"label": "configured"}
    detached = provider.host.config_snapshot()
    detached["label"] = "changed outside the host"
    assert provider.host.config_snapshot() == {"label": "configured"}
    assert not hasattr(provider.host, "local_workspace")
    assert not hasattr(provider.host, "agent_config")
    assert not hasattr(provider.host, "request_context")
    assert not hasattr(provider.host, "governor")
    handle = provider.host.credential("service")
    assert handle is not None
    assert provider.host.credential("unbound") is None
    assert await handle.public_values() == {"account": "tool-user"}
    assert await handle.read_secret("token") == "tool-secret"
    assert len(tools) == 1
    await assembly.close()


@pytest.mark.asyncio
async def test_invalid_tool_config_fails_before_provider_call() -> None:
    registry = GenerationRegistry()
    provider = _ConfiguredToolProvider()
    await registry.activate_bundle(
        _configured_tool_bundle(),
        lambda _: provider,
    )
    assembly = await RuntimeAssemblyFactory(registry).open(
        agent_id="default",
        session_id="transport-session",
        root_agent_id="default",
        root_session_id="transport-session",
        workspace_dir="/tmp/qwenpaw-workspace",
    )
    context = SimpleNamespace(
        invocation_scope=assembly.scope,
        extras={"runtime_assembly": assembly},
        workspace=SimpleNamespace(
            config=SimpleNamespace(
                capability_configs={
                    provider.provider_id: {"label": 42},
                },
                capability_credential_refs={},
            ),
            driver_manager=None,
        ),
    )

    with pytest.raises(ValueError, match="does not match config_schema"):
        # pylint: disable=protected-access
        await AgentBuilder()._collect_provider_tools(
            tool_providers=(provider,),
            ctx=context,
            local_workspace=None,
            agent_config=SimpleNamespace(),
            request_context={},
            governor=None,
            active_modes=(),
            active_skills=(),
            enabled_features=(),
        )
        # pylint: enable=protected-access

    assert provider.host is None
    await assembly.close()


@pytest.mark.asyncio
async def test_tool_definition_registers_provider_owned_governance() -> None:
    from qwenpaw.governance.tool_registry import DEFAULT_REGISTRY

    context = SimpleNamespace(invocation_scope=_scope())
    try:
        # pylint: disable=protected-access
        tools = await AgentBuilder()._collect_provider_tools(
            tool_providers=(_DefinedToolProvider(),),
            ctx=context,
            local_workspace=None,
            agent_config=SimpleNamespace(),
            request_context={"agent_id": "default"},
            governor=None,
            active_modes=(),
            active_skills=(),
            enabled_features=(),
        )
        # pylint: enable=protected-access

        assert len(tools) == 1
        assert DEFAULT_REGISTRY.get_type("DefinedProviderTool") == "internal"
        assert DEFAULT_REGISTRY.get_owner("_defined_provider_tool") == (
            "example.defined-tools"
        )
        assert getattr(tools[0], "_qp_action_kind") is ActionKind.BROWSER
    finally:
        DEFAULT_REGISTRY.unregister_owner("example.defined-tools")


async def _versioned_provider_tool(path: str = "") -> str:
    return path


class _VersionedToolProvider:
    provider_id = "example.versioned-tools"

    def __init__(
        self,
        tool_type: str,
        target_param: str,
        effect: ToolEffect,
    ) -> None:
        self._tool_type = tool_type
        self._target_param = target_param
        self._effect = effect

    async def list_tools(self, *_args: Any) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                function=_versioned_provider_tool,
                name="_versioned_provider_tool",
                tool_type=self._tool_type,
                target_param=self._target_param,
                effect=self._effect,
            ),
        ]


@pytest.mark.asyncio
async def test_hot_replaced_tool_keeps_invocation_governance_snapshot(
    tmp_path,
) -> None:
    from qwenpaw.governance.tool_registry import DEFAULT_REGISTRY

    context = SimpleNamespace(invocation_scope=_scope())
    governor = SimpleNamespace(coding_project_dir=tmp_path)
    common = {
        "ctx": context,
        "local_workspace": None,
        "agent_config": SimpleNamespace(),
        "request_context": {"agent_id": "default"},
        "governor": governor,
        "active_modes": (),
        "active_skills": (),
        "enabled_features": (),
    }
    try:
        # pylint: disable=protected-access
        old_tools = await AgentBuilder()._collect_provider_tools(
            tool_providers=(
                _VersionedToolProvider("internal", "", ToolEffect.NONE),
            ),
            **common,
        )
        new_tools = await AgentBuilder()._collect_provider_tools(
            tool_providers=(
                _VersionedToolProvider(
                    "file",
                    "path",
                    ToolEffect.LOCAL_WRITE,
                ),
            ),
            **common,
        )
        old_tools[0]._qp_raw_params = {"path": "notes.txt"}
        new_tools[0]._qp_raw_params = {"path": "notes.txt"}
        old_spec = old_tools[0]._build_tc_spec()
        new_spec = new_tools[0]._build_tc_spec()
        # pylint: enable=protected-access

        assert old_spec.tool_type == "internal"
        assert old_spec.target == ""
        assert old_spec.effect == "none"
        assert new_spec.tool_type == "file"
        assert new_spec.target == str(tmp_path / "notes.txt")
        assert new_spec.effect == "local_write"
    finally:
        DEFAULT_REGISTRY.unregister_owner("example.versioned-tools")
