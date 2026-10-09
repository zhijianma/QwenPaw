# -*- coding: utf-8 -*-
"""Contract tests for storage-neutral Conversation forks."""

import subprocess
import sys

from pydantic import ValidationError
import pytest

from qwenpaw.kernel import (
    ConversationForkBoundary,
    ConversationForkCommand,
    ConversationForkOrigin,
    ConversationForkPort,
    ConversationForkResult,
)


def _command() -> ConversationForkCommand:
    return ConversationForkCommand(
        agent_id="default",
        parent_chat_id="parent-chat",
        source_message_id="assistant-final",
        idempotency_key="fork-click-1",
        name="Alternative",
    )


def test_conversation_fork_contract_round_trip() -> None:
    command = _command()
    origin = ConversationForkOrigin(
        agent_id=command.agent_id,
        parent_chat_id=command.parent_chat_id,
        root_chat_id=command.parent_chat_id,
        source_message_id=command.source_message_id,
        depth=1,
    )
    result = ConversationForkResult(
        child_chat_id="child-chat",
        origin=origin,
    )

    assert command.boundary is ConversationForkBoundary.AFTER_RESPONSE
    assert (
        ConversationForkCommand.model_validate_json(
            command.model_dump_json(),
        )
        == command
    )
    assert (
        ConversationForkResult.model_validate_json(
            result.model_dump_json(),
        )
        == result
    )


def test_conversation_fork_contract_rejects_unstable_identity() -> None:
    with pytest.raises(ValidationError):
        ConversationForkCommand(
            agent_id="default",
            parent_chat_id="",
            source_message_id="assistant-final",
            idempotency_key="fork-click-1",
        )
    with pytest.raises(ValidationError):
        ConversationForkCommand(
            agent_id="default",
            parent_chat_id="parent-chat",
            source_message_id="assistant-final",
            idempotency_key="fork-click-1",
            boundary="before_response",
        )
    with pytest.raises(ValidationError):
        ConversationForkCommand(
            agent_id="default",
            parent_chat_id="parent-chat",
            source_message_id="assistant-final",
            idempotency_key="   ",
        )


def test_conversation_fork_contract_reads_legacy_identities() -> None:
    command = ConversationForkCommand.model_validate(
        {
            "agent_id": "default",
            "parent_conversation_id": "parent-chat",
            "source_message_id": "assistant-final",
            "idempotency_key": "legacy-fork",
        },
    )
    result = ConversationForkResult.model_validate(
        {
            "child_conversation_id": "child-chat",
            "origin": {
                "agent_id": "default",
                "parent_conversation_id": "parent-chat",
                "root_conversation_id": "root-chat",
                "source_message_id": "assistant-final",
                "depth": 2,
            },
        },
    )

    assert command.parent_chat_id == "parent-chat"
    assert command.parent_conversation_id == "parent-chat"
    assert result.child_chat_id == "child-chat"
    assert result.child_conversation_id == "child-chat"
    assert result.origin.parent_chat_id == "parent-chat"
    assert result.origin.root_chat_id == "root-chat"
    assert set(command.model_dump()) == {
        "schema",
        "agent_id",
        "parent_chat_id",
        "source_message_id",
        "idempotency_key",
        "boundary",
        "name",
    }
    assert "parent_conversation_id" not in (
        ConversationForkCommand.model_json_schema()["properties"]
    )


def test_conversation_fork_contract_rejects_conflicting_identities() -> None:
    with pytest.raises(ValidationError, match="must identify one Chat"):
        ConversationForkCommand.model_validate(
            {
                "agent_id": "default",
                "parent_chat_id": "parent-chat",
                "parent_conversation_id": "other-chat",
                "source_message_id": "assistant-final",
                "idempotency_key": "conflicting-fork",
            },
        )
    with pytest.raises(ValidationError, match="must identify one Chat"):
        ConversationForkResult.model_validate(
            {
                "child_chat_id": "child-chat",
                "child_conversation_id": "other-chat",
                "origin": {
                    "agent_id": "default",
                    "parent_chat_id": "parent-chat",
                    "root_chat_id": "root-chat",
                    "source_message_id": "assistant-final",
                    "depth": 1,
                },
            },
        )


def test_conversation_fork_port_is_runtime_checkable() -> None:
    class _Forker:
        async def fork(self, command):
            del command

        async def lineage(self, **kwargs):
            del kwargs
            return ()

    assert isinstance(_Forker(), ConversationForkPort)


def test_lite_adapter_import_does_not_cycle_through_chat_api() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from qwenpaw.conversations import "
                "LiteConversationForkAdapter; "
                "print(LiteConversationForkAdapter.__name__)"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "LiteConversationForkAdapter"
