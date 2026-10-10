# -*- coding: utf-8 -*-
"""Action contracts use ChatSpec.id without breaking legacy records."""

from uuid import uuid4

from qwenpaw.kernel import (
    ActionApprovalLink,
    ActionExecutionContext,
    ActionIdempotencyMode,
    ActionKind,
    ActionRequest,
    ActionResult,
    ActionRetryInputCheckpoint,
    ActionStatus,
    ApprovalSource,
    CommittedActionItem,
    ToolDefinition,
)


def _legacy_mcp_tool() -> None:
    """Provide a stable callable for ActionKind compatibility coverage."""


def test_legacy_mcp_action_kind_normalizes_to_driver() -> None:
    assert ActionKind.MCP is ActionKind.DRIVER
    assert ActionKind("mcp") is ActionKind.DRIVER

    definition = ToolDefinition(
        function=_legacy_mcp_tool,
        name="_legacy_mcp_tool",
        tool_type="network",
        action_kind="mcp",
    )

    assert definition.action_kind is ActionKind.DRIVER
    payload = definition.model_dump(mode="json")
    assert payload["action_kind"] == "driver"
    assert all(kind.value != "mcp" for kind in ActionKind)


def test_action_models_serialize_canonical_chat_identity() -> None:
    invocation_id = uuid4()
    action_id = uuid4()
    correlation_id = uuid4()
    digest = f"sha256:{'a' * 64}"
    models = (
        ActionApprovalLink(
            action_id=action_id,
            invocation_id=invocation_id,
            conversation_id="chat-legacy",
            approval_id=uuid4(),
            source=ApprovalSource.TOOL,
        ),
        ActionRequest(
            action_id=action_id,
            invocation_id=invocation_id,
            correlation_id=correlation_id,
            conversation_id="chat-legacy",
            registry_generation=1,
            capability_id="qwenpaw.system.test-tool",
            kind=ActionKind.TOOL,
            action_name="test_action",
            arguments_hash=digest,
            idempotency_key="action-test",
        ),
        ActionExecutionContext(
            action_id=action_id,
            invocation_id=invocation_id,
            correlation_id=correlation_id,
            conversation_id="chat-legacy",
            capability_id="qwenpaw.system.test-tool",
            idempotency_mode=ActionIdempotencyMode.UNDECLARED,
            idempotency_key="action-test",
            executor_item_id="call-1",
            attempt=1,
        ),
        ActionRetryInputCheckpoint(
            checkpoint_id=uuid4(),
            action_id=action_id,
            retry_root_action_id=action_id,
            invocation_id=invocation_id,
            agent_id="default",
            conversation_id="chat-legacy",
            correlation_id=correlation_id,
            registry_generation=1,
            capability_id="qwenpaw.system.test-tool",
            kind=ActionKind.TOOL,
            action_name="test_action",
            arguments_hash=digest,
            next_attempt=2,
        ),
        ActionResult(
            action_id=action_id,
            invocation_id=invocation_id,
            conversation_id="chat-legacy",
            status=ActionStatus.SUCCEEDED,
            observation_digest=digest,
        ),
        CommittedActionItem(
            action_id=action_id,
            invocation_id=invocation_id,
            conversation_id="chat-legacy",
            executor_item_id="call-1",
            observation_digest=digest,
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
