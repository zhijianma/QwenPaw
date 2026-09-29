# -*- coding: utf-8 -*-
"""Console compatibility API over legacy and durable Inbox sources."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from qwenpaw.app.routers.console import (
    MarkInboxReadRequest,
    delete_inbox_event,
    get_inbox_events,
    get_inbox_migration_observation,
    post_mark_inbox_read,
)
from qwenpaw.app.legacy_inbox_observation import (
    SQLiteLegacyInboxObservationStore,
)
from qwenpaw.kernel.models import utc_now
from qwenpaw.inbox import SQLiteInboxProjectionStore
from qwenpaw.kernel import (
    DeliveryDestination,
    DeliveryKind,
    DeliveryMode,
    DeliveryReceipt,
    DeliveryRequest,
    DeliveryStatus,
)


async def _project(workspace) -> str:
    request = DeliveryRequest(
        source_event_id=uuid4(),
        idempotency_key="console-inbox",
        agent_id="default",
        kind=DeliveryKind.RESULT,
        mode=DeliveryMode.FINAL,
        destination=DeliveryDestination(
            adapter_id="test.delivery",
            address="opaque",
        ),
        task_id=uuid4(),
        run_id=uuid4(),
        payload={"text": "Durable task result"},
    )
    assert request.delivery_id is not None
    await SQLiteInboxProjectionStore(
        Path(workspace.workspace_dir) / ".qwenpaw" / "lite" / "inbox.db",
    ).project(
        request,
        DeliveryReceipt(
            delivery_id=request.delivery_id,
            adapter_id=request.destination.adapter_id,
            status=DeliveryStatus.DELIVERED,
        ),
    )
    return str(request.delivery_id)


async def _project_skill_event(workspace) -> str:
    request = DeliveryRequest(
        source_event_id=uuid4(),
        idempotency_key="console-skill-event",
        agent_id="default",
        kind=DeliveryKind.RESULT,
        mode=DeliveryMode.FINAL,
        destination=DeliveryDestination(
            adapter_id="test.delivery",
            address="opaque",
        ),
        payload={
            "text": "translator → default",
            "title": "Auto Sync completed",
            "source_type": "skill_autoupdate",
            "event_type": "auto_sync",
            "source_status": "success",
            "severity": "info",
            "operational_payload": {
                "synced": ["translator"],
                "delivery_id": "must-not-override-system-metadata",
            },
        },
    )
    assert request.delivery_id is not None
    await SQLiteInboxProjectionStore(
        Path(workspace.workspace_dir) / ".qwenpaw" / "lite" / "inbox.db",
    ).project(
        request,
        DeliveryReceipt(
            delivery_id=request.delivery_id,
            adapter_id=request.destination.adapter_id,
            status=DeliveryStatus.DELIVERED,
        ),
    )
    return str(request.delivery_id)


async def _project_migrated_event(workspace, legacy_id: str) -> str:
    request = DeliveryRequest(
        source_event_id=uuid4(),
        idempotency_key=f"migrated-{legacy_id}",
        agent_id="default",
        kind=DeliveryKind.RESULT,
        mode=DeliveryMode.FINAL,
        destination=DeliveryDestination(
            adapter_id="test.delivery",
            address="opaque",
        ),
        payload={
            "text": "Migrated event",
            "source_type": "mail",
            "source_id": "mail-1",
            "event_type": "new_email",
            "source_status": "success",
            "operational_payload": {"legacy_event_id": legacy_id},
        },
    )
    assert request.delivery_id is not None
    await SQLiteInboxProjectionStore(
        Path(workspace.workspace_dir) / ".qwenpaw" / "lite" / "inbox.db",
    ).project(
        request,
        DeliveryReceipt(
            delivery_id=request.delivery_id,
            adapter_id=request.destination.adapter_id,
            status=DeliveryStatus.DELIVERED,
        ),
    )
    return str(request.delivery_id)


@pytest.mark.asyncio
async def test_console_merges_reads_and_handles_projection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = SimpleNamespace(agent_id="default", workspace_dir=tmp_path)
    monkeypatch.setattr(
        "qwenpaw.app.routers.console.get_agent_for_request",
        AsyncMock(return_value=workspace),
    )
    legacy = {
        "id": "legacy-1",
        "agent_id": "default",
        "source_type": "mail",
        "source_id": "mail-1",
        "event_type": "mail",
        "status": "success",
        "severity": "info",
        "title": "Legacy",
        "body": "Legacy event",
        "payload": {},
        "read": False,
        "created_at": 1.0,
    }
    monkeypatch.setattr(
        "qwenpaw.app.inbox_store.query_events",
        AsyncMock(return_value=([legacy], 1, 1)),
    )
    legacy_mark = AsyncMock(return_value=0)
    monkeypatch.setattr("qwenpaw.app.inbox_store.mark_read", legacy_mark)
    item_id = await _project(workspace)
    request = SimpleNamespace()

    page = await get_inbox_events(
        request=request,
        limit=50,
        offset=0,
        source_type=None,
        source_types=None,
        status=None,
        agent_id=None,
        unread_only=False,
    )
    assert page["total"] == 2
    assert page["unread_count"] == 2
    assert page["events"][0]["source_type"] == "task"

    marked = await post_mark_inbox_read(
        MarkInboxReadRequest(event_ids=[item_id]),
        request,
    )
    assert marked == {"updated": 1}
    legacy_mark.assert_awaited_once_with([item_id])

    deleted = await delete_inbox_event(item_id, request)
    assert deleted["deleted"] is True
    task_page = await get_inbox_events(
        request=request,
        limit=50,
        offset=0,
        source_type="task",
        source_types=None,
        status=None,
        agent_id=None,
        unread_only=False,
    )
    assert all(event["source_type"] != "task" for event in task_page["events"])


@pytest.mark.asyncio
async def test_console_filters_operational_projection_by_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = SimpleNamespace(agent_id="default", workspace_dir=tmp_path)
    monkeypatch.setattr(
        "qwenpaw.app.routers.console.get_agent_for_request",
        AsyncMock(return_value=workspace),
    )
    monkeypatch.setattr(
        "qwenpaw.app.inbox_store.query_events",
        AsyncMock(return_value=([], 0, 0)),
    )
    item_id = await _project_skill_event(workspace)

    page = await get_inbox_events(
        request=SimpleNamespace(),
        limit=50,
        offset=0,
        source_type="skill_autoupdate",
        source_types=None,
        status="success",
        agent_id=None,
        unread_only=False,
    )

    assert page["total"] == 1
    assert page["events"][0]["id"] == item_id
    assert page["events"][0]["event_type"] == "auto_sync"
    assert page["events"][0]["title"] == "Auto Sync completed"
    assert page["events"][0]["payload"]["synced"] == ["translator"]
    assert page["events"][0]["payload"]["delivery_id"] == item_id


@pytest.mark.asyncio
async def test_console_suppresses_legacy_only_after_projection_exists(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = SimpleNamespace(agent_id="default", workspace_dir=tmp_path)
    monkeypatch.setattr(
        "qwenpaw.app.routers.console.get_agent_for_request",
        AsyncMock(return_value=workspace),
    )
    legacy = {
        "id": "legacy-1",
        "agent_id": "default",
        "source_type": "mail",
        "source_id": "mail-1",
        "event_type": "new_email",
        "status": "success",
        "severity": "info",
        "title": "Legacy",
        "body": "Legacy event",
        "payload": {},
        "read": False,
        "created_at": 1.0,
    }
    monkeypatch.setattr(
        "qwenpaw.app.inbox_store.query_events",
        AsyncMock(return_value=([legacy], 1, 1)),
    )
    assert isinstance(legacy["id"], str)
    item_id = await _project_migrated_event(workspace, legacy["id"])

    page = await get_inbox_events(
        request=SimpleNamespace(),
        limit=50,
        offset=0,
        source_type=None,
        source_types=None,
        status=None,
        agent_id=None,
        unread_only=False,
    )

    assert page["total"] == 1
    assert page["events"][0]["id"] == item_id
    assert page["events"][0]["payload"]["legacy_event_id"] == "legacy-1"

    await delete_inbox_event(item_id, SimpleNamespace())
    handled_page = await get_inbox_events(
        request=SimpleNamespace(),
        limit=50,
        offset=0,
        source_type=None,
        source_types=None,
        status=None,
        agent_id=None,
        unread_only=False,
    )
    assert handled_page["total"] == 0


@pytest.mark.asyncio
async def test_console_exposes_agent_scoped_migration_observation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = SimpleNamespace(agent_id="default", workspace_dir=tmp_path)
    monkeypatch.setattr(
        "qwenpaw.app.routers.console.get_agent_for_request",
        AsyncMock(return_value=workspace),
    )
    now = utc_now()
    await SQLiteLegacyInboxObservationStore(
        tmp_path / ".qwenpaw" / "lite" / "inbox.db",
    ).record_scan(
        agent_id="default",
        started_at=now,
        completed_at=now,
        attempted=0,
        migrated=0,
        failed=0,
        source_fingerprint="a" * 64,
        scan_error=None,
    )

    assessment = await get_inbox_migration_observation(
        SimpleNamespace(),
    )

    assert not assessment.can_disable_dual_read
    assert assessment.observation is not None
    assert assessment.observation.agent_id == "default"
    assert "observation_window_incomplete" in assessment.blocker_codes
