# -*- coding: utf-8 -*-
"""Tests for legacy Approval waiter removal evidence."""

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from qwenpaw.app.approvals.compatibility import (
    SQLiteLegacyApprovalCompatibilityStore,
)
from qwenpaw.app.approvals.interaction_bridge import (
    attach_pending_to_interaction,
)
from qwenpaw.app.approvals.models import ApprovalRequestSummary
from qwenpaw.app.approvals.service import ApprovalService
from qwenpaw.app.routers.approval import legacy_approval_compatibility
from qwenpaw.interactions import InteractionService

INVOCATION_ID = UUID("00000000-0000-0000-0000-000000000201")


async def _pending(service: ApprovalService):
    return await service.create_pending_summary(
        session_id="legacy-session",
        root_session_id="legacy-session",
        owner_agent_id="default",
        user_id="local-user",
        channel="console",
        agent_id="default",
        summary=ApprovalRequestSummary(
            source_type="tool_guard",
            name="run_shell",
            severity="high",
            result_summary="Approval required.",
        ),
    )


@pytest.mark.asyncio
async def test_store_hashes_identity_and_resets_zero_use_window(
    tmp_path,
) -> None:
    path = tmp_path / "interaction-compatibility.db"
    store = SQLiteLegacyApprovalCompatibilityStore(path, "default")
    started_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    observed_at = started_at + timedelta(days=6)
    await store.start(started_at=started_at)

    assert await store.record(
        observation_id="private-request-id",
        source="tool_guard",
        reason="chat_identity_missing",
        observed_at=observed_at,
    )
    assert not await store.record(
        observation_id="private-request-id",
        source="tool_guard",
        reason="chat_identity_missing",
        observed_at=observed_at,
    )
    report = await store.report(now=observed_at + timedelta(days=1))

    assert report.total_hits == 1
    assert report.zero_usage_seconds == 86_400
    assert report.zero_usage_window_complete is False
    assert report.removal_authorized is False
    assert report.hits[0].source == "tool_guard"
    with sqlite3.connect(path) as connection:
        [stored_id] = connection.execute(
            "SELECT observation_id FROM legacy_approval_events_v1",
        ).fetchone()
    assert (
        stored_id
        == hashlib.sha256(
            b"private-request-id",
        ).hexdigest()
    )
    assert "private-request-id" not in path.read_bytes().decode(
        "utf-8",
        errors="ignore",
    )


@pytest.mark.asyncio
async def test_store_survives_restart_and_completes_window(tmp_path) -> None:
    path = tmp_path / "interaction-compatibility.db"
    started_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    first = SQLiteLegacyApprovalCompatibilityStore(path, "default")
    await first.start(started_at=started_at)
    restarted = SQLiteLegacyApprovalCompatibilityStore(path, "default")
    await restarted.start(started_at=started_at + timedelta(days=3))

    report = await restarted.report(now=started_at + timedelta(days=7))

    assert report.observation_started_at == started_at
    assert report.zero_usage_window_complete is True
    assert report.zero_usage_seconds_remaining == 0
    assert report.removal_authorized is False
    assert report.removal_blockers == (
        "legacy_approval_callers_not_confirmed_migrated",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("context_update", "expected_reason"),
    (
        ({}, "interaction_service_missing"),
        ({"service": True}, "chat_identity_missing"),
        (
            {"service": True, "conversation": True},
            "invocation_identity_missing",
        ),
    ),
)
async def test_bridge_observes_only_legitimate_legacy_fallbacks(
    tmp_path,
    context_update,
    expected_reason,
) -> None:
    approvals = ApprovalService()
    pending = await _pending(approvals)
    interactions = InteractionService(tmp_path / "interactions.sqlite3")
    store = SQLiteLegacyApprovalCompatibilityStore(
        tmp_path / "interaction-compatibility.db",
        "default",
    )
    await store.start()
    context = {"_legacy_approval_compatibility": store}
    if context_update.get("service"):
        context["_interaction_service"] = interactions
    if context_update.get("conversation"):
        context["os_conversation_id"] = "chat-1"

    attached = await attach_pending_to_interaction(
        context,
        pending,
        approvals,
        source="tool_guard",
        input_data={},
    )
    report = await store.report()

    assert attached is True
    assert report.total_hits == 1
    assert report.hits[0].reason == expected_reason


@pytest.mark.asyncio
async def test_durable_interaction_path_does_not_count_as_legacy(
    tmp_path,
) -> None:
    approvals = ApprovalService()
    pending = await _pending(approvals)
    interactions = InteractionService(tmp_path / "interactions.sqlite3")
    store = SQLiteLegacyApprovalCompatibilityStore(
        tmp_path / "interaction-compatibility.db",
        "default",
    )
    await store.start()

    attached = await attach_pending_to_interaction(
        {
            "_interaction_service": interactions,
            "_legacy_approval_compatibility": store,
            "os_conversation_id": "chat-1",
            "os_invocation_id": str(INVOCATION_ID),
        },
        pending,
        approvals,
        source="tool_guard",
        input_data={},
    )
    report = await store.report()

    assert attached is True
    assert report.total_hits == 0
    assert (
        len(
            await interactions.list_open(
                agent_id="default",
                conversation_id="chat-1",
            ),
        )
        == 1
    )


@pytest.mark.asyncio
async def test_report_api_reads_workspace_service(tmp_path) -> None:
    store = SQLiteLegacyApprovalCompatibilityStore(
        tmp_path / "interaction-compatibility.db",
        "default",
    )
    await store.start()

    payload = await legacy_approval_compatibility(
        SimpleNamespace(legacy_approval_compatibility=store),
    )

    assert payload["schema_version"] == (
        "qwenpaw.legacy-approval-compatibility.v1"
    )
    assert payload["total_hits"] == 0
