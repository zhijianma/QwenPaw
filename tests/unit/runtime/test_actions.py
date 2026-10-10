# -*- coding: utf-8 -*-
"""Tests for the Lite Action Plane and governed-tool bridge."""

from __future__ import annotations

import asyncio
import hashlib
import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolResponse

from qwenpaw.governance.tool_adapter import PolicyGuardedTool
from qwenpaw.governance.policy import (
    GovernanceAction,
    GovernanceDecision,
)
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
    ACTION_RETRY_DECISION_METADATA_KEY,
    ACTION_RETRY_HINT_METADATA_KEY,
    COMMITTED_ACTION_ITEM_METADATA_KEY,
    ActionIdempotencyMode,
    ActionKind,
    ActionRecord,
    ActionRequest,
    ActionResult,
    ActionRetryDisposition,
    ActionRetryPolicy,
    ActionRetryReason,
    ActionStatus,
    ArtifactRef,
    CapabilitySelection,
    CommittedActionItem,
    DriverToolDefinition,
    EvidenceRef,
    EnvironmentRef,
    InvocationScope,
    ModelStepReconciliationReason,
    RiskLevel,
    SideEffectStatus,
    ToolEffect,
    ToolSelection,
)
from qwenpaw.kernel.driver import DriverApprovalRejectedError
from qwenpaw.kernel.invocation import DEFAULT_DRIVER_PROVIDER_ID
from qwenpaw.runtime.actions import (
    ACTION_RESULT_CONTEXT_KEY,
    ActionConflictError,
    ActionRetryNotReadyError,
    ActionRequestPersistenceError,
    FilesystemActionStore,
    RuntimeActionRecorder,
    assess_model_step_reconciliation,
    model_step_action_call_ids,
    model_step_action_context_bindings,
    model_step_action_evidence_digest,
    model_step_reconciliation_evidence_digest,
    model_step_committed_action_items,
)
from qwenpaw.plugins.sdk import current_action_execution
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
        "code": "private browser program",
        "script": "private automation script",
        "apiToken": "do-not-persist",
        "nested": {"password": "also-secret"},
    }
    return context


def test_action_retry_policy_bounds_exponential_backoff() -> None:
    policy = ActionRetryPolicy(
        max_attempts=4,
        initial_delay_seconds=2,
        backoff_multiplier=3,
        max_delay_seconds=5,
    )

    assert policy.delay_before_attempt(2) == 2
    assert policy.delay_before_attempt(3) == 5
    assert policy.delay_before_attempt(4) == 5
    with pytest.raises(ValueError, match="attempt 2"):
        policy.delay_before_attempt(1)
    with pytest.raises(ValueError, match="exceeds"):
        ActionRetryPolicy(
            initial_delay_seconds=2,
            max_delay_seconds=1,
        )


def _action_record(
    invocation_id,
    *,
    suffix: str,
    status: ActionStatus | None = None,
    side_effect_status: SideEffectStatus | None = None,
) -> ActionRecord:
    request = ActionRequest(
        invocation_id=invocation_id,
        correlation_id=uuid4(),
        conversation_id="chat-recovery",
        registry_generation=1,
        capability_id="qwenpaw.system.test-tool",
        kind=ActionKind.TOOL,
        action_name=f"test_{suffix}",
        redacted_arguments={},
        arguments_hash=f"sha256:{'a' * 64}",
        idempotency_key=f"action-{suffix}",
    )
    result = None
    if status is not None:
        result = ActionResult(
            action_id=request.action_id,
            invocation_id=invocation_id,
            conversation_id=request.conversation_id,
            status=status,
            observation_digest=f"sha256:{'b' * 64}",
            side_effect_status=side_effect_status,
        )
    return ActionRecord(request=request, result=result)


def test_model_step_reconciliation_classifies_action_evidence() -> None:
    invocation_id = uuid4()
    unrelated = _action_record(uuid4(), suffix="unrelated")
    assert assess_model_step_reconciliation(
        [unrelated],
        invocation_id,
    ) is None

    pending = _action_record(invocation_id, suffix="pending")
    assessment = assess_model_step_reconciliation(
        [unrelated, pending],
        invocation_id,
    )
    assert assessment is not None
    assert assessment.reason is (
        ModelStepReconciliationReason.PENDING_ACTION_RESULT
    )
    assert assessment.pending_result_count == 1
    assert assessment.terminal_result_count == 0

    uncertain = _action_record(
        invocation_id,
        suffix="uncertain",
        status=ActionStatus.UNKNOWN,
        side_effect_status=SideEffectStatus.UNCERTAIN,
    )
    assessment = assess_model_step_reconciliation(
        [pending, uncertain],
        invocation_id,
    )
    assert assessment is not None
    assert assessment.reason is (
        ModelStepReconciliationReason.UNCERTAIN_SIDE_EFFECT
    )
    assert assessment.action_count == 2
    assert assessment.pending_result_count == 1
    assert assessment.uncertain_side_effect_count == 1
    assert assessment.terminal_result_count == 1

    succeeded = _action_record(
        invocation_id,
        suffix="succeeded",
        status=ActionStatus.SUCCEEDED,
        side_effect_status=SideEffectStatus.SUCCEEDED,
    )
    assessment = assess_model_step_reconciliation(
        [succeeded],
        invocation_id,
    )
    assert assessment is not None
    assert assessment.reason is (
        ModelStepReconciliationReason.DURABLE_CONTEXT_REQUIRED
    )


def test_model_step_action_evidence_requires_stable_executor_call_id() -> None:
    invocation_id = uuid4()
    record = _action_record(
        invocation_id,
        suffix="succeeded",
        status=ActionStatus.SUCCEEDED,
    )
    request = record.request.model_copy(
        update={
            "idempotency_key": f"tool:{invocation_id}:call-1",
        },
    )
    record = record.model_copy(update={"request": request})

    digest = model_step_action_evidence_digest(
        [record],
        invocation_id,
    )

    assert digest is not None
    assert digest.startswith("sha256:")
    assert model_step_reconciliation_evidence_digest([], invocation_id) is None
    assert model_step_action_call_ids(
        [record],
        invocation_id,
    ) == ("call-1",)
    assert model_step_action_context_bindings(
        [record],
        invocation_id,
    ) == (("call-1", str(record.request.action_id)),)
    committed_items = model_step_committed_action_items(
        [record],
        invocation_id,
    )
    assert committed_items is not None
    assert len(committed_items) == 1
    committed_item = committed_items[0]
    assert committed_item.action_id == record.request.action_id
    assert committed_item.executor_item_id == "call-1"
    assert committed_item.observation_digest == (
        record.result.observation_digest
    )
    assert model_step_action_call_ids(
        [_action_record(invocation_id, suffix="legacy")],
        invocation_id,
    ) is None


def test_uncertain_action_has_exact_reconciliation_digest() -> None:
    invocation_id = uuid4()
    uncertain = _action_record(
        invocation_id,
        suffix="uncertain",
        status=ActionStatus.UNKNOWN,
        side_effect_status=SideEffectStatus.UNCERTAIN,
    )

    digest = model_step_reconciliation_evidence_digest(
        [uncertain],
        invocation_id,
    )

    assert digest is not None
    assert digest.startswith("sha256:")
    assert (
        model_step_reconciliation_evidence_digest(
            [_action_record(invocation_id, suffix="pending")],
            invocation_id,
        )
        is None
    )


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
    assert response.metadata[ACTION_RESULT_CONTEXT_KEY] == str(
        request.action_id,
    )
    committed_item = CommittedActionItem.model_validate(
        response.metadata[COMMITTED_ACTION_ITEM_METADATA_KEY],
    )
    assert committed_item.action_id == request.action_id
    assert committed_item.invocation_id == scope.invocation_id
    assert committed_item.conversation_id == "chat-1"
    assert committed_item.executor_item_id == context.tool_call_id
    assert committed_item.observation_digest == (
        record.result.observation_digest
    )
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
    assert "private browser program" not in persisted
    assert "private automation script" not in persisted
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
async def test_background_snapshot_is_prepared_before_result_commit(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path)
    store = FilesystemActionStore(tmp_path)
    recorder = RuntimeActionRecorder(scope, store)
    context = _context()
    await recorder.begin(
        context,
        effect=ToolEffect.NONE,
        policy_decision="allow",
    )
    observed_result_states: list[bool] = []

    async def prepare_background(_response, _context, _item) -> None:
        [record] = await store.list_for_conversation("chat-1")
        observed_result_states.append(record.result is None)

    context.extra["_prepare_background_action_context"] = (
        prepare_background
    )
    response = ToolResponse(
        content=[TextBlock(type="text", text="background result")],
        id=context.tool_call_id,
        state=ToolResultState.SUCCESS,
    )

    await recorder.complete(response, context)

    [record] = await store.list_for_conversation("chat-1")
    assert observed_result_states == [True]
    assert record.result is not None
    assert COMMITTED_ACTION_ITEM_METADATA_KEY in response.metadata


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
            ACTION_RETRY_HINT_METADATA_KEY: True,
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
    assert record.result.retryable is False
    assert record.result.side_effect_status is SideEffectStatus.UNCERTAIN
    assert record.result.retry_decision is not None
    assert record.result.retry_decision.disposition is (
        ActionRetryDisposition.RECONCILE_REQUIRED
    )
    assert record.result.retry_decision.reason is (
        ActionRetryReason.SIDE_EFFECT_UNCERTAIN
    )
    assert record.result.artifact_refs == (artifact,)
    assert record.result.evidence_refs == (evidence,)


@pytest.mark.asyncio
async def test_read_failure_requires_provider_hint_for_new_action_retry(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path, conversation_id="chat-retry-read")
    store = FilesystemActionStore(tmp_path)
    recorder = RuntimeActionRecorder(scope, store)
    context = _context("read_file", "call-read")
    await recorder.begin(
        context,
        effect=ToolEffect.NONE,
        policy_decision="allow",
    )
    response = ToolResponse(
        content=[TextBlock(type="text", text="temporarily unavailable")],
        id=context.tool_call_id,
        state=ToolResultState.ERROR,
        metadata={ACTION_RETRY_HINT_METADATA_KEY: True},
    )

    await recorder.complete(response, context)

    [record] = await store.list_for_conversation("chat-retry-read")
    assert record.result is not None
    assert record.result.retryable is True
    assert record.result.side_effect_status is None
    assert record.result.retry_decision is not None
    assert record.result.retry_decision.disposition is (
        ActionRetryDisposition.RETRY_FROM_NEW_ACTION
    )
    assert record.result.retry_decision.reason is (
        ActionRetryReason.TRANSIENT_FAILURE
    )
    assert response.metadata[ACTION_RETRY_DECISION_METADATA_KEY] == (
        record.result.retry_decision.model_dump(mode="json")
    )


@pytest.mark.asyncio
async def test_executor_idempotency_admits_new_attempt_with_lineage(
    tmp_path: Path,
) -> None:
    conversation_id = "chat-retry-write"
    correlation_id = uuid4()
    first_scope = _scope(
        tmp_path,
        conversation_id=conversation_id,
    ).model_copy(update={"correlation_id": correlation_id})
    store = FilesystemActionStore(tmp_path)
    first_recorder = RuntimeActionRecorder(first_scope, store)
    first_context = _context("write_file", "call-write-1")
    await first_recorder.begin(
        first_context,
        effect=ToolEffect.LOCAL_WRITE,
        policy_decision="allow",
        idempotency_mode=ActionIdempotencyMode.EXECUTOR_ENFORCED,
    )
    first_response = ToolResponse(
        content=[TextBlock(type="text", text="temporary failure")],
        id=first_context.tool_call_id,
        state=ToolResultState.ERROR,
        metadata={ACTION_RETRY_HINT_METADATA_KEY: True},
    )
    await first_recorder.complete(first_response, first_context)
    [previous] = await store.list_for_conversation(conversation_id)
    assert previous.result is not None
    assert previous.result.retry_decision is not None
    assert previous.result.retry_decision.reason is (
        ActionRetryReason.EXECUTOR_IDEMPOTENT_FAILURE
    )

    retry_scope = _scope(
        tmp_path,
        conversation_id=conversation_id,
    ).model_copy(update={"correlation_id": correlation_id})
    retry_recorder = RuntimeActionRecorder(retry_scope, store)
    retry_context = _context("write_file", "call-write-2")
    retry = await retry_recorder.begin_retry(retry_context, previous)

    assert retry.action_id != previous.request.action_id
    assert retry.retry_root_action_id == previous.request.action_id
    assert retry.retry_of_action_id == previous.request.action_id
    assert retry.attempt == 2
    assert retry.idempotency_key == previous.request.idempotency_key
    assert retry.idempotency_mode is (
        ActionIdempotencyMode.EXECUTOR_ENFORCED
    )
    assert previous.result.retry_decision.max_attempts == 2
    assert previous.result.retry_decision.next_attempt == 2
    assert previous.result.retry_decision.retry_after_seconds == 0
    retry_response = ToolResponse(
        content=[TextBlock(type="text", text="written")],
        id=retry_context.tool_call_id,
        state=ToolResultState.SUCCESS,
    )
    await retry_recorder.complete(retry_response, retry_context)
    records = await store.scan_for_conversation(conversation_id)
    assert model_step_action_call_ids(
        records,
        retry_scope.invocation_id,
    ) == (retry_context.tool_call_id,)

    changed = _context("write_file", "call-write-3")
    changed.extra["tool_input"]["output_path"] = "other.md"
    with pytest.raises(ActionConflictError, match="arguments changed"):
        await retry_recorder.begin_retry(changed, previous)


@pytest.mark.asyncio
async def test_action_retry_uses_current_matching_environment_resolution(
    tmp_path: Path,
) -> None:
    conversation_id = "chat-retry-environment"
    correlation_id = uuid4()
    contract = default_lite_environment_contract(tmp_path)
    resolver = LiteEnvironmentResolver()
    first_invocation_id = uuid4()
    first_resolution = await resolver.resolve(
        contract,
        invocation_id=first_invocation_id,
        workspace_dir=str(tmp_path),
    )
    first_scope = InvocationScope(
        invocation_id=first_invocation_id,
        correlation_id=correlation_id,
        agent_id="default",
        conversation_id=conversation_id,
        session_id="transport-session",
        root_agent_id="default",
        root_session_id="transport-session",
        workspace_dir=str(tmp_path),
        registry_generation=7,
        selection=CapabilitySelection(),
        environment_contract=contract,
        environment_resolution=first_resolution,
    )
    store = FilesystemActionStore(tmp_path)
    first_recorder = RuntimeActionRecorder(first_scope, store)
    first_context = _context("write_file", "call-environment-1")
    await first_recorder.begin(
        first_context,
        effect=ToolEffect.LOCAL_WRITE,
        policy_decision="allow",
        idempotency_mode=ActionIdempotencyMode.EXECUTOR_ENFORCED,
    )
    await first_recorder.complete(
        ToolResponse(
            content=[TextBlock(type="text", text="temporary failure")],
            id=first_context.tool_call_id,
            state=ToolResultState.ERROR,
            metadata={ACTION_RETRY_HINT_METADATA_KEY: True},
        ),
        first_context,
    )
    [previous] = await store.list_for_conversation(conversation_id)

    retry_invocation_id = uuid4()
    retry_resolution = await resolver.resolve(
        contract,
        invocation_id=retry_invocation_id,
        workspace_dir=str(tmp_path),
    )
    retry_scope = first_scope.model_copy(
        update={
            "invocation_id": retry_invocation_id,
            "environment_resolution": retry_resolution,
        },
    )
    retry = await RuntimeActionRecorder(
        retry_scope,
        store,
    ).begin_retry(
        _context("write_file", "call-environment-2"),
        previous,
    )

    assert retry.environment_ref is not None
    assert retry.environment_ref.resolution_id == (
        retry_resolution.resolution_id
    )
    assert retry.environment_ref.resolution_id != (
        first_resolution.resolution_id
    )


@pytest.mark.asyncio
async def test_action_retry_rejects_changed_tool_selection(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path, "chat-retry-selection")
    store = FilesystemActionStore(tmp_path)
    first_selection = ToolSelection(active_modes=("coding",))
    first_recorder = RuntimeActionRecorder(
        scope,
        store,
        tool_selection=first_selection,
    )
    first_context = _context("read_file", "call-selection-1")
    await first_recorder.begin(
        first_context,
        effect=ToolEffect.NONE,
        policy_decision="allow",
    )
    await first_recorder.complete(
        ToolResponse(
            content=[TextBlock(type="text", text="temporary failure")],
            id=first_context.tool_call_id,
            state=ToolResultState.ERROR,
            metadata={ACTION_RETRY_HINT_METADATA_KEY: True},
        ),
        first_context,
    )
    [previous] = await store.list_for_conversation(
        scope.conversation_id,
    )
    retry_scope = scope.model_copy(
        update={
            "invocation_id": uuid4(),
            "correlation_id": scope.invocation_id,
        },
    )
    retry_recorder = RuntimeActionRecorder(
        retry_scope,
        store,
        tool_selection=ToolSelection(active_modes=("research",)),
    )

    with pytest.raises(ActionConflictError, match="tool selection mismatch"):
        await retry_recorder.begin_retry(
            _context("read_file", "call-selection-2"),
            previous,
        )


@pytest.mark.asyncio
async def test_action_retry_rejects_changed_provider_configuration(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path, "chat-retry-provider-config")
    store = FilesystemActionStore(tmp_path)
    first_digest = f"sha256:{'a' * 64}"
    first_recorder = RuntimeActionRecorder(
        scope,
        store,
        provider_execution_digests={
            "qwenpaw.system.workspace-tools": first_digest,
        },
    )
    first_context = _context("read_file", "call-provider-config-1")
    await first_recorder.begin(
        first_context,
        effect=ToolEffect.NONE,
        policy_decision="allow",
    )
    await first_recorder.complete(
        ToolResponse(
            content=[TextBlock(type="text", text="temporary failure")],
            id=first_context.tool_call_id,
            state=ToolResultState.ERROR,
            metadata={ACTION_RETRY_HINT_METADATA_KEY: True},
        ),
        first_context,
    )
    [previous] = await store.list_for_conversation(
        scope.conversation_id,
    )
    retry_scope = scope.model_copy(
        update={
            "invocation_id": uuid4(),
            "correlation_id": scope.invocation_id,
        },
    )
    retry_recorder = RuntimeActionRecorder(
        retry_scope,
        store,
        provider_execution_digests={
            "qwenpaw.system.workspace-tools": f"sha256:{'b' * 64}",
        },
    )

    with pytest.raises(
        ActionConflictError,
        match="provider configuration mismatch",
    ):
        await retry_recorder.begin_retry(
            _context("read_file", "call-provider-config-2"),
            previous,
        )


@pytest.mark.asyncio
async def test_action_retry_budget_is_persisted_and_exhausted(
    tmp_path: Path,
) -> None:
    conversation_id = "chat-retry-budget"
    correlation_id = uuid4()
    store = FilesystemActionStore(tmp_path)
    first_scope = _scope(
        tmp_path,
        conversation_id=conversation_id,
    ).model_copy(update={"correlation_id": correlation_id})
    first_recorder = RuntimeActionRecorder(
        first_scope,
        store,
        retry_policy=ActionRetryPolicy(max_attempts=2),
    )
    first_context = _context("read_file", "call-budget-1")
    await first_recorder.begin(
        first_context,
        effect=ToolEffect.NONE,
        policy_decision="allow",
    )
    failure = ToolResponse(
        content=[TextBlock(type="text", text="temporary failure")],
        id=first_context.tool_call_id,
        state=ToolResultState.ERROR,
        metadata={ACTION_RETRY_HINT_METADATA_KEY: True},
    )
    await first_recorder.complete(failure, first_context)
    [previous] = await store.list_for_conversation(conversation_id)

    retry_scope = _scope(
        tmp_path,
        conversation_id=conversation_id,
    ).model_copy(update={"correlation_id": correlation_id})
    retry_recorder = RuntimeActionRecorder(retry_scope, store)
    retry_context = _context("read_file", "call-budget-2")
    retry = await retry_recorder.begin_retry(retry_context, previous)
    await retry_recorder.complete(
        ToolResponse(
            content=[TextBlock(type="text", text="still unavailable")],
            id=retry_context.tool_call_id,
            state=ToolResultState.ERROR,
            metadata={ACTION_RETRY_HINT_METADATA_KEY: True},
        ),
        retry_context,
    )
    records = await store.scan_for_conversation(conversation_id)
    exhausted = next(
        record for record in records
        if record.request.action_id == retry.action_id
    )

    assert exhausted.request.retry_policy.max_attempts == 2
    assert exhausted.result is not None
    assert exhausted.result.retryable is False
    assert exhausted.result.retry_decision is not None
    assert exhausted.result.retry_decision.reason is (
        ActionRetryReason.ATTEMPT_BUDGET_EXHAUSTED
    )
    assert exhausted.result.retry_decision.next_attempt is None
    with pytest.raises(ActionConflictError, match="not admitted"):
        await retry_recorder.begin_retry(
            _context("read_file", "call-budget-3"),
            exhausted,
        )


@pytest.mark.asyncio
async def test_action_retry_waits_for_the_persisted_backoff_deadline(
    tmp_path: Path,
) -> None:
    conversation_id = "chat-retry-backoff"
    correlation_id = uuid4()
    clock_value = [datetime(2026, 10, 8, tzinfo=timezone.utc)]
    store = FilesystemActionStore(tmp_path)
    first_scope = _scope(
        tmp_path,
        conversation_id=conversation_id,
    ).model_copy(update={"correlation_id": correlation_id})
    first_recorder = RuntimeActionRecorder(
        first_scope,
        store,
        retry_policy=ActionRetryPolicy(
            max_attempts=3,
            initial_delay_seconds=5,
        ),
        clock=lambda: clock_value[0],
    )
    first_context = _context("read_file", "call-backoff-1")
    await first_recorder.begin(
        first_context,
        effect=ToolEffect.NONE,
        policy_decision="allow",
    )
    await first_recorder.complete(
        ToolResponse(
            content=[TextBlock(type="text", text="temporary failure")],
            id=first_context.tool_call_id,
            state=ToolResultState.ERROR,
            metadata={ACTION_RETRY_HINT_METADATA_KEY: True},
        ),
        first_context,
    )
    [previous] = await store.list_for_conversation(conversation_id)
    assert previous.result is not None
    assert previous.result.retry_decision is not None
    assert previous.result.retry_decision.retry_after_seconds == 5

    retry_scope = _scope(
        tmp_path,
        conversation_id=conversation_id,
    ).model_copy(update={"correlation_id": correlation_id})
    retry_recorder = RuntimeActionRecorder(
        retry_scope,
        store,
        clock=lambda: clock_value[0],
    )
    retry_context = _context("read_file", "call-backoff-2")
    clock_value[0] += timedelta(seconds=4)
    with pytest.raises(ActionRetryNotReadyError) as error:
        await retry_recorder.begin_retry(retry_context, previous)
    assert error.value.retry_at == (
        previous.result.completed_at + timedelta(seconds=5)
    )

    clock_value[0] += timedelta(seconds=1)
    retry = await retry_recorder.begin_retry(retry_context, previous)
    assert retry.attempt == 2
    assert retry.retry_policy == previous.request.retry_policy


@pytest.mark.asyncio
async def test_plugin_executor_reads_only_active_action_identity(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path, conversation_id="chat-plugin-execution")
    store = FilesystemActionStore(tmp_path)
    recorder = RuntimeActionRecorder(scope, store)
    observed = []

    async def idempotent_probe(value: str):
        observed.append(current_action_execution())
        return ToolResponse(
            content=[TextBlock(type="text", text=value)],
            id="call-idempotent-plugin",
            state=ToolResultState.SUCCESS,
        )

    owner = "test.action-pipeline.idempotent-plugin"
    register_tool_governance(
        DEFAULT_REGISTRY,
        python_name="idempotent_probe",
        tool_type="internal",
        effect="none",
        owner=owner,
    )
    tool = PolicyGuardedTool(
        idempotent_probe,
        request_context={"_action_recorder": recorder},
        action_idempotency=ActionIdempotencyMode.HOST_GUARDED,
    )
    coordinator = ToolCoordinator()
    tool_call = type(
        "ToolCall",
        (),
        {
            "id": "call-idempotent-plugin",
            "name": "idempotent_probe",
            "input": {"value": "ok"},
        },
    )()

    async def next_handler(tool_call):
        yield await tool(  # pylint: disable=not-callable
            value=tool_call.input["value"],
        )

    try:
        async for _event in coordinator.execute(
            tool_call=tool_call,
            next_handler=next_handler,
            session_id=scope.session_id,
            agent_id=scope.agent_id,
            root_session_id=scope.root_session_id,
            result_processor=recorder.complete,
        ):
            pass
    finally:
        DEFAULT_REGISTRY.unregister_owner(owner)

    assert len(observed) == 1
    execution = observed[0]
    assert execution is not None
    assert execution.invocation_id == scope.invocation_id
    assert execution.idempotency_mode is ActionIdempotencyMode.HOST_GUARDED
    assert execution.attempt == 1
    assert current_action_execution() is None


@pytest.mark.asyncio
async def test_tool_policy_audit_uses_authoritative_action_identity(
    tmp_path: Path,
) -> None:
    scope = _scope(tmp_path, conversation_id="chat-policy-audit")
    store = FilesystemActionStore(tmp_path)
    recorder = RuntimeActionRecorder(scope, store)

    class Governor:
        def __init__(self) -> None:
            self.audits = []

        def assert_policy(self, _spec, *, execution_level=None):
            del execution_level
            return GovernanceDecision(
                action=GovernanceAction.ALLOW,
                reason="contract test",
            )

        def audit(self, spec, decision):
            self.audits.append((spec, decision))

    governor = Governor()

    async def governed_write(value: str):
        return ToolResponse(
            content=[TextBlock(type="text", text=value)],
            id="call-policy-audit",
            state=ToolResultState.SUCCESS,
        )

    owner = "test.action-pipeline.policy-audit"
    register_tool_governance(
        DEFAULT_REGISTRY,
        python_name="governed_write",
        tool_type="file",
        effect="local_write",
        owner=owner,
    )
    tool = PolicyGuardedTool(
        governed_write,
        governor=governor,
        request_context={
            "session_id": scope.session_id,
            "agent_id": scope.agent_id,
            "approval_level": "strict",
            "os_chat_id": scope.chat_id,
            "os_invocation_id": str(scope.invocation_id),
            "os_correlation_id": str(
                scope.correlation_id or scope.invocation_id,
            ),
            "_action_recorder": recorder,
        },
    )
    coordinator = ToolCoordinator()
    tool_call = type(
        "ToolCall",
        (),
        {
            "id": "call-policy-audit",
            "name": "governed_write",
            "input": {"value": "ok"},
        },
    )()

    async def next_handler(tool_call):
        await tool.check_permissions(tool_call.input)
        yield await tool(  # pylint: disable=not-callable
            value=tool_call.input["value"],
        )

    try:
        async for _event in coordinator.execute(
            tool_call=tool_call,
            next_handler=next_handler,
            session_id=scope.session_id,
            agent_id=scope.agent_id,
            root_session_id=scope.root_session_id,
            result_processor=recorder.complete,
        ):
            pass
    finally:
        DEFAULT_REGISTRY.unregister_owner(owner)

    [record] = await store.list_for_conversation(scope.chat_id)
    [(audit_spec, _decision)] = governor.audits
    assert audit_spec.chat_id == scope.chat_id
    assert audit_spec.invocation_id == str(scope.invocation_id)
    assert audit_spec.correlation_id == str(
        scope.correlation_id or scope.invocation_id,
    )
    assert audit_spec.action_id == str(record.request.action_id)


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
    observed_execution = None

    async def invoke(payload):
        nonlocal observed_execution, observed_request_before_execution
        records = await store.list_for_conversation("chat-driver-action")
        observed_request_before_execution = (
            len(records) == 1 and records[0].result is None
        )
        observed_execution = current_action_execution()
        return {"echo": payload["value"]}

    definition = DriverToolDefinition(
        provider_id="example.driver-provider",
        capability_id="driver://example/tools/write#invoke",
        name="driver_write",
        idempotency_mode=ActionIdempotencyMode.EXECUTOR_ENFORCED,
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
    assert record.request.idempotency_mode is (
        ActionIdempotencyMode.EXECUTOR_ENFORCED
    )
    assert observed_execution is not None
    assert observed_execution.action_id == record.request.action_id
    assert observed_execution.idempotency_key == (
        record.request.idempotency_key
    )
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
        metadata={"action_idempotency": "executor_enforced"},
    )
    observed_request_context = {}

    async def invoke(invocation):
        observed_request_context.update(invocation.request_context)
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
    assert record.request.idempotency_mode is (
        ActionIdempotencyMode.EXECUTOR_ENFORCED
    )
    assert observed_request_context["qwenpaw_action_id"] == str(
        record.request.action_id,
    )
    assert observed_request_context["qwenpaw_action_idempotency_key"] == (
        record.request.idempotency_key
    )
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
