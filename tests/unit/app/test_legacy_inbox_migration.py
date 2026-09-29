# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Tests for non-destructive legacy Inbox migration."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qwenpaw.app.legacy_inbox_migration import (
    migrate_legacy_inbox_events,
)
from qwenpaw.app.legacy_inbox_observation import (
    SQLiteLegacyInboxObservationStore,
)
from qwenpaw.app.workspace.workspace import Workspace
from qwenpaw.inbox import SQLiteInboxProjectionStore
from qwenpaw.operations import SQLiteOperationalEventStore
from qwenpaw.plugins.generations import GenerationRegistry


def test_workspace_registers_optional_one_shot_migration(
    tmp_path: Path,
) -> None:
    workspace = Workspace("default", str(tmp_path))

    descriptor = workspace._service_manager.descriptors[
        "legacy_inbox_migration"
    ]

    assert descriptor.start_method == "start"
    assert descriptor.optional is True
    assert descriptor.priority == 55


@pytest.mark.asyncio
async def test_migration_replays_read_row_without_mutating_source(
    tmp_path: Path,
) -> None:
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        capability_registry=GenerationRegistry(),
    )
    rows = [
        {
            "id": "legacy-1",
            "agent_id": "default",
            "source_type": "cron",
            "source_id": "job-1",
            "event_type": "Cron Timeout",
            "status": "timeout",
            "severity": "warning",
            "title": "Cron timed out",
            "body": "The job exceeded its budget.",
            "payload": {"job_id": "job-1"},
            "read": True,
            "created_at": 1_700_000_000.0,
        },
        {
            "agent_id": "default",
            "title": "Missing stable identity",
        },
        {
            "id": "foreign-1",
            "agent_id": "another-agent",
            "title": "Foreign event",
        },
    ]
    original = deepcopy(rows)

    first = await migrate_legacy_inbox_events(workspace, rows)
    replay = await migrate_legacy_inbox_events(workspace, rows)

    assert first == replay
    assert first.attempted == 3
    assert first.migrated == 1
    assert first.failed == 2
    assert rows == original

    data_dir = tmp_path / ".qwenpaw" / "lite"
    events = await SQLiteOperationalEventStore(
        data_dir / "operations.db",
    ).list_events(agent_id="default")
    items = await SQLiteInboxProjectionStore(
        data_dir / "inbox.db",
    ).list_items(
        agent_id="default",
        include_handled=True,
    )

    assert len(events) == 1
    assert events[0].producer_id == "qwenpaw.legacy.inbox"
    assert events[0].event_type == "cron-timeout"
    assert events[0].source_status == "timeout"
    assert len(items) == 1
    assert items[0].read
    assert items[0].source_status == "timeout"
    assert items[0].source_payload["legacy_event_id"] == "legacy-1"
    assert items[0].source_payload["job_id"] == "job-1"
    assert items[0].created_at.timestamp() == 1_700_000_000.0
    observation = await SQLiteLegacyInboxObservationStore(
        data_dir / "inbox.db",
    ).get(agent_id="default")
    assert observation is not None
    assert observation.scan_count == 2
    assert observation.cumulative_attempted == 6
    assert observation.cumulative_migrated == 2
    assert observation.cumulative_failed == 4
    assert observation.stable_clean_scans == 0
    assert observation.last_scan_error is not None


@pytest.mark.asyncio
async def test_changed_legacy_row_conflicts_without_replacing_projection(
    tmp_path: Path,
) -> None:
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        capability_registry=GenerationRegistry(),
    )
    row = {
        "id": "legacy-1",
        "agent_id": "default",
        "source_type": "mail",
        "source_id": "message-1",
        "event_type": "new_email",
        "status": "success",
        "severity": "info",
        "title": "Original",
        "body": "Original body",
        "payload": {},
        "read": False,
        "created_at": 1_700_000_000.0,
    }

    first = await migrate_legacy_inbox_events(workspace, [row])
    changed = await migrate_legacy_inbox_events(
        workspace,
        [{**row, "body": "Changed body"}],
    )

    assert first.migrated == 1
    assert changed.migrated == 0
    assert changed.failed == 1
    items = await SQLiteInboxProjectionStore(
        tmp_path / ".qwenpaw" / "lite" / "inbox.db",
    ).list_items(agent_id="default")
    assert len(items) == 1
    assert items[0].summary == "Original body"


@pytest.mark.asyncio
async def test_service_start_failure_is_observed_and_redacted(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
    )
    monkeypatch.setattr(
        "qwenpaw.app.legacy_inbox_migration."
        "operational_delivery_service_for_workspace",
        AsyncMock(
            side_effect=RuntimeError("token=supersecret"),
        ),
    )

    with pytest.raises(RuntimeError, match="supersecret"):
        await migrate_legacy_inbox_events(
            workspace,
            [{"id": "legacy-1"}],
        )

    observation = await SQLiteLegacyInboxObservationStore(
        tmp_path / ".qwenpaw" / "lite" / "inbox.db",
    ).get(agent_id="default")
    assert observation is not None
    assert observation.last_failed == 1
    assert observation.last_scan_error is not None
    assert "supersecret" not in observation.last_scan_error
    assert "<redacted>" in observation.last_scan_error
