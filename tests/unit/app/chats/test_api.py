# -*- coding: utf-8 -*-
"""Ownership-boundary tests for the global chat API."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from qwenpaw.app.chats.api import (
    _artifact_receipt_from_history,
    get_chat,
    get_chat_status,
    list_chat_actions,
    list_chat_capability_locks,
    list_chat_context_manifests,
    list_chat_model_calls,
    list_chat_observations,
    page_chat_observations,
    page_chat_trajectory,
    list_chats,
)
from qwenpaw.app.chats.models import ChatHistory, ChatSpec
from qwenpaw.invocation_control import (
    InvocationControlService,
    SQLiteInvocationControl,
)
from qwenpaw.kernel import (
    ActionKind,
    ActionRequest,
    CapabilityLockManifest,
    CapabilityRelease,
    CapabilityProviderKind,
    ConversationOutcome,
    ConversationOutcomeStatus,
    ContextManifest,
    ArtifactRef,
    EvidenceRef,
    PlanStep,
    ModelCallAttempt,
    ModelRouteReason,
    RouteDecision,
    RunnerSignal,
    TurnSubmissionRequest,
    VerificationResult,
    VerificationStatus,
    RestartPolicy,
)
from qwenpaw.runtime.capability_locks import lite_capability_lock_store
from qwenpaw.runtime.context_manifests import lite_context_manifest_store
from qwenpaw.runtime.actions import lite_action_store
from qwenpaw.runtime.model_calls import lite_model_call_store
from qwenpaw.runtime.outcomes import lite_conversation_outcome_store
from qwenpaw.schemas import DataContent, Message
from qwenpaw.tasks.ledger import SQLiteExecutionLedger
from qwenpaw.tasks.results import verification_signal
from qwenpaw.tasks.service import TaskService


def _chat(chat_id: str, *, app_id: str | None = None) -> ChatSpec:
    meta = (
        {
            "pawapp": {
                "app_id": app_id,
                "agent_id": "datapaw",
            },
        }
        if app_id
        else {}
    )
    return ChatSpec(
        id=chat_id,
        session_id=f"console:{chat_id}",
        user_id="default",
        channel="console",
        meta=meta,
    )


def test_artifact_receipt_resolves_from_tool_result_link() -> None:
    artifact_id = "b6f55fc5-8b5a-470b-b7d1-f01f05b20c44"
    history = ChatHistory(
        messages=[
            Message(
                content=[
                    DataContent(
                        data={
                            "artifact_links": [
                                {
                                    "artifact_ref": {
                                        "artifact_id": artifact_id,
                                    },
                                    "artifact_receipt": "receipt-1",
                                },
                            ],
                        },
                    ),
                ],
            ),
        ],
    )

    assert (
        _artifact_receipt_from_history(
            history,
            UUID(artifact_id),
        )
        == "receipt-1"
    )


@pytest.mark.asyncio
async def test_list_chats_can_exclude_app_owned_dialogues():
    normal = _chat("normal")
    app_owned = _chat("app-owned", app_id="datapaw")
    manager = SimpleNamespace(
        list_chats=AsyncMock(return_value=[normal, app_owned]),
    )
    tracker = SimpleNamespace(get_status=AsyncMock(return_value="idle"))

    result = await list_chats(
        user_id=None,
        channel=None,
        archived=False,
        include_app_owned=False,
        mgr=manager,
        workspace=SimpleNamespace(task_tracker=tracker),
    )

    assert [chat.id for chat in result] == ["normal"]
    tracker.get_status.assert_awaited_once_with("normal")


@pytest.mark.asyncio
async def test_get_chat_hides_app_owned_dialogue_when_caller_opts_out():
    manager = SimpleNamespace(
        get_chat=AsyncMock(return_value=_chat("app-owned", app_id="datapaw")),
    )

    with pytest.raises(HTTPException) as raised:
        await get_chat(
            chat_id="app-owned",
            include_app_owned=False,
            mgr=manager,
            session=SimpleNamespace(),
            workspace=SimpleNamespace(),
        )

    assert raised.value.status_code == 404


@pytest.mark.asyncio
async def test_get_chat_status_uses_tracker_without_loading_chat_persistence():
    tracker = SimpleNamespace(get_status=AsyncMock(return_value="running"))
    workspace = SimpleNamespace(task_tracker=tracker)

    result = await get_chat_status(
        chat_id="chat-1",
        workspace=workspace,
    )

    assert result.status == "running"
    tracker.get_status.assert_awaited_once_with("chat-1")


@pytest.mark.asyncio
async def test_get_chat_status_treats_unknown_run_key_as_idle():
    tracker = SimpleNamespace(get_status=AsyncMock(return_value="idle"))

    result = await get_chat_status(
        chat_id="missing",
        workspace=SimpleNamespace(task_tracker=tracker),
    )

    assert result.status == "idle"
    tracker.get_status.assert_awaited_once_with("missing")


@pytest.mark.asyncio
async def test_list_chat_actions_returns_privacy_safe_records(tmp_path):
    chat_id = "chat-actions"
    manager = SimpleNamespace(
        get_chat=AsyncMock(return_value=_chat(chat_id)),
    )
    invocation_id = uuid4()
    request = ActionRequest(
        invocation_id=invocation_id,
        correlation_id=invocation_id,
        conversation_id=chat_id,
        registry_generation=7,
        capability_id="qwenpaw.system.workspace-tools",
        kind=ActionKind.BROWSER,
        action_name="browser",
        redacted_arguments={"code": "private browser program"},
        arguments_hash="sha256:" + "a" * 64,
        idempotency_key="browser:legacy:call-1",
    )
    await lite_action_store(tmp_path).begin(request)

    records = await list_chat_actions(
        chat_id=chat_id,
        limit=20,
        mgr=manager,
        workspace=SimpleNamespace(workspace_dir=tmp_path),
    )

    [record] = records
    assert record.request.chat_id == chat_id
    assert record.request.conversation_id == chat_id
    assert record.request.kind is ActionKind.BROWSER
    assert record.request.arguments == {}
    assert record.request.redacted_arguments == {
        "code": "[CONTENT OMITTED]",
    }
    assert record.request.arguments_hash != "sha256:" + "a" * 64
    payload = record.model_dump(mode="json")
    assert payload["request"]["chat_id"] == chat_id
    assert "conversation_id" not in payload["request"]
    assert "arguments" not in payload["request"]
    manager.get_chat.assert_awaited_once_with(chat_id)


@pytest.mark.asyncio
async def test_list_chat_actions_rejects_unknown_chat(tmp_path):
    manager = SimpleNamespace(get_chat=AsyncMock(return_value=None))

    with pytest.raises(HTTPException) as raised:
        await list_chat_actions(
            chat_id=str(uuid4()),
            limit=20,
            mgr=manager,
            workspace=SimpleNamespace(workspace_dir=tmp_path),
        )

    assert raised.value.status_code == 404
    assert not (tmp_path / ".qwenpaw").exists()


@pytest.mark.asyncio
async def test_list_chat_model_calls_returns_content_free_records(tmp_path):
    chat_id = "chat-model-calls"
    manager = SimpleNamespace(
        get_chat=AsyncMock(return_value=_chat(chat_id)),
    )
    invocation_id = uuid4()
    manifest_id = uuid4()
    attempt_id = uuid4()
    decision_id = uuid4()
    route = RouteDecision(
        route_decision_id=decision_id,
        attempt_id=attempt_id,
        invocation_id=invocation_id,
        correlation_id=invocation_id,
        conversation_id=chat_id,
        registry_generation=7,
        context_manifest_id=manifest_id,
        model_call_index=1,
        attempt_index=1,
        provider_id="provider-a",
        model_id="model-a",
        reason=ModelRouteReason.PRIMARY,
    )
    attempt = ModelCallAttempt(
        attempt_id=attempt_id,
        route_decision_id=decision_id,
        invocation_id=invocation_id,
        correlation_id=invocation_id,
        conversation_id=chat_id,
        registry_generation=7,
        context_manifest_id=manifest_id,
        model_call_index=1,
        attempt_index=1,
        provider_id="provider-a",
        model_id="model-a",
    )
    await lite_model_call_store(tmp_path).begin(route, attempt)

    records = await list_chat_model_calls(
        chat_id=chat_id,
        limit=20,
        mgr=manager,
        workspace=SimpleNamespace(workspace_dir=tmp_path),
    )

    [record] = records
    assert record.route.reason is ModelRouteReason.PRIMARY
    assert record.attempt.context_manifest_id == manifest_id
    assert record.result is None
    payload = record.model_dump(mode="json")
    assert payload["route"]["chat_id"] == chat_id
    assert "conversation_id" not in payload["route"]
    assert payload["attempt"]["chat_id"] == chat_id
    assert "conversation_id" not in payload["attempt"]
    assert "messages" not in record.model_dump_json()
    manager.get_chat.assert_awaited_once_with(chat_id)


@pytest.mark.asyncio
async def test_list_chat_model_calls_rejects_unknown_chat(tmp_path):
    manager = SimpleNamespace(get_chat=AsyncMock(return_value=None))

    with pytest.raises(HTTPException) as raised:
        await list_chat_model_calls(
            chat_id=str(uuid4()),
            limit=20,
            mgr=manager,
            workspace=SimpleNamespace(workspace_dir=tmp_path),
        )

    assert raised.value.status_code == 404
    assert not (tmp_path / ".qwenpaw").exists()


@pytest.mark.asyncio
async def test_list_chat_capability_locks_returns_owned_releases(
    tmp_path,
) -> None:
    chat_id = "chat-capability-locks"
    manager = SimpleNamespace(
        get_chat=AsyncMock(return_value=_chat(chat_id)),
    )
    invocation_id = uuid4()
    releases = (
        CapabilityRelease(
            capability_id="example.tools.provider",
            slot="tool.provider",
            provider_id="example.tools",
            provider_kind=CapabilityProviderKind.PLUGIN,
            version="1.2.0",
            restart_policy=RestartPolicy.HOT,
            descriptor_hash=f"sha256:{'a' * 64}",
        ),
    )
    manifest = CapabilityLockManifest.create(
        invocation_id=invocation_id,
        correlation_id=invocation_id,
        agent_id="default",
        chat_id=chat_id,
        registry_generation=7,
        releases=releases,
    )
    await lite_capability_lock_store(tmp_path).append(manifest)

    records = await list_chat_capability_locks(
        chat_id=chat_id,
        limit=20,
        mgr=manager,
        workspace=SimpleNamespace(workspace_dir=tmp_path),
    )

    assert records == [manifest]
    assert records[0].releases[0].provider_kind is (
        CapabilityProviderKind.PLUGIN
    )
    manager.get_chat.assert_awaited_once_with(chat_id)


@pytest.mark.asyncio
async def test_list_chat_capability_locks_rejects_unknown_chat(
    tmp_path,
) -> None:
    manager = SimpleNamespace(get_chat=AsyncMock(return_value=None))

    with pytest.raises(HTTPException) as raised:
        await list_chat_capability_locks(
            chat_id="missing-lock-chat",
            limit=20,
            mgr=manager,
            workspace=SimpleNamespace(workspace_dir=tmp_path),
        )

    assert raised.value.status_code == 404
    assert not (tmp_path / ".qwenpaw").exists()


@pytest.mark.asyncio
async def test_list_chat_context_manifests_returns_lock_reference(
    tmp_path,
) -> None:
    chat_id = "chat-context-manifests"
    manager = SimpleNamespace(
        get_chat=AsyncMock(return_value=_chat(chat_id)),
    )
    invocation_id = uuid4()
    lock_id = uuid4()
    manifest = ContextManifest(
        invocation_id=invocation_id,
        correlation_id=invocation_id,
        conversation_id=chat_id,
        registry_generation=7,
        capability_lock_id=lock_id,
        capability_lock_hash=f"sha256:{'c' * 64}",
        model_call_index=1,
        policy_id="qwenpaw.system.context.default",
        policy_version="1.0.0",
        total_size_bytes=0,
        total_estimated_tokens=0,
        disclosed_tool_count=0,
        manifest_hash=f"sha256:{'d' * 64}",
    )
    await lite_context_manifest_store(tmp_path).append(manifest)

    records = await list_chat_context_manifests(
        chat_id=chat_id,
        limit=20,
        mgr=manager,
        workspace=SimpleNamespace(workspace_dir=tmp_path),
    )

    assert records == [manifest]
    assert records[0].capability_lock_id == lock_id
    assert "content" not in records[0].model_dump_json()


@pytest.mark.asyncio
async def test_list_chat_context_manifests_rejects_unknown_chat(
    tmp_path,
) -> None:
    manager = SimpleNamespace(get_chat=AsyncMock(return_value=None))

    with pytest.raises(HTTPException) as raised:
        await list_chat_context_manifests(
            chat_id="missing-context-chat",
            limit=20,
            mgr=manager,
            workspace=SimpleNamespace(workspace_dir=tmp_path),
        )

    assert raised.value.status_code == 404
    assert not (tmp_path / ".qwenpaw").exists()


@pytest.mark.asyncio
async def test_list_chat_observations_uses_owned_projection(tmp_path):
    chat_id = "chat-observations"
    manager = SimpleNamespace(
        get_chat=AsyncMock(return_value=_chat(chat_id)),
    )
    invocation_id = uuid4()
    attempt_id = uuid4()
    decision_id = uuid4()
    route = RouteDecision(
        route_decision_id=decision_id,
        attempt_id=attempt_id,
        invocation_id=invocation_id,
        correlation_id=invocation_id,
        conversation_id=chat_id,
        registry_generation=7,
        context_manifest_id=uuid4(),
        model_call_index=1,
        attempt_index=1,
        provider_id="provider-a",
        model_id="model-a",
        reason=ModelRouteReason.PRIMARY,
    )
    attempt = ModelCallAttempt(
        attempt_id=attempt_id,
        route_decision_id=decision_id,
        invocation_id=invocation_id,
        correlation_id=invocation_id,
        conversation_id=chat_id,
        registry_generation=7,
        context_manifest_id=route.context_manifest_id,
        model_call_index=1,
        attempt_index=1,
        provider_id="provider-a",
        model_id="model-a",
    )
    await lite_model_call_store(tmp_path).begin(route, attempt)
    control_store = SQLiteInvocationControl(tmp_path / "control.sqlite3")
    control_service = InvocationControlService(store=control_store)
    submitted = await control_store.submit(
        TurnSubmissionRequest(
            agent_id="default",
            conversation_id=chat_id,
            content="queued content",
            idempotency_key="queued-turn",
        ),
        expected_revision=0,
    )
    assert submitted.submission_id is not None
    await control_service.cancel_queued(
        agent_id="default",
        conversation_id=chat_id,
        submission_id=submitted.submission_id,
        idempotency_key="cancel-turn",
    )
    task_ledger = SQLiteExecutionLedger(
        tmp_path / ".qwenpaw" / "lite" / "tasks.db",
    )
    await task_ledger.initialize()
    task_service = TaskService(
        store=task_ledger,
        registry_generation=7,
    )
    task = await task_service.create_task(
        objective="Verify Chat observation",
        agent_id="default",
        metadata={"conversation_id": chat_id},
    )
    await task_service.plan_task(
        task.task_id,
        steps=(
            PlanStep(
                title="Verify",
                objective="Verify Chat observation",
            ),
        ),
    )
    _, run = await task_service.start_task(
        task.task_id,
        runner_id="qwenpaw.runner.tests",
    )
    artifact = ArtifactRef(
        kind="report.markdown",
        uri="qwenpaw-artifact://sha256/" + "c" * 64,
        media_type="text/markdown",
        content_hash="sha256:" + "c" * 64,
        size_bytes=128,
        metadata={"name": "private-observation.md"},
    )
    evidence = EvidenceRef(
        artifact_id=artifact.artifact_id,
        claim="PRIVATE OBSERVATION CLAIM",
        producer="qwenpaw.runner.tests",
    )
    await task_service.record_runner_signal(
        task.task_id,
        run.run_id,
        RunnerSignal(
            event_type="artifact.created",
            source="qwenpaw.runner.tests",
            artifact_refs=(artifact,),
            evidence_refs=(evidence,),
        ),
    )
    verification = VerificationResult(
        task_id=task.task_id,
        run_id=run.run_id,
        verifier_id="qwenpaw.verifier.tests",
        status=VerificationStatus.PASSED,
    )
    await task_service.record_runner_signal(
        task.task_id,
        run.run_id,
        verification_signal(verification),
    )

    observations = await list_chat_observations(
        chat_id=chat_id,
        limit=20,
        mgr=manager,
        workspace=SimpleNamespace(
            workspace_dir=tmp_path,
            agent_id="default",
            interaction_service=None,
            invocation_control=control_service,
        ),
    )

    assert {item.stage.value for item in observations} == {
        "intent",
        "policy",
        "execution",
        "evidence",
    }
    assert {item.source.source_type for item in observations} == {
        "qwenpaw.model.route-decision",
        "qwenpaw.model.attempt",
        "qwenpaw.control.command",
        "qwenpaw.control.receipt",
        "qwenpaw.control.submission",
        "qwenpaw.task.artifact-record",
        "qwenpaw.task.evidence-record",
        "qwenpaw.task.verification-record",
    }
    submission_observations = [
        item
        for item in observations
        if item.source.source_type == "qwenpaw.control.submission"
    ]
    assert {item.status.value for item in submission_observations} == {
        "recorded",
        "cancelled",
    }
    assert all(
        "queued content" not in item.model_dump_json()
        for item in submission_observations
    )
    [verification_observation] = [
        item
        for item in observations
        if item.source.source_type == "qwenpaw.task.verification-record"
    ]
    assert verification_observation.task_id == task.task_id
    assert verification_observation.run_id == run.run_id
    assert verification_observation.registry_generation == 7
    assert verification_observation.facts["verification_id"] == str(
        verification.verification_id,
    )
    task_results = [
        item
        for item in observations
        if item.source.source_type
        in {
            "qwenpaw.task.artifact-record",
            "qwenpaw.task.evidence-record",
        }
    ]
    assert len(task_results) == 2
    assert all(item.task_id == task.task_id for item in task_results)
    serialized = "".join(item.model_dump_json() for item in task_results)
    assert "PRIVATE OBSERVATION CLAIM" not in serialized
    assert "private-observation.md" not in serialized
    assert "qwenpaw-artifact://" not in serialized
    manager.get_chat.assert_awaited_once_with(chat_id)


@pytest.mark.asyncio
async def test_list_chat_observations_rejects_unknown_chat(tmp_path):
    manager = SimpleNamespace(get_chat=AsyncMock(return_value=None))

    with pytest.raises(HTTPException) as raised:
        await list_chat_observations(
            chat_id=str(uuid4()),
            limit=20,
            mgr=manager,
            workspace=SimpleNamespace(
                workspace_dir=tmp_path,
                agent_id="default",
                interaction_service=None,
                invocation_control=None,
            ),
        )

    assert raised.value.status_code == 404
    assert not (tmp_path / ".qwenpaw").exists()


@pytest.mark.asyncio
async def test_page_chat_observations_maps_invalid_cursor_to_400(tmp_path):
    chat_id = "chat-invalid-observation-cursor"
    manager = SimpleNamespace(
        get_chat=AsyncMock(return_value=_chat(chat_id)),
    )

    with pytest.raises(HTTPException) as raised:
        await page_chat_observations(
            chat_id=chat_id,
            limit=20,
            cursor="not-a-cursor",
            mgr=manager,
            workspace=SimpleNamespace(
                workspace_dir=tmp_path,
                agent_id="default",
                interaction_service=None,
                invocation_control=None,
            ),
        )

    assert raised.value.status_code == 400


@pytest.mark.asyncio
async def test_page_chat_trajectory_returns_owned_outcome(tmp_path):
    chat_id = "chat-trajectory"
    correlation_id = uuid4()
    manager = SimpleNamespace(
        get_chat=AsyncMock(return_value=_chat(chat_id)),
    )
    outcomes = lite_conversation_outcome_store(tmp_path)
    outcome = ConversationOutcome(
        agent_id="default",
        conversation_id=chat_id,
        correlation_id=correlation_id,
        status=ConversationOutcomeStatus.ACHIEVED,
        producer_id="qwenpaw.system.tests",
        summary="The requested result is verified.",
    )
    await outcomes.append(outcome)
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="default",
        interaction_service=None,
        invocation_control=None,
        conversation_outcome_store=outcomes,
    )

    page = await page_chat_trajectory(
        chat_id=chat_id,
        correlation_id=correlation_id,
        limit=20,
        cursor=None,
        mgr=manager,
        workspace=workspace,
    )

    assert page.conversation_id == chat_id
    assert page.correlation_id == correlation_id
    assert len(page.items) == 1
    assert page.items[0].category.value == "outcome"
    assert page.items[0].source.source_id == str(outcome.outcome_id)
