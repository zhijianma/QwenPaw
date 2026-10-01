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
    ModelCallAttempt,
    ModelCallResult,
    ModelCallStatus,
    ModelRouteReason,
    ObservationStage,
    ObservationStatus,
    ToolEffect,
    RouteDecision,
)
from qwenpaw.runtime.model_calls import lite_model_call_store
from qwenpaw.runtime.actions import lite_action_store
from qwenpaw.runtime.observations import lite_observation_projection


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

    assert [item.stage for item in observations] == [
        ObservationStage.EVIDENCE,
        ObservationStage.INTENT,
    ]
    assert observations[0].status is ObservationStatus.SUCCEEDED
    assert observations[0].source.source_type == "qwenpaw.action.result"
    assert observations[1].facts["capability_id"] == "example.export"
    assert observations[1].status is ObservationStatus.RECORDED
    assert "redacted_arguments" not in observations[1].facts
