# -*- coding: utf-8 -*-
"""Tests for semantic projections over durable runtime facts."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from qwenpaw.kernel import (
    ActionKind,
    ActionRequest,
    ActionResult,
    ActionStatus,
    ActorRef,
    ActorType,
    ControlCommand,
    ControlCommandKind,
    ControlCommandStatus,
    ControlReceipt,
    ControlRecord,
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
    ObservationStage,
    ObservationStatus,
    SteerSafePoint,
    ToolEffect,
    RouteDecision,
)
from qwenpaw.runtime.model_calls import lite_model_call_store
from qwenpaw.runtime.actions import lite_action_store
from qwenpaw.runtime.observations import lite_observation_projection
from qwenpaw.runtime.compactions import lite_compaction_store
from qwenpaw.interactions import InteractionService


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
        async def list_for_conversation(self, **_kwargs):
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
