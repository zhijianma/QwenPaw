# -*- coding: utf-8 -*-
"""Contract tests for canonical Conversation Outcome Chat identity."""

from uuid import uuid4

from pydantic import ValidationError
import pytest

from qwenpaw.kernel import (
    ConversationOutcome,
    ConversationOutcomeDeclaration,
    ConversationOutcomeRequest,
    ConversationOutcomeStatus,
)


def _legacy_payload(contract_type):
    contract = contract_type(
        agent_id="default",
        conversation_id="chat-legacy",
        correlation_id=uuid4(),
        status=ConversationOutcomeStatus.PARTIAL,
        producer_id="qwenpaw.system.tests",
        summary="A durable partial outcome.",
    )
    payload = contract.model_dump(mode="json")
    assert payload["chat_id"] == "chat-legacy"
    assert "conversation_id" not in payload
    assert contract.conversation_id == "chat-legacy"
    properties = contract_type.model_json_schema()["properties"]
    assert "chat_id" in properties
    assert "conversation_id" not in properties

    payload["conversation_id"] = payload.pop("chat_id")
    restored = contract_type.model_validate(payload)
    assert restored.chat_id == "chat-legacy"
    assert restored.conversation_id == "chat-legacy"


def test_outcome_declaration_restores_legacy_chat_identity() -> None:
    _legacy_payload(ConversationOutcomeDeclaration)


def test_outcome_restores_legacy_chat_identity() -> None:
    _legacy_payload(ConversationOutcome)


def test_provider_request_cannot_forge_chat_identity() -> None:
    properties = ConversationOutcomeRequest.model_json_schema()["properties"]
    assert "chat_id" not in properties
    assert "conversation_id" not in properties


def test_outcome_rejects_conflicting_chat_identity_aliases() -> None:
    with pytest.raises(ValidationError, match="must identify one Chat"):
        ConversationOutcome(
            agent_id="default",
            chat_id="chat-a",
            conversation_id="chat-b",
            correlation_id=uuid4(),
            status=ConversationOutcomeStatus.PARTIAL,
            producer_id="qwenpaw.system.tests",
            summary="Ambiguous ownership must fail closed.",
        )
