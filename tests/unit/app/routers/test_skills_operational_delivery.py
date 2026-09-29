# -*- coding: utf-8 -*-
"""Skill automation coverage through Operational Event and Delivery."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from qwenpaw.app.routers.skills import post_auto_sync_inbox
from qwenpaw.inbox import SQLiteInboxProjectionStore
from qwenpaw.operations import SQLiteOperationalEventStore
from qwenpaw.plugins.generations import GenerationRegistry


@pytest.mark.asyncio
async def test_auto_sync_commits_source_fact_before_inbox_projection(
    tmp_path: Path,
) -> None:
    """Skill notifications use the shared durable source-to-Inbox path."""
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        capability_registry=GenerationRegistry(),
    )
    result = {
        "synced": [{"skill": "translator", "agents": ["default"]}],
        "failed": [],
    }

    assert await post_auto_sync_inbox(result, workspace=workspace) is True
    assert await post_auto_sync_inbox(result, workspace=workspace) is True

    data_dir = tmp_path / ".qwenpaw" / "lite"
    events = await SQLiteOperationalEventStore(
        data_dir / "operations.db",
    ).list_events(
        agent_id="default",
        producer_id="qwenpaw.system.skills",
    )
    items = await SQLiteInboxProjectionStore(
        data_dir / "inbox.db",
    ).list_items(agent_id="default")

    assert len(events) == 1
    assert events[0].event_type == "auto_sync"
    assert events[0].source_type == "skill_autoupdate"
    assert len(items) == 1
    assert items[0].source_event_id == events[0].event_id
    assert items[0].event_type == "auto_sync"
    assert items[0].title == "Auto Sync: 1 skill(s) synced"
