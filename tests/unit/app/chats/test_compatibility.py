# -*- coding: utf-8 -*-
"""Tests for structured Chat compatibility observations."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from qwenpaw.app.chats.api import record_external_queue_fallback
from qwenpaw.app.chats.compatibility import (
    ExternalQueueFallbackRequest,
    SQLiteExternalQueueCompatibilityStore,
)


@pytest.mark.asyncio
async def test_external_queue_fallback_is_idempotent_and_agent_scoped(
    tmp_path,
) -> None:
    store = SQLiteExternalQueueCompatibilityStore(
        tmp_path / "chat-compatibility.db",
    )
    started_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    request = ExternalQueueFallbackRequest(
        observation_id="enqueue-1",
        backend_id="codex",
    )

    first = await store.record(
        agent_id="agent-a",
        request=request,
        observed_at=started_at,
    )
    replay = await store.record(
        agent_id="agent-a",
        request=request,
        observed_at=started_at + timedelta(seconds=1),
    )
    await store.record(
        agent_id="agent-a",
        request=ExternalQueueFallbackRequest(
            observation_id="enqueue-2",
            backend_id="codex",
        ),
        observed_at=started_at + timedelta(seconds=2),
    )
    await store.record(
        agent_id="agent-b",
        request=request,
        observed_at=started_at + timedelta(seconds=3),
    )

    report = await store.report(agent_id="agent-a")
    other = await store.report(agent_id="agent-b")

    assert first is True
    assert replay is False
    assert report.total_hits == 2
    assert report.backends[0].backend_id == "codex"
    assert report.backends[0].replacement_capability == "conversation.queue"
    assert report.backends[0].first_seen_at == started_at
    assert report.backends[0].last_seen_at == started_at + timedelta(seconds=2)
    assert report.removal_authorized is False
    assert other.total_hits == 1
    assert other.backends[0].backend_id == "codex"


@pytest.mark.asyncio
async def test_external_queue_endpoint_verifies_configured_backend(
    monkeypatch,
    tmp_path,
) -> None:
    workspace = SimpleNamespace(
        agent_id="agent-a",
        workspace_dir=tmp_path,
    )

    async def load_profile(_agent_id: str):
        return SimpleNamespace(backend="codex")

    monkeypatch.setattr(
        "qwenpaw.app.chats.api.load_agent_config_async",
        load_profile,
    )
    request = ExternalQueueFallbackRequest(
        observation_id="enqueue-1",
        backend_id="codex",
    )

    assert await record_external_queue_fallback(request, workspace) == {
        "recorded": True,
    }
    assert await record_external_queue_fallback(request, workspace) == {
        "recorded": False,
    }

    with pytest.raises(HTTPException) as exc_info:
        await record_external_queue_fallback(
            request.model_copy(update={"backend_id": "qoder"}),
            workspace,
        )
    assert exc_info.value.status_code == 409
