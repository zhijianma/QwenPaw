# -*- coding: utf-8 -*-
"""Tests for the server-authoritative Chat invocation-control API."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from qwenpaw.app.chats.api import (
    get_chat_manager,
    get_workspace,
    router,
    stream_chat_runtime_projection,
)
from qwenpaw.app.chats.manager import ChatManager
from qwenpaw.app.chats.models import ChatSpec
from qwenpaw.app.chats.repo import JsonChatRepository
from qwenpaw.app.chats.submission_dispatcher import (
    CONSOLE_SUBMISSION_ENVELOPE,
    WorkspaceChatSubmissionDispatcher,
)
from qwenpaw.invocation_control import (
    InvocationControlService,
    RuntimeInterruptSession,
    SQLiteInvocationControl,
)
from qwenpaw.interactions import InteractionService
from qwenpaw.kernel import (
    ControlCommandStatus,
    InteractionKind,
    InteractionMode,
    InteractionRequest,
    ModelCallAttempt,
    ModelCallResult,
    ModelCallStatus,
    ModelFailureClass,
    ModelRecoveryDisposition,
    ResourceWaitStatus,
    UserInputReason,
    SteerSafePoint,
    SubmissionStatus,
    TurnSubmissionRequest,
)
from qwenpaw.recovery import ModelResourceWaitService


async def _control_context(tmp_path: Path):
    manager = ChatManager(
        repo=JsonChatRepository(tmp_path / "chats.json"),
    )
    await manager.create_chat(
        ChatSpec(
            id="chat-spec-1",
            session_id="console:chat-spec-1",
            user_id="local-user",
            channel="console",
        ),
    )
    store = SQLiteInvocationControl(tmp_path / "control.sqlite3")
    service = InvocationControlService(store=store)
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        chat_manager=manager,
        invocation_control=service,
        interaction_service=InteractionService(
            tmp_path / "interactions.sqlite3",
        ),
        model_resource_wait_service=ModelResourceWaitService(
            tmp_path / "resource-waits.sqlite3",
            agent_id="default",
        ),
    )
    workspace.submission_dispatcher = WorkspaceChatSubmissionDispatcher(
        workspace=workspace,
        control=service,
    )
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_chat_manager] = lambda: manager
    app.dependency_overrides[get_workspace] = lambda: workspace
    return app, service, store


@pytest.mark.asyncio
async def test_chat_queue_reorder_cancel_and_idempotent_replay(
    tmp_path: Path,
) -> None:
    app, service, store = await _control_context(tmp_path)
    first = await store.submit(
        TurnSubmissionRequest(
            agent_id="default",
            conversation_id="chat-spec-1",
            content="first",
            idempotency_key="turn-1",
        ),
        expected_revision=0,
    )
    second = await store.submit(
        TurnSubmissionRequest(
            agent_id="default",
            conversation_id="chat-spec-1",
            content="second",
            idempotency_key="turn-2",
        ),
        expected_revision=1,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        projection = await client.get("/api/chats/chat-spec-1/queue")
        reordered = await client.post(
            "/api/chats/chat-spec-1/queue/reorder",
            json={
                "idempotency_key": "reorder-1",
                "expected_revision": 2,
                "ordered_submission_ids": [
                    str(second.submission_id),
                    str(first.submission_id),
                ],
            },
        )
        replay = await client.post(
            "/api/chats/chat-spec-1/queue/reorder",
            json={
                "idempotency_key": "reorder-1",
                "expected_revision": 0,
                "ordered_submission_ids": [
                    str(second.submission_id),
                    str(first.submission_id),
                ],
            },
        )
        stale_cancel = await client.post(
            f"/api/chats/chat-spec-1/queue/{first.submission_id}/cancel",
            json={
                "idempotency_key": "cancel-stale",
                "expected_revision": 2,
            },
        )
        cancelled = await client.post(
            f"/api/chats/chat-spec-1/queue/{first.submission_id}/cancel",
            json={
                "idempotency_key": "cancel-1",
                "expected_revision": reordered.json()["revision"],
            },
        )
        stopped = await client.post(
            "/api/chats/chat-spec-1/control/stop-and-clear",
            json={
                "idempotency_key": "stop-and-clear-1",
                "expected_revision": cancelled.json()["revision"],
            },
        )

    assert projection.status_code == 200
    assert projection.json()["revision"] == 2
    assert reordered.status_code == 200
    assert reordered.json()["status"] == "applied"
    assert replay.json() == reordered.json()
    assert stale_cancel.status_code == 409
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "applied"
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "applied"
    remaining = await service.read_queue(
        agent_id="default",
        conversation_id="chat-spec-1",
    )
    assert remaining.submissions == ()
    await service.close()


@pytest.mark.asyncio
async def test_stop_and_clear_cancels_pending_model_recovery(
    tmp_path: Path,
) -> None:
    app, service, _ = await _control_context(tmp_path)
    workspace = app.dependency_overrides[get_workspace]()
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
    wait = await workspace.model_resource_wait_service.defer(
        attempt,
        ModelCallResult(
            attempt_id=attempt.attempt_id,
            invocation_id=attempt.invocation_id,
            conversation_id=attempt.conversation_id,
            status=ModelCallStatus.FAILED,
            failure_class=ModelFailureClass.QUOTA_EXHAUSTED,
            recovery_disposition=(
                ModelRecoveryDisposition.WAIT_RESOURCE
            ),
        ),
    )
    assert wait is not None

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        stopped = await client.post(
            "/api/chats/chat-spec-1/control/stop-and-clear",
            json={
                "idempotency_key": "stop-resource-wait",
                "expected_revision": 0,
            },
        )

    cancelled = await workspace.model_resource_wait_service.get(
        wait.wait_id,
    )
    assert stopped.status_code == 200
    assert cancelled is not None
    assert cancelled.status is ResourceWaitStatus.CANCELLED
    await service.close()


@pytest.mark.asyncio
async def test_chat_submission_persists_versioned_input_before_dispatch(
    tmp_path: Path,
) -> None:
    app, service, _ = await _control_context(tmp_path)
    payload = {
        "idempotency_key": "client-message-1",
        "expected_revision": 0,
        "content_parts": [{"type": "text", "text": "durable input"}],
        "request_context": {"approval_level": "strict"},
        "request_extensions": {"plugin_option": {"format": "brief"}},
    }

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        submitted = await client.post(
            "/api/chats/chat-spec-1/submissions",
            json=payload,
        )
        replay = await client.post(
            "/api/chats/chat-spec-1/submissions",
            json=payload,
        )

    assert submitted.status_code == 200
    assert replay.json() == submitted.json()
    submission_id = UUID(submitted.json()["submission_id"])
    record = await service.get_submission(submission_id)
    assert record is not None
    assert record.status is SubmissionStatus.QUEUED
    assert record.input_envelope is not None
    assert record.input_envelope.kind == CONSOLE_SUBMISSION_ENVELOPE
    assert record.input_envelope.payload["content_parts"] == [
        {"type": "text", "text": "durable input"},
    ]
    assert record.input_envelope.payload["meta"]["request_context"] == {
        "approval_level": "strict",
    }
    assert record.input_envelope.payload["plugin_option"] == {
        "format": "brief",
    }
    await service.close()


@pytest.mark.asyncio
async def test_chat_submission_server_sequences_concurrent_appends(
    tmp_path: Path,
) -> None:
    app, service, _ = await _control_context(tmp_path)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        first, second = await asyncio.gather(
            client.post(
                "/api/chats/chat-spec-1/submissions",
                json={
                    "idempotency_key": "tab-a",
                    "content_parts": [{"type": "text", "text": "A"}],
                },
            ),
            client.post(
                "/api/chats/chat-spec-1/submissions",
                json={
                    "idempotency_key": "tab-b",
                    "content_parts": [{"type": "text", "text": "B"}],
                },
            ),
        )

    assert first.status_code == 200
    assert second.status_code == 200
    projection = await service.read_queue(
        agent_id="default",
        conversation_id="chat-spec-1",
    )
    assert [item.sequence for item in projection.submissions] == [1, 2]
    assert {item.idempotency_key for item in projection.submissions} == {
        "tab-a",
        "tab-b",
    }
    await service.close()


@pytest.mark.asyncio
async def test_chat_submission_rejects_reserved_request_extension(
    tmp_path: Path,
) -> None:
    app, service, _ = await _control_context(tmp_path)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/chats/chat-spec-1/submissions",
            json={
                "idempotency_key": "spoofed-extension",
                "expected_revision": 0,
                "content_parts": [{"type": "text", "text": "hello"}],
                "request_extensions": {"sender_id": "another-user"},
            },
        )

    assert response.status_code == 422
    assert not (
        await service.read_queue(
            agent_id="default",
            conversation_id="chat-spec-1",
        )
    ).submissions
    await service.close()


@pytest.mark.asyncio
async def test_chat_steer_applies_at_runtime_safe_point(
    tmp_path: Path,
) -> None:
    app, service, _ = await _control_context(tmp_path)
    lease = await service.begin_turn(
        TurnSubmissionRequest(
            agent_id="default",
            conversation_id="chat-spec-1",
            content="active request",
            idempotency_key="turn-active",
        ),
        invocation_id=uuid4(),
    )
    projection = await service.read_queue(
        agent_id="default",
        conversation_id="chat-spec-1",
    )
    injected: list[str] = []

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        accepted = await client.post(
            "/api/chats/chat-spec-1/control/steer",
            json={
                "idempotency_key": "steer-1",
                "expected_revision": projection.revision,
                "instruction": "Use the shorter implementation.",
            },
        )
        conflicting_replay = await client.post(
            "/api/chats/chat-spec-1/control/steer",
            json={
                "idempotency_key": "steer-1",
                "expected_revision": projection.revision,
                "instruction": "Use a different implementation.",
            },
        )

    async def inject(delivery, _safe_point) -> None:
        injected.append(delivery.instruction)

    count = await lease.steering.apply_pending(
        SteerSafePoint.BEFORE_REASONING,
        inject,
    )
    await service.wait_dispatch(UUID(accepted.json()["command_id"]))

    assert accepted.status_code == 200
    assert accepted.json()["status"] == "accepted"
    assert conflicting_replay.status_code == 409
    assert count == 1
    assert injected == ["Use the shorter implementation."]
    await service.finish_turn(lease, SubmissionStatus.SUCCEEDED)
    await service.close()


@pytest.mark.asyncio
async def test_chat_interrupt_uses_runtime_cancellation_root(
    tmp_path: Path,
) -> None:
    app, service, _ = await _control_context(tmp_path)
    invocation_id = uuid4()
    lease = await service.begin_turn(
        TurnSubmissionRequest(
            agent_id="default",
            conversation_id="chat-spec-1",
            content="active request",
            idempotency_key="turn-active",
        ),
        invocation_id=invocation_id,
    )
    binding_holder: dict[str, RuntimeInterruptSession] = {}

    async def runtime_task() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await binding_holder["binding"].close()
            await service.finish_turn(
                lease,
                SubmissionStatus.INTERRUPTED,
            )
            raise

    task = asyncio.create_task(runtime_task())
    binding = await service.bind_interrupt(
        invocation_id,
        task,
        lease=lease,
        agent_id="default",
        conversation_id="chat-spec-1",
    )
    binding_holder["binding"] = binding
    projection = await service.read_queue(
        agent_id="default",
        conversation_id="chat-spec-1",
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        interrupted = await client.post(
            "/api/chats/chat-spec-1/control/interrupt",
            json={
                "idempotency_key": "interrupt-1",
                "expected_revision": projection.revision,
            },
        )

    assert interrupted.status_code == 200
    assert interrupted.json()["status"] == ControlCommandStatus.APPLIED.value
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lease.submission.status is SubmissionStatus.INTERRUPTED
    await service.close()


@pytest.mark.asyncio
async def test_chat_control_rejects_missing_chat_and_idle_interrupt(
    tmp_path: Path,
) -> None:
    app, service, _ = await _control_context(tmp_path)
    body = {
        "idempotency_key": "interrupt-idle",
        "expected_revision": 0,
    }

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        missing = await client.get("/api/chats/missing/queue")
        idle = await client.post(
            "/api/chats/chat-spec-1/control/interrupt",
            json=body,
        )
        cursor_conflict = await client.get(
            "/api/chats/chat-spec-1/runtime/stream",
            params={"after_cursor": "v1-query"},
            headers={"Last-Event-ID": "v1-header"},
        )

    assert missing.status_code == 404
    assert idle.status_code == 409
    assert cursor_conflict.status_code == 400
    await service.close()


@pytest.mark.asyncio
async def test_runtime_snapshot_stream_recovers_queue_and_interactions(
    tmp_path: Path,
) -> None:
    app, service, _ = await _control_context(tmp_path)
    manager = app.dependency_overrides[get_chat_manager]()
    workspace = app.dependency_overrides[get_workspace]()

    class ConnectedRequest:
        @staticmethod
        async def is_disconnected() -> bool:
            return False

    response = await stream_chat_runtime_projection(
        "chat-spec-1",
        ConnectedRequest(),
        None,
        None,
        manager,
        workspace,
    )
    iterator = response.body_iterator
    first = await anext(iterator)
    first_text = first.decode() if isinstance(first, bytes) else first
    first_cursor = first_text.splitlines()[0].removeprefix("id: ")

    pending = asyncio.create_task(anext(iterator))
    interaction = InteractionRequest(
        kind=InteractionKind.USER_INPUT,
        mode=InteractionMode.BLOCKING,
        agent_id="default",
        conversation_id="chat-spec-1",
        invocation_id=uuid4(),
        user_input_reason=UserInputReason.MATERIAL_PREFERENCE,
        title="Choose format",
        prompt="Markdown or HTML?",
    )
    await workspace.interaction_service.open(interaction)
    second = await asyncio.wait_for(pending, timeout=2)
    second_text = second.decode() if isinstance(second, bytes) else second

    assert "event: snapshot" in first_text
    assert '"interactions":[]' in first_text
    assert '"activity":{"schema":"qwenpaw.observation-page.v1"' in (
        first_text
    )
    assert f"id: {first_cursor}" not in second_text
    assert str(interaction.interaction_id) in second_text
    await iterator.aclose()
    await service.close()
