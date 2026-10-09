# -*- coding: utf-8 -*-
"""Model Call contracts use ChatSpec.id across legacy persistence."""

from uuid import uuid4

from qwenpaw.kernel import (
    ContextManifest,
    ModelCallAttempt,
    ModelCallResult,
    ModelCallStatus,
    ModelRouteReason,
    RouteDecision,
)


def test_model_call_models_serialize_canonical_chat_identity() -> None:
    invocation_id = uuid4()
    correlation_id = uuid4()
    manifest_id = uuid4()
    decision_id = uuid4()
    attempt_id = uuid4()
    models = (
        ContextManifest(
            manifest_id=manifest_id,
            invocation_id=invocation_id,
            correlation_id=correlation_id,
            conversation_id="chat-legacy",
            registry_generation=1,
            model_call_index=1,
            policy_id="qwenpaw.system.context-policy",
            policy_version="1",
            total_size_bytes=0,
            total_estimated_tokens=0,
            disclosed_tool_count=0,
            manifest_hash=f"sha256:{'a' * 64}",
        ),
        RouteDecision(
            route_decision_id=decision_id,
            attempt_id=attempt_id,
            invocation_id=invocation_id,
            correlation_id=correlation_id,
            conversation_id="chat-legacy",
            registry_generation=1,
            context_manifest_id=manifest_id,
            model_call_index=1,
            attempt_index=1,
            provider_id="provider-a",
            model_id="model-a",
            reason=ModelRouteReason.PRIMARY,
        ),
        ModelCallAttempt(
            attempt_id=attempt_id,
            route_decision_id=decision_id,
            invocation_id=invocation_id,
            correlation_id=correlation_id,
            conversation_id="chat-legacy",
            registry_generation=1,
            context_manifest_id=manifest_id,
            model_call_index=1,
            attempt_index=1,
            provider_id="provider-a",
            model_id="model-a",
        ),
        ModelCallResult(
            attempt_id=attempt_id,
            invocation_id=invocation_id,
            conversation_id="chat-legacy",
            status=ModelCallStatus.SUCCEEDED,
        ),
    )

    for model in models:
        payload = model.model_dump(mode="json")
        assert payload["chat_id"] == "chat-legacy"
        assert "conversation_id" not in payload

        legacy_payload = dict(payload)
        legacy_payload["conversation_id"] = legacy_payload.pop("chat_id")
        restored = type(model).model_validate(legacy_payload)
        assert restored.chat_id == "chat-legacy"
        assert restored.conversation_id == "chat-legacy"
