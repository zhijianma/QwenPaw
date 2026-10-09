# -*- coding: utf-8 -*-
"""Tests for structured Chat compatibility observations."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from qwenpaw.app.chats.api import (
    legacy_stop_compatibility,
    record_external_queue_fallback,
)
from qwenpaw.app.chats.compatibility import (
    ExternalQueueFallbackRequest,
    SQLiteExternalQueueCompatibilityStore,
    SQLiteLegacyStopCompatibilityStore,
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


@pytest.mark.asyncio
async def test_legacy_stop_observation_is_idempotent_and_resets_window(
    tmp_path,
) -> None:
    path = tmp_path / "chat-compatibility.db"
    store = SQLiteLegacyStopCompatibilityStore(path, "agent-a")
    started_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    await store.start(started_at=started_at)

    first = await store.record(
        observation_id="console-stop:1",
        entrypoint="console.stop_api",
        disposition="os_interrupt",
        observed_at=started_at,
    )
    replay = await store.record(
        observation_id="console-stop:1",
        entrypoint="console.stop_api",
        disposition="compatibility_cancelled",
        observed_at=started_at + timedelta(days=1),
    )
    second = await store.record(
        observation_id="channel-stop:2",
        entrypoint="channel.slash_stop",
        disposition="chat_not_found",
        observed_at=started_at + timedelta(days=2),
    )

    report = await store.report(now=started_at + timedelta(days=8))

    assert first is True
    assert replay is False
    assert second is True
    assert report.total_hits == 2
    assert report.last_legacy_hit_at == started_at + timedelta(days=2)
    assert report.zero_usage_seconds == int(timedelta(days=6).total_seconds())
    assert report.zero_usage_window_complete is False
    assert report.removal_authorized is False
    assert {item.entrypoint for item in report.hits} == {
        "console.stop_api",
        "channel.slash_stop",
    }
    assert b"console-stop:1" not in path.read_bytes()
    assert b"channel-stop:2" not in path.read_bytes()


@pytest.mark.asyncio
async def test_legacy_stop_zero_window_survives_store_reopen(tmp_path) -> None:
    path = tmp_path / "chat-compatibility.db"
    started_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    store = SQLiteLegacyStopCompatibilityStore(path, "agent-a")
    await store.start(started_at=started_at)

    reopened = SQLiteLegacyStopCompatibilityStore(path, "agent-a")
    report = await reopened.report(now=started_at + timedelta(days=7))

    assert report.total_hits == 0
    assert report.zero_usage_window_complete is True
    assert report.zero_usage_seconds_remaining == 0
    assert report.removal_blockers == (
        "legacy_clients_not_confirmed_migrated",
    )


@pytest.mark.asyncio
async def test_legacy_stop_report_endpoint_uses_workspace_service(
    tmp_path,
) -> None:
    store = SQLiteLegacyStopCompatibilityStore(
        tmp_path / "chat-compatibility.db",
        "agent-a",
    )
    await store.start()

    payload = await legacy_stop_compatibility(
        SimpleNamespace(legacy_stop_compatibility=store),
    )

    assert payload["schema_version"] == (
        "qwenpaw.legacy-stop-compatibility.v1"
    )
    assert payload["agent_id"] == "agent-a"
    assert payload["total_hits"] == 0
