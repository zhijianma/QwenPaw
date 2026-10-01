# -*- coding: utf-8 -*-
"""Ownership-boundary tests for the global chat API."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from qwenpaw.app.chats.api import (
    _artifact_receipt_from_history,
    get_chat,
    get_chat_status,
    list_chat_actions,
    list_chat_model_calls,
    list_chat_observations,
    list_chats,
)
from qwenpaw.app.chats.models import ChatHistory, ChatSpec
from qwenpaw.kernel import (
    ActionKind,
    ActionRequest,
    ModelCallAttempt,
    ModelRouteReason,
    RouteDecision,
)
from qwenpaw.runtime.actions import lite_action_store
from qwenpaw.runtime.model_calls import lite_model_call_store
from qwenpaw.schemas import DataContent, Message


def _chat(chat_id: str, *, app_id: str | None = None) -> ChatSpec:
    meta = (
        {
            "pawapp": {
                "app_id": app_id,
                "agent_id": "datapaw",
            },
        }
        if app_id
        else {}
    )
    return ChatSpec(
        id=chat_id,
        session_id=f"console:{chat_id}",
        user_id="default",
        channel="console",
        meta=meta,
    )


def test_artifact_receipt_resolves_from_tool_result_link() -> None:
    artifact_id = "b6f55fc5-8b5a-470b-b7d1-f01f05b20c44"
    history = ChatHistory(
        messages=[
            Message(
                content=[
                    DataContent(
                        data={
                            "artifact_links": [
                                {
                                    "artifact_ref": {
                                        "artifact_id": artifact_id,
                                    },
                                    "artifact_receipt": "receipt-1",
                                },
                            ],
                        },
                    ),
                ],
            ),
        ],
    )

    assert (
        _artifact_receipt_from_history(
            history,
            UUID(artifact_id),
        )
        == "receipt-1"
    )


@pytest.mark.asyncio
async def test_list_chats_can_exclude_app_owned_dialogues():
    normal = _chat("normal")
    app_owned = _chat("app-owned", app_id="datapaw")
    manager = SimpleNamespace(
        list_chats=AsyncMock(return_value=[normal, app_owned]),
    )
    tracker = SimpleNamespace(get_status=AsyncMock(return_value="idle"))

    result = await list_chats(
        user_id=None,
        channel=None,
        archived=False,
        include_app_owned=False,
        mgr=manager,
        workspace=SimpleNamespace(task_tracker=tracker),
    )

    assert [chat.id for chat in result] == ["normal"]
    tracker.get_status.assert_awaited_once_with("normal")


@pytest.mark.asyncio
async def test_get_chat_hides_app_owned_dialogue_when_caller_opts_out():
    manager = SimpleNamespace(
        get_chat=AsyncMock(return_value=_chat("app-owned", app_id="datapaw")),
    )

    with pytest.raises(HTTPException) as raised:
        await get_chat(
            chat_id="app-owned",
            include_app_owned=False,
            mgr=manager,
            session=SimpleNamespace(),
            workspace=SimpleNamespace(),
        )

    assert raised.value.status_code == 404


@pytest.mark.asyncio
async def test_get_chat_status_uses_tracker_without_loading_chat_persistence():
    tracker = SimpleNamespace(get_status=AsyncMock(return_value="running"))
    workspace = SimpleNamespace(task_tracker=tracker)

    result = await get_chat_status(
        chat_id="chat-1",
        workspace=workspace,
    )

    assert result.status == "running"
    tracker.get_status.assert_awaited_once_with("chat-1")


@pytest.mark.asyncio
async def test_get_chat_status_treats_unknown_run_key_as_idle():
    tracker = SimpleNamespace(get_status=AsyncMock(return_value="idle"))

    result = await get_chat_status(
        chat_id="missing",
        workspace=SimpleNamespace(task_tracker=tracker),
    )

    assert result.status == "idle"
    tracker.get_status.assert_awaited_once_with("missing")


@pytest.mark.asyncio
async def test_list_chat_actions_returns_privacy_safe_records(tmp_path):
    chat_id = "chat-actions"
    manager = SimpleNamespace(
        get_chat=AsyncMock(return_value=_chat(chat_id)),
    )
    invocation_id = uuid4()
    request = ActionRequest(
        invocation_id=invocation_id,
        correlation_id=invocation_id,
        conversation_id=chat_id,
        registry_generation=7,
        capability_id="qwenpaw.system.workspace-tools",
        kind=ActionKind.BROWSER,
        action_name="browser",
        redacted_arguments={"code": "private browser program"},
        arguments_hash="sha256:" + "a" * 64,
        idempotency_key="browser:legacy:call-1",
    )
    await lite_action_store(tmp_path).begin(request)

    records = await list_chat_actions(
        chat_id=chat_id,
        limit=20,
        mgr=manager,
        workspace=SimpleNamespace(workspace_dir=tmp_path),
    )

    [record] = records
    assert record.request.conversation_id == chat_id
    assert record.request.kind is ActionKind.BROWSER
    assert record.request.arguments == {}
    assert record.request.redacted_arguments == {
        "code": "[CONTENT OMITTED]",
    }
    assert record.request.arguments_hash != "sha256:" + "a" * 64
    assert "arguments" not in record.model_dump(mode="json")["request"]
    manager.get_chat.assert_awaited_once_with(chat_id)


@pytest.mark.asyncio
async def test_list_chat_actions_rejects_unknown_chat(tmp_path):
    manager = SimpleNamespace(get_chat=AsyncMock(return_value=None))

    with pytest.raises(HTTPException) as raised:
        await list_chat_actions(
            chat_id=str(uuid4()),
            limit=20,
            mgr=manager,
            workspace=SimpleNamespace(workspace_dir=tmp_path),
        )

    assert raised.value.status_code == 404
    assert not (tmp_path / ".qwenpaw").exists()


@pytest.mark.asyncio
async def test_list_chat_model_calls_returns_content_free_records(tmp_path):
    chat_id = "chat-model-calls"
    manager = SimpleNamespace(
        get_chat=AsyncMock(return_value=_chat(chat_id)),
    )
    invocation_id = uuid4()
    manifest_id = uuid4()
    attempt_id = uuid4()
    decision_id = uuid4()
    route = RouteDecision(
        route_decision_id=decision_id,
        attempt_id=attempt_id,
        invocation_id=invocation_id,
        correlation_id=invocation_id,
        conversation_id=chat_id,
        registry_generation=7,
        context_manifest_id=manifest_id,
        model_call_index=1,
        attempt_index=1,
        provider_id="provider-a",
        model_id="model-a",
        reason=ModelRouteReason.PRIMARY,
    )
    attempt = ModelCallAttempt(
        attempt_id=attempt_id,
        route_decision_id=decision_id,
        invocation_id=invocation_id,
        correlation_id=invocation_id,
        conversation_id=chat_id,
        registry_generation=7,
        context_manifest_id=manifest_id,
        model_call_index=1,
        attempt_index=1,
        provider_id="provider-a",
        model_id="model-a",
    )
    await lite_model_call_store(tmp_path).begin(route, attempt)

    records = await list_chat_model_calls(
        chat_id=chat_id,
        limit=20,
        mgr=manager,
        workspace=SimpleNamespace(workspace_dir=tmp_path),
    )

    [record] = records
    assert record.route.reason is ModelRouteReason.PRIMARY
    assert record.attempt.context_manifest_id == manifest_id
    assert record.result is None
    assert "messages" not in record.model_dump_json()
    manager.get_chat.assert_awaited_once_with(chat_id)


@pytest.mark.asyncio
async def test_list_chat_model_calls_rejects_unknown_chat(tmp_path):
    manager = SimpleNamespace(get_chat=AsyncMock(return_value=None))

    with pytest.raises(HTTPException) as raised:
        await list_chat_model_calls(
            chat_id=str(uuid4()),
            limit=20,
            mgr=manager,
            workspace=SimpleNamespace(workspace_dir=tmp_path),
        )

    assert raised.value.status_code == 404
    assert not (tmp_path / ".qwenpaw").exists()


@pytest.mark.asyncio
async def test_list_chat_observations_uses_owned_projection(tmp_path):
    chat_id = "chat-observations"
    manager = SimpleNamespace(
        get_chat=AsyncMock(return_value=_chat(chat_id)),
    )
    invocation_id = uuid4()
    attempt_id = uuid4()
    decision_id = uuid4()
    route = RouteDecision(
        route_decision_id=decision_id,
        attempt_id=attempt_id,
        invocation_id=invocation_id,
        correlation_id=invocation_id,
        conversation_id=chat_id,
        registry_generation=7,
        context_manifest_id=uuid4(),
        model_call_index=1,
        attempt_index=1,
        provider_id="provider-a",
        model_id="model-a",
        reason=ModelRouteReason.PRIMARY,
    )
    attempt = ModelCallAttempt(
        attempt_id=attempt_id,
        route_decision_id=decision_id,
        invocation_id=invocation_id,
        correlation_id=invocation_id,
        conversation_id=chat_id,
        registry_generation=7,
        context_manifest_id=route.context_manifest_id,
        model_call_index=1,
        attempt_index=1,
        provider_id="provider-a",
        model_id="model-a",
    )
    await lite_model_call_store(tmp_path).begin(route, attempt)

    observations = await list_chat_observations(
        chat_id=chat_id,
        limit=20,
        mgr=manager,
        workspace=SimpleNamespace(
            workspace_dir=tmp_path,
            agent_id="default",
            interaction_service=None,
        ),
    )

    assert {item.stage.value for item in observations} == {
        "policy",
        "execution",
    }
    assert {item.source.source_type for item in observations} == {
        "qwenpaw.model.route-decision",
        "qwenpaw.model.attempt",
    }
    manager.get_chat.assert_awaited_once_with(chat_id)


@pytest.mark.asyncio
async def test_list_chat_observations_rejects_unknown_chat(tmp_path):
    manager = SimpleNamespace(get_chat=AsyncMock(return_value=None))

    with pytest.raises(HTTPException) as raised:
        await list_chat_observations(
            chat_id=str(uuid4()),
            limit=20,
            mgr=manager,
            workspace=SimpleNamespace(
                workspace_dir=tmp_path,
                agent_id="default",
                interaction_service=None,
            ),
        )

    assert raised.value.status_code == 404
    assert not (tmp_path / ".qwenpaw").exists()
