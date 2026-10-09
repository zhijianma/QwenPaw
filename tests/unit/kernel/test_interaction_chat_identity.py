# -*- coding: utf-8 -*-
"""Interaction contracts use ChatSpec.id across legacy persistence."""

from uuid import uuid4

from qwenpaw.kernel import (
    InteractionKind,
    InteractionMode,
    InteractionRequest,
    UserInputReason,
)


def test_interaction_serializes_canonical_chat_identity() -> None:
    interaction = InteractionRequest(
        kind=InteractionKind.USER_INPUT,
        mode=InteractionMode.BLOCKING,
        agent_id="default",
        conversation_id="chat-legacy",
        invocation_id=uuid4(),
        user_input_reason=UserInputReason.MATERIAL_PREFERENCE,
        title="Choose output",
        prompt="Which format?",
    )

    payload = interaction.model_dump(mode="json")
    assert payload["chat_id"] == "chat-legacy"
    assert "conversation_id" not in payload

    legacy_payload = dict(payload)
    legacy_payload["conversation_id"] = legacy_payload.pop("chat_id")
    restored = InteractionRequest.model_validate(legacy_payload)
    assert restored.chat_id == "chat-legacy"
    assert restored.conversation_id == "chat-legacy"
