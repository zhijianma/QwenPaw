# -*- coding: utf-8 -*-
"""Tests for Chat-owned runtime interaction delivery endpoints."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from qwenpaw.app.chats.api import (
    list_chat_interactions,
    list_chat_wait_conditions,
    respond_chat_interaction,
)
from qwenpaw.app.chats.manager import ChatManager
from qwenpaw.app.chats.models import (
    ChatInteractionDecisionRequest,
    ChatSpec,
)
from qwenpaw.app.chats.repo import JsonChatRepository
from qwenpaw.interactions import InteractionService
from qwenpaw.recovery import ModelResourceWaitService
from qwenpaw.kernel import (
    ApprovalDecisionValue,
    InteractionKind,
    InteractionMode,
    InteractionOption,
    InteractionRequest,
    ModelCallAttempt,
    ModelCallResult,
    ModelCallStatus,
    ModelFailureClass,
    ModelRecoveryDisposition,
    UserInputReason,
    InteractionStatus,
    WaitConditionStatus,
)


async def _context(tmp_path):
    manager = ChatManager(
        repo=JsonChatRepository(tmp_path / "chats.json"),
    )
    chat = await manager.create_chat(
        ChatSpec(
            id="chat-spec-1",
            session_id="console:chat-spec-1",
            user_id="local-user",
            channel="console",
        ),
    )
    service = InteractionService(tmp_path / "interactions.sqlite3")
    workspace = SimpleNamespace(
        agent_id="default",
        interaction_service=service,
    )
    request = InteractionRequest(
        kind=InteractionKind.USER_INPUT,
        mode=InteractionMode.BLOCKING,
        agent_id="default",
        conversation_id=chat.id,
        invocation_id=uuid4(),
        user_input_reason=UserInputReason.MATERIAL_PREFERENCE,
        title="Choose output",
        prompt="Which format?",
        options=(
            InteractionOption(option_id="md", label="Markdown"),
            InteractionOption(option_id="html", label="HTML"),
        ),
    )
    await service.open(request)
    return manager, workspace, request


@pytest.mark.asyncio
async def test_list_and_resolve_chat_owned_interaction(tmp_path) -> None:
    manager, workspace, interaction = await _context(tmp_path)
    continuation_wakes: list[bool] = []
    recovery_wakes: list[bool] = []
    workspace.submission_dispatcher = SimpleNamespace(
        wake_continuations=lambda: continuation_wakes.append(True),
        wake_resource_waits=lambda: recovery_wakes.append(True),
    )
    http_request = SimpleNamespace(
        state=SimpleNamespace(user={"username": "console-admin"}),
    )

    opened = await list_chat_interactions(
        "chat-spec-1",
        manager,
        workspace,
    )
    resolution = await respond_chat_interaction(
        "chat-spec-1",
        interaction.interaction_id,
        ChatInteractionDecisionRequest(
            idempotency_key="choose-html",
            expected_revision=1,
            selected_option_ids=("html",),
        ),
        http_request,
        manager,
        workspace,
    )

    assert opened == [interaction]
    opened_payload = opened[0].model_dump(mode="json")
    assert opened_payload["chat_id"] == "chat-spec-1"
    assert "conversation_id" not in opened_payload
    assert resolution.status is InteractionStatus.RESOLVED
    assert resolution.response is not None
    assert resolution.response.actor.id == "console-admin"
    assert continuation_wakes == [True]
    assert recovery_wakes == [True]
    assert (
        await list_chat_interactions(
            "chat-spec-1",
            manager,
            workspace,
        )
        == []
    )
    conditions = await list_chat_wait_conditions(
        "chat-spec-1",
        True,
        100,
        manager,
        workspace,
    )
    assert len(conditions) == 1
    assert conditions[0].status is WaitConditionStatus.SATISFIED
    assert conditions[0].condition_id == interaction.interaction_id


@pytest.mark.asyncio
async def test_response_rejects_cross_chat_interaction(tmp_path) -> None:
    manager, workspace, interaction = await _context(tmp_path)
    await manager.create_chat(
        ChatSpec(
            id="another-chat",
            session_id="console:another",
            user_id="local-user",
            channel="console",
        ),
    )

    with pytest.raises(HTTPException) as rejected:
        await respond_chat_interaction(
            "another-chat",
            interaction.interaction_id,
            ChatInteractionDecisionRequest(
                idempotency_key="cross-chat",
                expected_revision=1,
                text="forged",
            ),
            SimpleNamespace(state=SimpleNamespace()),
            manager,
            workspace,
        )

    assert rejected.value.status_code == 404


@pytest.mark.asyncio
async def test_wait_conditions_merge_interaction_and_resource_sources(
    tmp_path,
) -> None:
    manager, workspace, _interaction = await _context(tmp_path)
    resource_waits = ModelResourceWaitService(
        tmp_path / "resource-waits.sqlite3",
        agent_id="default",
    )
    workspace.model_resource_wait_service = resource_waits
    attempt = ModelCallAttempt(
        attempt_id=uuid4(),
        route_decision_id=uuid4(),
        invocation_id=uuid4(),
        correlation_id=uuid4(),
        conversation_id="chat-spec-1",
        registry_generation=1,
        context_manifest_id=uuid4(),
        model_call_index=1,
        attempt_index=1,
        provider_id="provider-a",
        model_id="model-a",
    )
    await resource_waits.defer(
        attempt,
        ModelCallResult(
            attempt_id=attempt.attempt_id,
            invocation_id=attempt.invocation_id,
            conversation_id=attempt.conversation_id,
            status=ModelCallStatus.FAILED,
            failure_class=ModelFailureClass.QUOTA_EXHAUSTED,
            recovery_disposition=ModelRecoveryDisposition.WAIT_RESOURCE,
        ),
    )

    conditions = await list_chat_wait_conditions(
        "chat-spec-1",
        False,
        100,
        manager,
        workspace,
    )

    assert {condition.kind.value for condition in conditions} == {
        "resource",
        "user_input",
    }


@pytest.mark.asyncio
async def test_task_approval_response_reconciles_durable_decision(
    tmp_path,
    monkeypatch,
) -> None:
    manager, workspace, _interaction = await _context(tmp_path)
    task_id = uuid4()
    approval_id = uuid4()
    interaction = InteractionRequest(
        kind=InteractionKind.APPROVAL,
        mode=InteractionMode.BLOCKING,
        agent_id="default",
        conversation_id="chat-spec-1",
        invocation_id=uuid4(),
        task_id=task_id,
        source_id=approval_id,
        title="Approve command",
        prompt="Allow execution?",
        options=(
            InteractionOption(
                option_id="approve_exact",
                label="Approve once",
            ),
            InteractionOption(option_id="deny", label="Deny"),
        ),
    )
    await workspace.interaction_service.open(interaction)
    commands = []

    class _Approvals:
        async def reconcile(self, command):
            commands.append(command)

    async def _bindings(_request):
        return SimpleNamespace(approvals=_Approvals())

    monkeypatch.setattr(
        "qwenpaw.app.task_runtime.task_application_bindings",
        _bindings,
    )
    request = SimpleNamespace(
        state=SimpleNamespace(user={"username": "console-admin"}),
    )

    await respond_chat_interaction(
        "chat-spec-1",
        interaction.interaction_id,
        ChatInteractionDecisionRequest(
            idempotency_key="approve-task",
            expected_revision=1,
            selected_option_ids=("approve_exact",),
        ),
        request,
        manager,
        workspace,
    )

    assert len(commands) == 1
    assert commands[0].task_id == task_id
    assert commands[0].approval_id == approval_id
    assert commands[0].decision is ApprovalDecisionValue.APPROVED
    assert commands[0].delivery_managed_by_interaction is True


@pytest.mark.asyncio
async def test_response_maps_stale_revision_to_conflict(tmp_path) -> None:
    manager, workspace, interaction = await _context(tmp_path)

    with pytest.raises(HTTPException) as conflict:
        await respond_chat_interaction(
            "chat-spec-1",
            interaction.interaction_id,
            ChatInteractionDecisionRequest(
                idempotency_key="stale",
                expected_revision=2,
                text="answer",
            ),
            SimpleNamespace(state=SimpleNamespace()),
            manager,
            workspace,
        )

    assert conflict.value.status_code == 409


def test_response_model_rejects_empty_or_duplicate_answers() -> None:
    with pytest.raises(ValidationError):
        ChatInteractionDecisionRequest(
            idempotency_key="empty",
            expected_revision=1,
        )
    with pytest.raises(ValidationError):
        ChatInteractionDecisionRequest(
            idempotency_key="duplicates",
            expected_revision=1,
            selected_option_ids=("md", "md"),
        )
