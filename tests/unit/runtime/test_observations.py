# -*- coding: utf-8 -*-
"""Tests for semantic projections over durable runtime facts."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from qwenpaw.kernel import (
    AcceptanceVerification,
    ActionKind,
    ActionRequest,
    ActionResult,
    ActionStatus,
    ActorRef,
    ActorType,
    ArtifactRecord,
    ArtifactRef,
    ArtifactStatus,
    ConversationTaskResultRecords,
    ControlCommand,
    ControlCommandKind,
    ControlCommandStatus,
    ControlReceipt,
    ControlRecord,
    EvidenceRef,
    EvidenceRecord,
    CompactionRecord,
    CompactionStatus,
    CompactionTrigger,
    InteractionKind,
    InteractionMode,
    InteractionOption,
    InteractionRequest,
    InteractionResponse,
    ModelCallAttempt,
    ModelCallResult,
    ModelCallStatus,
    ModelRouteReason,
    ObservationCategory,
    ObservationSource,
    ObservationStage,
    ObservationStatus,
    RuntimeObservation,
    SubmissionStatus,
    SteerSafePoint,
    ToolEffect,
    RouteDecision,
    VerificationRecord,
    VerificationResult,
    VerificationStatus,
    TurnSubmissionRequest,
)
from qwenpaw.invocation_control import (
    InvocationControlService,
    SQLiteInvocationControl,
)
from qwenpaw.runtime.model_calls import lite_model_call_store
from qwenpaw.runtime.actions import lite_action_store
from qwenpaw.runtime.observation_index import (
    LiteObservationIndex,
    ObservationCursorError,
)
from qwenpaw.runtime.observations import (
    LiteObservationProjection,
    lite_observation_projection,
)
from qwenpaw.runtime.compactions import lite_compaction_store
from qwenpaw.interactions import InteractionService
from qwenpaw.tasks.conversation_artifacts import (
    conversation_artifact_receipts,
)


@pytest.mark.asyncio
async def test_model_call_projects_policy_execution_and_evidence(
    tmp_path,
) -> None:
    conversation_id = "chat-1"
    invocation_id = uuid4()
    attempt_id = uuid4()
    route_id = uuid4()
    manifest_id = uuid4()
    started_at = datetime.now(timezone.utc)
    route = RouteDecision(
        route_decision_id=route_id,
        attempt_id=attempt_id,
        invocation_id=invocation_id,
        correlation_id=invocation_id,
        conversation_id=conversation_id,
        registry_generation=5,
        context_manifest_id=manifest_id,
        model_call_index=1,
        attempt_index=1,
        provider_id="provider-a",
        model_id="model-a",
        requested_provider_id="provider-a",
        requested_model_id="model-a",
        reason=ModelRouteReason.PRIMARY,
        decided_at=started_at,
    )
    attempt = ModelCallAttempt(
        attempt_id=attempt_id,
        route_decision_id=route_id,
        invocation_id=invocation_id,
        correlation_id=invocation_id,
        conversation_id=conversation_id,
        registry_generation=5,
        context_manifest_id=manifest_id,
        model_call_index=1,
        attempt_index=1,
        provider_id="provider-a",
        model_id="model-a",
        adapter_id="example.Adapter",
        adapter_version="1.0.0",
        started_at=started_at + timedelta(milliseconds=1),
    )
    result = ModelCallResult(
        attempt_id=attempt_id,
        invocation_id=invocation_id,
        conversation_id=conversation_id,
        status=ModelCallStatus.SUCCEEDED,
        emitted_content=True,
        input_tokens=11,
        output_tokens=7,
        cost_micros=None,
        cost_unknown=True,
        completed_at=started_at + timedelta(seconds=1),
    )
    store = lite_model_call_store(tmp_path)
    await store.begin(route, attempt)
    await store.complete(result)

    observations = await lite_observation_projection(
        tmp_path,
    ).list_for_conversation(conversation_id)

    assert [item.stage for item in observations] == [
        ObservationStage.EVIDENCE,
        ObservationStage.EXECUTION,
        ObservationStage.POLICY,
    ]
    assert observations[0].status is ObservationStatus.SUCCEEDED
    assert observations[0].facts["cost_unknown"] is True
    assert observations[1].status is ObservationStatus.STARTED
    assert observations[2].facts["reason"] == "primary"
    assert "messages" not in "".join(
        item.model_dump_json() for item in observations
    )


@pytest.mark.asyncio
async def test_projection_limit_applies_to_observations(tmp_path) -> None:
    projection = lite_observation_projection(tmp_path)

    assert await projection.list_for_conversation("chat-1", limit=1) == []
    with pytest.raises(ValueError, match="limit must be between"):
        await projection.list_for_conversation("chat-1", limit=0)


@pytest.mark.asyncio
async def test_index_reconciles_stale_derived_pointers(tmp_path) -> None:
    conversation_id = "chat-reconcile"
    index = LiteObservationIndex(tmp_path / "observations.sqlite3")
    observation = RuntimeObservation(
        observation_id=uuid4(),
        category=ObservationCategory.CONTROL,
        stage=ObservationStage.INTENT,
        status=ObservationStatus.RECORDED,
        source=ObservationSource(
            source_type="qwenpaw.control.retired-source",
            source_id=str(uuid4()),
        ),
        conversation_id=conversation_id,
        title="Retired derived source",
        occurred_at=datetime.now(timezone.utc),
    )
    populated, _ = await index.sync_and_page(
        conversation_id,
        (observation,),
        limit=10,
        cursor=None,
    )

    reconciled, cursor = await index.sync_and_page(
        conversation_id,
        (),
        limit=10,
        cursor=None,
    )

    assert [item.observation_id for item in populated] == [
        str(observation.observation_id),
    ]
    assert reconciled == ()
    assert cursor is None


@pytest.mark.asyncio
async def test_submission_projects_immutable_intent_and_terminal_evidence(
    tmp_path,
) -> None:
    conversation_id = "chat-submission"
    correlation_id = uuid4()
    store = SQLiteInvocationControl(tmp_path / "control.sqlite3")
    control = InvocationControlService(store=store)
    receipt = await store.submit(
        TurnSubmissionRequest(
            agent_id="default",
            conversation_id=conversation_id,
            content="private user request",
            idempotency_key="submission-1",
            correlation_id=correlation_id,
        ),
        expected_revision=0,
    )
    projection = lite_observation_projection(
        tmp_path,
        agent_id="default",
        submissions=control,
    )

    queued = await projection.list_for_conversation(conversation_id)

    assert len(queued) == 1
    assert queued[0].stage is ObservationStage.INTENT
    assert queued[0].correlation_id == correlation_id
    assert "private user request" not in queued[0].model_dump_json()

    invocation_id = uuid4()
    admitted = await store.claim_next(
        agent_id="default",
        conversation_id=conversation_id,
        invocation_id=invocation_id,
    )
    assert admitted is not None
    running = await store.transition_submission(
        admitted.submission_id,
        invocation_id=invocation_id,
        target=SubmissionStatus.RUNNING,
        expected_revision=admitted.revision,
    )
    while_running = await projection.list_for_conversation(conversation_id)
    assert len(while_running) == 1

    await store.transition_submission(
        running.submission_id,
        invocation_id=invocation_id,
        target=SubmissionStatus.SUCCEEDED,
        expected_revision=running.revision,
    )
    terminal = await projection.list_for_conversation(conversation_id)

    assert len(terminal) == 2
    evidence = next(
        item
        for item in terminal
        if item.stage is ObservationStage.EVIDENCE
    )
    assert evidence.status is ObservationStatus.SUCCEEDED
    assert evidence.invocation_id == invocation_id
    assert evidence.correlation_id == correlation_id
    assert evidence.facts["submission_status"] == "succeeded"
    assert evidence.source.source_id == str(receipt.submission_id)
    await control.close()


@pytest.mark.asyncio
async def test_conversation_artifact_projects_content_safe_registry_facts(
    tmp_path,
) -> None:
    conversation_id = "chat-artifact"
    invocation_id = uuid4()
    correlation_id = uuid4()
    artifact = ArtifactRef(
        kind="report.markdown",
        uri="qwenpaw-artifact://sha256/" + "a" * 64,
        media_type="text/markdown",
        content_hash="sha256:" + "a" * 64,
        size_bytes=42,
        metadata={"name": "secret-name.md"},
    )
    evidence = EvidenceRef(
        artifact_id=artifact.artifact_id,
        claim="Sensitive internal verification claim",
        producer="example.report-tools.export",
    )
    receipts = conversation_artifact_receipts(tmp_path)
    receipt_id = await receipts.create_owned(
        artifact,
        evidence,
        chat_id=conversation_id,
        invocation_id=invocation_id,
        correlation_id=correlation_id,
        registry_generation=9,
    )

    observations = await lite_observation_projection(
        tmp_path,
        conversation_artifacts=receipts,
    ).list_for_conversation(conversation_id)

    assert {item.category for item in observations} == {
        ObservationCategory.ARTIFACT,
        ObservationCategory.EVIDENCE,
    }
    assert all(item.invocation_id == invocation_id for item in observations)
    assert all(
        item.correlation_id == correlation_id for item in observations
    )
    assert all(item.registry_generation == 9 for item in observations)
    assert all(item.source.source_id == receipt_id for item in observations)
    serialized = "".join(item.model_dump_json() for item in observations)
    assert "Sensitive internal verification claim" not in serialized
    assert "secret-name.md" not in serialized
    assert "qwenpaw-artifact://" not in serialized


@pytest.mark.asyncio
async def test_task_results_replace_duplicate_chat_receipt_with_causality(
    tmp_path,
) -> None:
    conversation_id = "chat-task-results"
    now = datetime.now(timezone.utc)
    task_id = uuid4()
    run_id = uuid4()
    event_id = uuid4()
    correlation_id = uuid4()
    artifact = ArtifactRef(
        kind="report.markdown",
        uri="qwenpaw-artifact://sha256/" + "b" * 64,
        media_type="text/markdown",
        content_hash="sha256:" + "b" * 64,
        size_bytes=84,
        metadata={"name": "private-task-report.md"},
    )
    evidence = EvidenceRef(
        artifact_id=artifact.artifact_id,
        claim="PRIVATE TASK CLAIM",
        producer="qwenpaw.tests.report",
        captured_at=now,
    )
    await conversation_artifact_receipts(tmp_path).create_owned(
        artifact,
        evidence,
        chat_id=conversation_id,
        correlation_id=correlation_id,
    )
    snapshot = ConversationTaskResultRecords(
        conversation_id=conversation_id,
        artifacts=(
            ArtifactRecord(
                artifact=artifact,
                task_id=task_id,
                run_id=run_id,
                event_id=event_id,
                correlation_id=correlation_id,
                version=2,
                status=ArtifactStatus.READY,
                producer="qwenpaw.tests.report",
                created_at=now,
            ),
        ),
        evidence=(
            EvidenceRecord(
                evidence=evidence,
                task_id=task_id,
                run_id=run_id,
                event_id=event_id,
                correlation_id=correlation_id,
                source="qwenpaw.tests.report",
            ),
        ),
    )

    class History:
        async def read_for_conversation(self, chat_id):
            assert chat_id == conversation_id
            return snapshot

    observations = await lite_observation_projection(
        tmp_path,
        conversation_artifacts=conversation_artifact_receipts(tmp_path),
        task_results=History(),
    ).list_for_conversation(conversation_id)

    assert len(observations) == 2
    assert {item.category for item in observations} == {
        ObservationCategory.ARTIFACT,
        ObservationCategory.EVIDENCE,
    }
    assert all(item.task_id == task_id for item in observations)
    assert all(item.run_id == run_id for item in observations)
    assert all(item.source.source_id == str(event_id) for item in observations)
    artifact_observation = next(
        item
        for item in observations
        if item.category is ObservationCategory.ARTIFACT
    )
    assert artifact_observation.facts["version"] == 2
    assert artifact_observation.facts["artifact_status"] == "ready"
    serialized = "".join(
        item.model_dump_json() for item in observations
    )
    assert "PRIVATE TASK CLAIM" not in serialized
    assert "private-task-report.md" not in serialized
    assert "qwenpaw-artifact://" not in serialized


@pytest.mark.asyncio
async def test_page_watermark_excludes_late_terminal_evidence(
    tmp_path,
) -> None:
    conversation_id = "chat-page-watermark"
    invocation_id = uuid4()
    attempt_id = uuid4()
    now = datetime.now(timezone.utc)
    route = RouteDecision(
        route_decision_id=uuid4(),
        attempt_id=attempt_id,
        invocation_id=invocation_id,
        correlation_id=invocation_id,
        conversation_id=conversation_id,
        registry_generation=3,
        context_manifest_id=uuid4(),
        model_call_index=1,
        attempt_index=1,
        provider_id="provider-a",
        model_id="model-a",
        reason=ModelRouteReason.PRIMARY,
        decided_at=now,
    )
    attempt = ModelCallAttempt(
        attempt_id=attempt_id,
        route_decision_id=route.route_decision_id,
        invocation_id=invocation_id,
        correlation_id=invocation_id,
        conversation_id=conversation_id,
        registry_generation=3,
        context_manifest_id=route.context_manifest_id,
        model_call_index=1,
        attempt_index=1,
        provider_id="provider-a",
        model_id="model-a",
        started_at=now + timedelta(milliseconds=1),
    )
    store = lite_model_call_store(tmp_path)
    await store.begin(route, attempt)
    projection = lite_observation_projection(tmp_path)

    first = await projection.page_for_conversation(
        conversation_id,
        limit=1,
    )
    assert first.next_cursor is not None
    assert first.items[0].stage is ObservationStage.EXECUTION

    await store.complete(
        ModelCallResult(
            attempt_id=attempt_id,
            invocation_id=invocation_id,
            conversation_id=conversation_id,
            status=ModelCallStatus.SUCCEEDED,
            completed_at=now + timedelta(milliseconds=2),
        ),
    )
    second = await projection.page_for_conversation(
        conversation_id,
        limit=1,
        cursor=first.next_cursor,
    )

    assert [item.stage for item in second.items] == [
        ObservationStage.POLICY,
    ]
    assert second.next_cursor is None
    fresh = await projection.page_for_conversation(
        conversation_id,
        limit=1,
    )
    assert fresh.items[0].stage is ObservationStage.EVIDENCE


@pytest.mark.asyncio
async def test_pages_order_same_time_cross_source_without_duplicates(
    tmp_path,
) -> None:
    conversation_id = "chat-page-tie"
    now = datetime.now(timezone.utc)
    invocation_id = uuid4()
    await lite_action_store(tmp_path).begin(
        ActionRequest(
            invocation_id=invocation_id,
            correlation_id=invocation_id,
            conversation_id=conversation_id,
            registry_generation=4,
            capability_id="example.read",
            kind=ActionKind.TOOL,
            action_name="read",
            arguments_hash=f"sha256:{'a' * 64}",
            effect=ToolEffect.NONE,
            idempotency_key="tie-action",
            requested_at=now,
        ),
    )
    await lite_compaction_store(tmp_path).append(
        CompactionRecord(
            agent_id="default",
            conversation_id=conversation_id,
            invocation_id=uuid4(),
            registry_generation=4,
            strategy_id="qwenpaw.context.scroll",
            trigger=CompactionTrigger.MANUAL,
            status=CompactionStatus.SUCCEEDED,
            before_message_count=3,
            after_message_count=1,
            evicted_messages=2,
            context_changed=True,
            started_at=now,
            completed_at=now,
        ),
    )
    projection = lite_observation_projection(tmp_path)
    observed = []
    cursor = None
    while True:
        page = await projection.page_for_conversation(
            conversation_id,
            limit=1,
            cursor=cursor,
        )
        observed.extend(page.items)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert len(observed) == 3
    assert len({item.observation_id for item in observed}) == 3
    assert [str(item.observation_id) for item in observed] == sorted(
        (str(item.observation_id) for item in observed),
        reverse=True,
    )


@pytest.mark.asyncio
async def test_page_rejects_cursor_from_another_conversation(
    tmp_path,
) -> None:
    now = datetime.now(timezone.utc)
    await lite_compaction_store(tmp_path).append(
        CompactionRecord(
            agent_id="default",
            conversation_id="chat-owner-a",
            invocation_id=uuid4(),
            registry_generation=2,
            strategy_id="qwenpaw.context.scroll",
            trigger=CompactionTrigger.MANUAL,
            status=CompactionStatus.SUCCEEDED,
            before_message_count=3,
            after_message_count=2,
            evicted_messages=1,
            context_changed=True,
            started_at=now,
            completed_at=now,
        ),
    )
    await lite_compaction_store(tmp_path).append(
        CompactionRecord(
            agent_id="default",
            conversation_id="chat-owner-a",
            invocation_id=uuid4(),
            registry_generation=2,
            strategy_id="qwenpaw.context.scroll",
            trigger=CompactionTrigger.MANUAL,
            status=CompactionStatus.SUCCEEDED,
            before_message_count=2,
            after_message_count=1,
            evicted_messages=1,
            context_changed=True,
            started_at=now,
            completed_at=now + timedelta(milliseconds=1),
        ),
    )
    projection = lite_observation_projection(tmp_path)
    first = await projection.page_for_conversation(
        "chat-owner-a",
        limit=1,
    )
    assert first.next_cursor is not None

    with pytest.raises(ObservationCursorError, match="owner mismatch"):
        await projection.page_for_conversation(
            "chat-owner-b",
            limit=1,
            cursor=first.next_cursor,
        )


@pytest.mark.asyncio
async def test_pagination_is_not_limited_to_first_thousand_sources(
    tmp_path,
) -> None:
    conversation_id = "chat-large-history"
    now = datetime.now(timezone.utc)
    records = tuple(
        CompactionRecord(
            agent_id="default",
            conversation_id=conversation_id,
            invocation_id=uuid4(),
            registry_generation=2,
            strategy_id="qwenpaw.context.scroll",
            trigger=CompactionTrigger.AUTOMATIC,
            status=CompactionStatus.SUCCEEDED,
            before_message_count=2,
            after_message_count=1,
            evicted_messages=1,
            context_changed=True,
            started_at=now + timedelta(microseconds=index),
            completed_at=now + timedelta(microseconds=index),
        )
        for index in range(1001)
    )

    class EmptyHistory:
        async def scan_for_conversation(self, *_args, **_kwargs):
            return ()

    class CompactionHistory:
        async def scan_for_conversation(self, *_args, **_kwargs):
            return records

    projection = LiteObservationProjection(
        EmptyHistory(),
        EmptyHistory(),
        CompactionHistory(),
        LiteObservationIndex(tmp_path / "observations.sqlite3"),
    )
    first = await projection.page_for_conversation(
        conversation_id,
        limit=1000,
    )
    assert len(first.items) == 1000
    assert first.next_cursor is not None
    second = await projection.page_for_conversation(
        conversation_id,
        limit=1000,
        cursor=first.next_cursor,
    )
    assert len(second.items) == 1
    assert second.next_cursor is None
    assert not (
        {item.observation_id for item in first.items}
        & {item.observation_id for item in second.items}
    )


@pytest.mark.asyncio
async def test_action_uses_the_same_observation_contract(tmp_path) -> None:
    conversation_id = "chat-actions"
    invocation_id = uuid4()
    action_id = uuid4()
    requested_at = datetime.now(timezone.utc)
    request = ActionRequest(
        action_id=action_id,
        invocation_id=invocation_id,
        correlation_id=invocation_id,
        conversation_id=conversation_id,
        registry_generation=8,
        capability_id="example.export",
        kind=ActionKind.TOOL,
        action_name="export_report",
        redacted_arguments={"path": "report.md"},
        arguments_hash=f"sha256:{'a' * 64}",
        effect=ToolEffect.LOCAL_WRITE,
        idempotency_key="action-1",
        requested_at=requested_at,
    )
    result = ActionResult(
        action_id=action_id,
        invocation_id=invocation_id,
        conversation_id=conversation_id,
        status=ActionStatus.SUCCEEDED,
        observation_digest=f"sha256:{'b' * 64}",
        completed_at=requested_at + timedelta(seconds=1),
    )
    store = lite_action_store(tmp_path)
    await store.begin(request)
    await store.complete(result)

    observations = await lite_observation_projection(
        tmp_path,
    ).list_for_conversation(conversation_id)

    assert observations[0].stage is ObservationStage.EVIDENCE
    assert {item.stage for item in observations[1:]} == {
        ObservationStage.INTENT,
        ObservationStage.POLICY,
    }
    assert observations[0].status is ObservationStatus.SUCCEEDED
    assert observations[0].source.source_type == "qwenpaw.action.result"
    intent = next(
        item
        for item in observations
        if item.category.value == "action"
        and item.stage is ObservationStage.INTENT
    )
    guardrail = next(
        item for item in observations if item.category.value == "guardrail"
    )
    assert intent.facts["capability_id"] == "example.export"
    assert intent.status is ObservationStatus.RECORDED
    assert guardrail.facts["policy_decision"] == "allow"
    assert "redacted_arguments" not in intent.facts
    assert "redacted_arguments" not in guardrail.facts


@pytest.mark.asyncio
async def test_interaction_projects_hitl_without_response_content(
    tmp_path,
) -> None:
    conversation_id = "chat-hitl"
    service = InteractionService(tmp_path / "interactions.sqlite3")
    request = InteractionRequest(
        kind=InteractionKind.USER_INPUT,
        mode=InteractionMode.BLOCKING,
        agent_id="default",
        conversation_id=conversation_id,
        invocation_id=uuid4(),
        title="Sensitive title",
        prompt="Private question body",
        options=(InteractionOption(option_id="safe", label="Private label"),),
    )
    await service.open(request)
    await service.resolve(
        InteractionResponse(
            interaction_id=request.interaction_id,
            idempotency_key="answer-1",
            expected_revision=1,
            actor=ActorRef(type=ActorType.USER, id="private-user"),
            selected_option_ids=("safe",),
            text="Private response body",
            values={"private": "structured answer"},
        ),
    )

    observations = await lite_observation_projection(
        tmp_path,
        agent_id="default",
        interactions=service,
    ).list_for_conversation(conversation_id)

    assert [item.category.value for item in observations] == ["hitl", "hitl"]
    assert observations[0].status is ObservationStatus.RESOLVED
    assert observations[0].facts["selected_option_ids"] == ["safe"]
    assert observations[1].status is ObservationStatus.RECORDED
    serialized = "".join(item.model_dump_json() for item in observations)
    assert "Private question body" not in serialized
    assert "Private response body" not in serialized
    assert "structured answer" not in serialized
    assert "private-user" not in serialized
    assert "Private label" not in serialized


@pytest.mark.asyncio
async def test_control_projects_safe_audit_without_instruction_content(
    tmp_path,
) -> None:
    conversation_id = "chat-control"
    invocation_id = uuid4()
    requested_at = datetime.now(timezone.utc)
    steer = ControlCommand(
        kind=ControlCommandKind.STEER,
        agent_id="default",
        conversation_id=conversation_id,
        idempotency_key="PRIVATE IDEMPOTENCY KEY",
        expected_revision=3,
        target_invocation_id=invocation_id,
        instruction="PRIVATE STEER CONTENT",
        requested_at=requested_at,
    )
    steer_receipt = ControlReceipt(
        command_id=steer.command_id,
        kind=steer.kind,
        status=ControlCommandStatus.APPLIED,
        agent_id=steer.agent_id,
        conversation_id=steer.conversation_id,
        revision=4,
        detail="PRIVATE RECEIPT DETAIL",
        applied_at_safe_point=SteerSafePoint.BEFORE_TOOL_BATCH,
        recorded_at=requested_at + timedelta(seconds=1),
    )
    interrupt = ControlCommand(
        kind=ControlCommandKind.INTERRUPT_CURRENT,
        agent_id="default",
        conversation_id=conversation_id,
        idempotency_key="interrupt-private",
        expected_revision=4,
        target_invocation_id=invocation_id,
        requested_at=requested_at + timedelta(seconds=2),
    )
    interrupt_receipt = ControlReceipt(
        command_id=interrupt.command_id,
        kind=interrupt.kind,
        status=ControlCommandStatus.ACCEPTED,
        agent_id=interrupt.agent_id,
        conversation_id=interrupt.conversation_id,
        revision=5,
        detail="PRIVATE INTERRUPT DETAIL",
        recorded_at=requested_at + timedelta(seconds=3),
    )

    class History:
        async def scan_for_conversation(self, **_kwargs):
            return (
                ControlRecord(command=steer, receipt=steer_receipt),
                ControlRecord(
                    command=interrupt,
                    receipt=interrupt_receipt,
                ),
            )

    observations = await lite_observation_projection(
        tmp_path,
        agent_id="default",
        controls=History(),
    ).list_for_conversation(conversation_id)

    assert len(observations) == 4
    assert {item.category for item in observations} == {
        ObservationCategory.CONTROL,
        ObservationCategory.INTERRUPT,
    }
    steer_evidence = next(
        item
        for item in observations
        if item.category is ObservationCategory.CONTROL
        and item.stage is ObservationStage.EVIDENCE
    )
    interrupt_evidence = next(
        item
        for item in observations
        if item.category is ObservationCategory.INTERRUPT
        and item.stage is ObservationStage.EVIDENCE
    )
    assert steer_evidence.status is ObservationStatus.APPLIED
    assert steer_evidence.facts["applied_at_safe_point"] == "before_tool_batch"
    assert interrupt_evidence.status is ObservationStatus.ACCEPTED
    intent = next(
        item
        for item in observations
        if item.category is ObservationCategory.CONTROL
        and item.stage is ObservationStage.INTENT
    )
    assert intent.facts["has_instruction"] is True
    serialized = "".join(item.model_dump_json() for item in observations)
    assert "PRIVATE STEER CONTENT" not in serialized
    assert "PRIVATE RECEIPT DETAIL" not in serialized
    assert "PRIVATE INTERRUPT DETAIL" not in serialized
    assert "PRIVATE IDEMPOTENCY KEY" not in serialized


@pytest.mark.asyncio
async def test_compaction_projects_content_free_evidence(tmp_path) -> None:
    conversation_id = "chat-compaction"
    now = datetime.now(timezone.utc)
    record = CompactionRecord(
        agent_id="default",
        conversation_id=conversation_id,
        invocation_id=uuid4(),
        correlation_id=uuid4(),
        registry_generation=9,
        strategy_id="qwenpaw.context.scroll",
        trigger=CompactionTrigger.OVERFLOW_RECOVERY,
        status=CompactionStatus.SUCCEEDED,
        before_message_count=12,
        after_message_count=4,
        evicted_messages=8,
        folded_items=2,
        context_changed=True,
        summary_changed=True,
        started_at=now,
        completed_at=now + timedelta(milliseconds=20),
    )
    await lite_compaction_store(tmp_path).append(record)

    observations = await lite_observation_projection(
        tmp_path,
    ).list_for_conversation(conversation_id)

    assert len(observations) == 1
    observation = observations[0]
    assert observation.category is ObservationCategory.COMPACTION
    assert observation.stage is ObservationStage.EVIDENCE
    assert observation.status is ObservationStatus.SUCCEEDED
    assert observation.facts == {
        "compaction_id": str(record.compaction_id),
        "strategy_id": "qwenpaw.context.scroll",
        "trigger": "overflow_recovery",
        "before_message_count": 12,
        "after_message_count": 4,
        "evicted_messages": 8,
        "folded_items": 2,
        "context_changed": True,
        "summary_changed": True,
        "error_code": None,
    }


@pytest.mark.asyncio
async def test_verification_projects_host_evidence_without_content(
    tmp_path,
) -> None:
    conversation_id = "chat-verification"
    now = datetime.now(timezone.utc)
    task_id = uuid4()
    run_id = uuid4()
    record = VerificationRecord(
        verification=VerificationResult(
            task_id=task_id,
            run_id=run_id,
            verifier_id="qwenpaw.verifier.tests",
            status=VerificationStatus.FAILED,
            acceptance=(
                AcceptanceVerification(
                    criterion="PRIVATE CRITERION",
                    passed=False,
                    reason="PRIVATE FAILURE REASON",
                ),
            ),
            metadata={"private": "PRIVATE METADATA"},
        ),
        task_id=task_id,
        run_id=run_id,
        event_id=uuid4(),
        invocation_id=uuid4(),
        correlation_id=uuid4(),
        registry_generation=12,
        source="qwenpaw.verifier.tests",
        occurred_at=now,
    )

    class History:
        async def scan_for_conversation(self, chat_id):
            assert chat_id == conversation_id
            return (record,)

    observations = await lite_observation_projection(
        tmp_path,
        verifications=History(),
    ).list_for_conversation(conversation_id)

    assert len(observations) == 1
    observation = observations[0]
    assert observation.category is ObservationCategory.VERIFICATION
    assert observation.stage is ObservationStage.EVIDENCE
    assert observation.status is ObservationStatus.FAILED
    assert observation.task_id == task_id
    assert observation.run_id == run_id
    assert observation.invocation_id == record.invocation_id
    assert observation.registry_generation == 12
    assert observation.occurred_at == now
    assert observation.facts == {
        "verification_id": str(record.verification.verification_id),
        "verifier_id": "qwenpaw.verifier.tests",
        "acceptance_total": 1,
        "acceptance_passed": 0,
        "acceptance_failed": 1,
        "artifact_count": 0,
        "evidence_count": 0,
    }
    serialized = observation.model_dump_json()
    assert "PRIVATE CRITERION" not in serialized
    assert "PRIVATE FAILURE REASON" not in serialized
    assert "PRIVATE METADATA" not in serialized
