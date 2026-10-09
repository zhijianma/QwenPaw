# -*- coding: utf-8 -*-
"""Tests for task-independent invocation contracts."""

from pydantic import ValidationError
import pytest

from qwenpaw.kernel.invocation import InvocationScope


def test_invocation_scope_is_task_independent_and_immutable() -> None:
    scope = InvocationScope(
        agent_id="default",
        chat_id="chat-spec-1",
        session_id="chat-session",
        root_agent_id="default",
        root_session_id="chat-session",
        workspace_dir="/tmp/qwenpaw-workspace",
        registry_generation=3,
    )

    payload = scope.model_dump(mode="json")
    assert "task_id" not in payload
    assert "run_id" not in payload
    assert payload["chat_id"] == "chat-spec-1"
    assert "conversation_id" not in payload
    assert scope.chat_id == "chat-spec-1"
    assert scope.conversation_id == "chat-spec-1"
    assert scope.capability_ids == (
        "qwenpaw.system.chat.agent-factory",
        "qwenpaw.system.modes.workspace-modes",
        "qwenpaw.system.workspace-tools",
        "qwenpaw.system.memory.workspace-memory",
        "qwenpaw.system.prompts.workspace-prompt",
        "qwenpaw.system.drivers.workspace-driver",
        "qwenpaw.system.commands.workspace-commands",
        "qwenpaw.system.hooks.workspace-hooks",
        "qwenpaw.system.loop-gates.workspace-gates",
    )
    with pytest.raises(ValidationError):
        scope.registry_generation = 4


def test_invocation_scope_legacy_identity_conflict() -> None:
    values = {
        "agent_id": "default",
        "session_id": "chat-session",
        "root_agent_id": "default",
        "root_session_id": "chat-session",
        "workspace_dir": "/tmp/qwenpaw-workspace",
        "registry_generation": 3,
    }

    legacy = InvocationScope(
        **values,
        conversation_id="chat-spec-1",
    )
    assert legacy.chat_id == "chat-spec-1"
    assert legacy.conversation_id == "chat-spec-1"

    with pytest.raises(ValidationError, match="must identify one Chat"):
        InvocationScope.model_validate(
            {
                **values,
                "chat_id": "chat-a",
                "conversation_id": "chat-b",
            },
        )
