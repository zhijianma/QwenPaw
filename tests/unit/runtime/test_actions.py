# -*- coding: utf-8 -*-
"""Tests for the Lite Action Plane and governed-tool bridge."""

from __future__ import annotations

import asyncio
import hashlib
import json
import stat
from pathlib import Path
from uuid import uuid4

import pytest
from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolResponse

from qwenpaw.governance.tool_adapter import PolicyGuardedTool
from qwenpaw.governance.tool_registry import (
    DEFAULT_REGISTRY,
    register_tool_governance,
)
from qwenpaw.drivers.adapters.agentscope_tool import (
    DriverCapabilityTool,
    adapt_driver_definitions,
)
from qwenpaw.drivers.capabilities import (
    CapabilityExposure,
    DriverCapability,
    DriverInvocationResult,
)
from qwenpaw.kernel import (
    ActionKind,
    ActionRequest,
    ActionResult,
    ActionStatus,
    ArtifactRef,
    CapabilitySelection,
    DriverToolDefinition,
    EvidenceRef,
    EnvironmentRef,
    InvocationScope,
    RiskLevel,
    ToolEffect,
)
from qwenpaw.kernel.driver import DriverApprovalRejectedError
from qwenpaw.kernel.invocation import DEFAULT_DRIVER_PROVIDER_ID
from qwenpaw.runtime.actions import (
    ActionConflictError,
    ActionRequestPersistenceError,
    FilesystemActionStore,
    RuntimeActionRecorder,
)
from qwenpaw.runtime.environments import (
    LiteEnvironmentResolver,
    default_lite_environment_contract,
)
from qwenpaw.runtime.tool_artifacts import (
    TOOL_ARTIFACT_ERRORS_KEY,
    TOOL_ARTIFACT_LINKS_KEY,
)
from qwenpaw.tool_calls import ToolCallContext
from qwenpaw.tool_calls import ToolCoordinator


def _scope(tmp_path: Path, conversation_id: str = "chat-1") -> InvocationScope:
    return InvocationScope(
        agent_id="default",
        conversation_id=conversation_id,
        session_id="transport-session",
        root_agent_id="default",
        root_session_id="transport-session",
        workspace_dir=str(tmp_path),
        registry_generation=7,
        selection=CapabilitySelection(),
    )


def _context(
    tool_name: str = "plugin_export",
    tool_call_id: str = "call-1",
) -> ToolCallContext:
    context = ToolCallContext(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        session_id="transport-session",
        agent_id="default",
        root_session_id="transport-session",
        root_agent_id="default",
        started_at=0.0,
        offload_deadline=None,
        cancel_event=asyncio.Event(),
    )
    context.extra["tool_input"] = {
        "output_path": "report.md",
        "content": "private document body",
        "apiToken": "do-not-persist",
        "nested": {"password": "also-secret"},
    }
    return context


@pytest.mark.asyncio
async def test_action_store_is_private_and_never_persists_raw_values(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path)
    store = FilesystemActionStore(tmp_path)
    recorder = RuntimeActionRecorder(
        scope,
        store,
        tool_owners={"plugin_export": "example.report-tools"},
    )
    context = _context()

    request = await recorder.begin(
        context,
        effect=ToolEffect.LOCAL_WRITE,
        policy_decision="allow",
    )
    response = ToolResponse(
        content=[TextBlock(type="text", text="secret tool output")],
        id=context.tool_call_id,
        state=ToolResultState.SUCCESS,
    )
    await recorder.complete(response, context)

    [record] = await store.list_for_conversation("chat-1")
    assert record.request.capability_id == "example.report-tools"
    assert record.request.kind is ActionKind.TOOL
    assert record.request.risk is RiskLevel.MEDIUM
    assert record.result is not None
    assert record.result.status is ActionStatus.SUCCEEDED
    assert request.arguments["apiToken"] == "do-not-persist"

    request_path = next(
        tmp_path.glob(".qwenpaw/lite/actions/*/*/*/request.json"),
    )
    result_path = request_path.with_name("result.json")
    persisted = request_path.read_text(encoding="utf-8")
    result_payload = result_path.read_text(encoding="utf-8")
    assert "do-not-persist" not in persisted
    assert "also-secret" not in persisted
    assert "private document body" not in persisted
    assert "[CONTENT OMITTED]" in persisted
    assert "[REDACTED]" in persisted
    assert "secret tool output" not in result_payload
    assert "arguments" not in json.loads(persisted)
    assert "observation" not in json.loads(result_payload)
    raw_arguments_hash = hashlib.sha256(
        json.dumps(
            context.extra["tool_input"],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8"),
    ).hexdigest()
    assert record.request.arguments_hash != f"sha256:{raw_arguments_hash}"
    assert stat.S_IMODE(request_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(result_path.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_explicit_browser_kind_replaces_legacy_tool_inference(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path, conversation_id="chat-browser")
    store = FilesystemActionStore(tmp_path)
    recorder = RuntimeActionRecorder(scope, store)

    request = await recorder.begin(
        _context(tool_name="browser", tool_call_id="call-browser"),
        effect=ToolEffect.EXTERNAL_WRITE,
        policy_decision="allow",
        kind=ActionKind.BROWSER,
    )

    assert request.kind is ActionKind.BROWSER
    assert request.risk is RiskLevel.HIGH
    assert request.reversible is False


@pytest.mark.asyncio
async def test_action_request_references_resolved_environment(
    tmp_path: Path,
) -> None:
    invocation_id = uuid4()
    contract = default_lite_environment_contract(tmp_path)
    resolution = await LiteEnvironmentResolver().resolve(
        contract,
        invocation_id=invocation_id,
        workspace_dir=str(tmp_path),
    )
    scope = InvocationScope(
        invocation_id=invocation_id,
        agent_id="default",
        conversation_id="chat-1",
        session_id="transport-session",
        root_agent_id="default",
        root_session_id="transport-session",
        workspace_dir=str(tmp_path),
        registry_generation=7,
        selection=CapabilitySelection(),
        environment_contract=contract,
        environment_resolution=resolution,
    )
    recorder = RuntimeActionRecorder(scope, FilesystemActionStore(tmp_path))

    request = await recorder.begin(
        _context(tool_name="read_file"),
        effect=ToolEffect.NONE,
        policy_decision="allow",
    )

    assert request.environment_ref is not None
    assert request.environment_ref.resolution_id == resolution.resolution_id
    assert request.environment_ref.contract_id == contract.contract_id
    assert request.environment_ref.contract_version == contract.version
    assert request.environment_ref.resolver_id == resolution.resolver_id

    action_environment = EnvironmentRef(
        resolution_id=uuid4(),
        contract_id="qwenpaw.system.environment.sandbox",
        contract_version="1.0.0",
        resolver_id="qwenpaw.system.environment.sandbox-resolver",
    )
    sandboxed = await recorder.begin(
        _context(tool_name="Bash", tool_call_id="call-sandboxed"),
        effect=ToolEffect.PROCESS,
        policy_decision="sandbox_fallback",
        environment_ref=action_environment,
    )

    assert sandboxed.environment_ref == action_environment


@pytest.mark.asyncio
async def test_action_store_is_idempotent_but_rejects_conflicts(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path)
    store = FilesystemActionStore(tmp_path)
    request = ActionRequest(
        invocation_id=scope.invocation_id,
        correlation_id=scope.invocation_id,
        conversation_id=scope.conversation_id,
        registry_generation=scope.registry_generation,
        capability_id="qwenpaw.system.workspace-tools",
        kind=ActionKind.TOOL,
        action_name="read_file",
        arguments={"path": "README.md"},
        redacted_arguments={"path": "README.md"},
        arguments_hash="sha256:" + "a" * 64,
        idempotency_key="tool:one",
    )
    await store.begin(request)
    await store.begin(
        request.model_copy(update={"requested_at": request.requested_at}),
    )

    with pytest.raises(ActionConflictError):
        await store.begin(
            request.model_copy(update={"action_name": "write_file"}),
        )


@pytest.mark.asyncio
async def test_action_result_links_published_artifacts_and_partial_status(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path)
    store = FilesystemActionStore(tmp_path)
    recorder = RuntimeActionRecorder(scope, store)
    context = _context("write_file")
    await recorder.begin(
        context,
        effect=ToolEffect.LOCAL_WRITE,
        policy_decision="allow",
    )
    artifact = ArtifactRef(
        kind="document.markdown",
        uri="qwenpaw://artifact/example",
        media_type="text/markdown",
        content_hash="sha256:" + "b" * 64,
        size_bytes=12,
    )
    evidence = EvidenceRef(
        artifact_id=artifact.artifact_id,
        claim="The file was captured",
        producer="qwenpaw.system.workspace-tools.write_file",
    )
    response = ToolResponse(
        content=[TextBlock(type="text", text="written")],
        id=context.tool_call_id,
        state=ToolResultState.SUCCESS,
        metadata={
            TOOL_ARTIFACT_LINKS_KEY: [
                {
                    "artifact_ref": artifact.model_dump(mode="json"),
                    "evidence_ref": evidence.model_dump(mode="json"),
                },
            ],
            TOOL_ARTIFACT_ERRORS_KEY: [{"code": "one_output_missing"}],
        },
    )

    await recorder.complete(response, context)

    [record] = await store.list_for_conversation("chat-1")
    assert record.result is not None
    assert record.result.status is ActionStatus.PARTIAL
    assert record.result.error_code == "artifact_capture_partial"
    assert record.result.side_effect_status is not None
    assert record.result.side_effect_status.value == "succeeded"
    assert record.result.artifact_refs == (artifact,)
    assert record.result.evidence_refs == (evidence,)


@pytest.mark.asyncio
async def test_failed_action_keeps_diagnostic_artifact_evidence(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path)
    store = FilesystemActionStore(tmp_path)
    recorder = RuntimeActionRecorder(scope, store)
    context = _context("browser")
    await recorder.begin(
        context,
        effect=ToolEffect.EXTERNAL_WRITE,
        policy_decision="allow",
        kind=ActionKind.BROWSER,
    )
    artifact = ArtifactRef(
        kind="browser.output",
        uri="qwenpaw://artifact/browser-output",
        media_type="text/plain",
        content_hash="sha256:" + "d" * 64,
        size_bytes=20,
    )
    evidence = EvidenceRef(
        artifact_id=artifact.artifact_id,
        claim="Browser preserved its overflow output",
        producer="qwenpaw.system.workspace-tools.browser",
    )
    response = ToolResponse(
        content=[TextBlock(type="text", text="output_too_large")],
        id=context.tool_call_id,
        state=ToolResultState.ERROR,
        metadata={
            TOOL_ARTIFACT_LINKS_KEY: [
                {
                    "artifact_ref": artifact.model_dump(mode="json"),
                    "evidence_ref": evidence.model_dump(mode="json"),
                },
            ],
        },
    )

    await recorder.complete(response, context)

    [record] = await store.list_for_conversation("chat-1")
    assert record.request.kind is ActionKind.BROWSER
    assert record.result is not None
    assert record.result.status is ActionStatus.FAILED
    assert record.result.artifact_refs == (artifact,)
    assert record.result.evidence_refs == (evidence,)


@pytest.mark.asyncio
async def test_result_without_request_is_rejected(tmp_path: Path) -> None:
    store = FilesystemActionStore(tmp_path)
    invocation_id = uuid4()
    result = ActionResult(
        action_id=uuid4(),
        invocation_id=invocation_id,
        conversation_id="chat-1",
        status=ActionStatus.UNKNOWN,
        observation_digest="sha256:" + "c" * 64,
    )

    with pytest.raises(ActionConflictError):
        await store.complete(result)


@pytest.mark.asyncio
async def test_request_store_failure_prevents_tool_execution(
    tmp_path: Path,
) -> None:
    class FailingStore:
        async def begin(self, request):
            raise OSError("disk unavailable")

        async def complete(self, result):
            raise AssertionError("completion must not run")

        async def link_approval(self, link):
            raise AssertionError("approval linking must not run")

        async def list_for_conversation(self, conversation_id, *, limit=100):
            del conversation_id, limit
            return ()

    scope = _scope(tmp_path, conversation_id="chat-fail-closed")
    recorder = RuntimeActionRecorder(scope, FailingStore())
    executed = False

    async def guarded_probe(value: str):
        """Must remain unreachable when request persistence fails."""
        nonlocal executed
        executed = True
        return ToolResponse(
            content=[TextBlock(type="text", text=value)],
            id="call-fail-closed",
        )

    owner = "test.action-pipeline.fail-closed"
    register_tool_governance(
        DEFAULT_REGISTRY,
        python_name="guarded_probe",
        tool_type="internal",
        effect="none",
        owner=owner,
    )
    tool = PolicyGuardedTool(
        guarded_probe,
        request_context={
            "session_id": scope.session_id,
            "agent_id": scope.agent_id,
            "_action_recorder": recorder,
        },
    )
    coordinator = ToolCoordinator()

    async def next_handler(tool_call):
        yield await tool(  # pylint: disable=not-callable
            value=tool_call.input["value"],
        )

    tool_call = type(
        "ToolCall",
        (),
        {
            "id": "call-fail-closed",
            "name": "guarded_probe",
            "input": {"value": "never"},
        },
    )()
    try:
        events = []
        async for event in coordinator.execute(
            tool_call=tool_call,
            next_handler=next_handler,
            session_id=scope.session_id,
            agent_id=scope.agent_id,
            root_session_id=scope.root_session_id,
            result_processor=recorder.complete,
        ):
            events.append(event)
    finally:
        DEFAULT_REGISTRY.unregister_owner(owner)

    assert executed is False
    assert events[-1].state is ToolResultState.ERROR
    assert "not executed" in events[-1].content[0].text
    assert "outcome is unknown" not in events[-1].content[0].text


def test_request_persistence_error_has_safe_public_message() -> None:
    assert "not executed" in str(
        ActionRequestPersistenceError(
            "tool was not executed because action intent could not be "
            "recorded",
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_id", "tool_name", "action_kind", "expected_kind"),
    [
        (
            "qwenpaw.system.workspace-tools",
            "builtin_probe",
            None,
            ActionKind.TOOL,
        ),
        (
            "example.probe-tools",
            "plugin_probe",
            ActionKind.BROWSER,
            ActionKind.BROWSER,
        ),
    ],
)
async def test_governed_system_and_plugin_tools_share_action_pipeline(
    tmp_path: Path,
    provider_id: str,
    tool_name: str,
    action_kind: ActionKind | None,
    expected_kind: ActionKind,
) -> None:
    scope = _scope(tmp_path, conversation_id=f"chat-{tool_name}")
    store = FilesystemActionStore(tmp_path)
    recorder = RuntimeActionRecorder(
        scope,
        store,
        tool_owners={tool_name: provider_id},
    )
    observed_request_before_execution = False

    async def probe(value: str):
        """Return a value after checking pre-execution evidence."""
        nonlocal observed_request_before_execution
        records = await store.list_for_conversation(
            scope.conversation_id or "",
        )
        observed_request_before_execution = (
            len(records) == 1 and records[0].result is None
        )
        return ToolResponse(
            content=[TextBlock(type="text", text=value)],
            id="call-probe",
            state=ToolResultState.SUCCESS,
        )

    probe.__name__ = tool_name
    registry_owner = f"test.action-pipeline.{tool_name}"
    register_tool_governance(
        DEFAULT_REGISTRY,
        python_name=tool_name,
        tool_type="internal",
        effect="none",
        owner=registry_owner,
    )
    request_context = {
        "session_id": scope.session_id,
        "agent_id": scope.agent_id,
        "os_invocation_id": str(scope.invocation_id),
        "os_correlation_id": str(scope.correlation_id or scope.invocation_id),
        "_action_recorder": recorder,
    }
    tool = PolicyGuardedTool(
        probe,
        request_context=request_context,
        action_kind=action_kind,
    )
    coordinator = ToolCoordinator()

    async def next_handler(tool_call):
        yield await tool(  # pylint: disable=not-callable
            value=tool_call.input["value"],
        )

    tool_call = type(
        "ToolCall",
        (),
        {"id": "call-probe", "name": tool_name, "input": {"value": "ok"}},
    )()
    try:
        events = []
        async for event in coordinator.execute(
            tool_call=tool_call,
            next_handler=next_handler,
            session_id=scope.session_id,
            agent_id=scope.agent_id,
            root_session_id=scope.root_session_id,
            result_processor=recorder.complete,
        ):
            events.append(event)
    finally:
        DEFAULT_REGISTRY.unregister_owner(registry_owner)

    assert observed_request_before_execution is True
    assert events[-1].state is ToolResultState.SUCCESS
    [record] = await store.list_for_conversation(
        scope.conversation_id or "",
    )
    assert record.request.capability_id == provider_id
    assert record.request.kind is expected_kind
    assert record.result is not None
    assert record.result.status is ActionStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_driver_definition_uses_real_provider_and_capability_identity(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path, conversation_id="chat-driver-action")
    store = FilesystemActionStore(tmp_path)
    recorder = RuntimeActionRecorder(scope, store)
    observed_request_before_execution = False

    async def invoke(payload):
        nonlocal observed_request_before_execution
        records = await store.list_for_conversation("chat-driver-action")
        observed_request_before_execution = (
            len(records) == 1 and records[0].result is None
        )
        return {"echo": payload["value"]}

    definition = DriverToolDefinition(
        provider_id="example.driver-provider",
        capability_id="driver://example/tools/write#invoke",
        name="driver_write",
        invoke=invoke,
    )
    [tool] = adapt_driver_definitions(
        [definition],
        request_context={"_action_recorder": recorder},
    )
    coordinator = ToolCoordinator()
    tool_call = type(
        "ToolCall",
        (),
        {
            "id": "call-driver",
            "name": "driver_write",
            "input": {"value": "ok"},
        },
    )()

    async def next_handler(tool_call):
        yield await tool(value=tool_call.input["value"])

    events = []
    async for event in coordinator.execute(
        tool_call=tool_call,
        next_handler=next_handler,
        session_id=scope.session_id,
        agent_id=scope.agent_id,
        root_session_id=scope.root_session_id,
        result_processor=recorder.complete,
    ):
        events.append(event)

    assert observed_request_before_execution is True
    assert events[-1].state is ToolResultState.SUCCESS
    [record] = await store.list_for_conversation("chat-driver-action")
    assert record.request.kind is ActionKind.DRIVER
    assert record.request.capability_id == "example.driver-provider"
    assert record.request.action_name == (
        "driver://example/tools/write#invoke"
    )
    assert record.request.effect is ToolEffect.EXTERNAL_WRITE
    assert record.request.risk is RiskLevel.HIGH
    assert record.request.reversible is False
    assert record.result is not None
    assert record.result.status is ActionStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_legacy_driver_capability_uses_action_plane(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path, conversation_id="chat-legacy-driver")
    store = FilesystemActionStore(tmp_path)
    recorder = RuntimeActionRecorder(scope, store)
    capability = DriverCapability(
        capability_id="driver://mcp/legacy/tools/read#invoke",
        driver_name="legacy",
        protocol="mcp",
        kind="tool",
        action="invoke",
        name="read",
        exposure=CapabilityExposure(as_tool=True, tool_name="legacy_read"),
    )

    async def invoke(_invocation):
        return DriverInvocationResult(ok=True, value="done")

    tool = DriverCapabilityTool(
        capability,
        invoke,
        request_context={"_action_recorder": recorder},
    )
    coordinator = ToolCoordinator()
    tool_call = type(
        "ToolCall",
        (),
        {"id": "call-legacy-driver", "name": tool.name, "input": {}},
    )()

    async def next_handler(tool_call):
        del tool_call
        yield await tool()

    async for _event in coordinator.execute(
        tool_call=tool_call,
        next_handler=next_handler,
        session_id=scope.session_id,
        agent_id=scope.agent_id,
        root_session_id=scope.root_session_id,
        result_processor=recorder.complete,
    ):
        pass

    [record] = await store.list_for_conversation("chat-legacy-driver")
    assert record.request.kind is ActionKind.DRIVER
    assert record.request.capability_id == DEFAULT_DRIVER_PROVIDER_ID
    assert record.request.action_name == capability.capability_id
    assert record.result is not None
    assert record.result.status is ActionStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_driver_approval_rejection_is_a_denied_action(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path, conversation_id="chat-driver-denied")
    store = FilesystemActionStore(tmp_path)
    recorder = RuntimeActionRecorder(scope, store)

    async def invoke(_payload):
        raise DriverApprovalRejectedError(
            "driver://example/tools/write#invoke",
            "user denied",
        )

    definition = DriverToolDefinition(
        provider_id="example.driver-provider",
        capability_id="driver://example/tools/write#invoke",
        name="driver_write_denied",
        invoke=invoke,
    )
    [tool] = adapt_driver_definitions(
        [definition],
        request_context={"_action_recorder": recorder},
    )
    coordinator = ToolCoordinator()
    tool_call = type(
        "ToolCall",
        (),
        {
            "id": "call-driver-denied",
            "name": definition.name,
            "input": {"value": "blocked"},
        },
    )()

    async def next_handler(tool_call):
        yield await tool(value=tool_call.input["value"])

    events = []
    async for event in coordinator.execute(
        tool_call=tool_call,
        next_handler=next_handler,
        session_id=scope.session_id,
        agent_id=scope.agent_id,
        root_session_id=scope.root_session_id,
        result_processor=recorder.complete,
    ):
        events.append(event)

    assert events[-1].state is ToolResultState.DENIED
    [record] = await store.list_for_conversation("chat-driver-denied")
    assert record.result is not None
    assert record.result.status is ActionStatus.DENIED
    assert record.result.error_code == "denied"
