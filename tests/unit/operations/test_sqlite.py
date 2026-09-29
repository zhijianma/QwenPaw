# -*- coding: utf-8 -*-
"""Tests for the Lite operational event source store."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from qwenpaw.kernel import (
    OperationalEvent,
    OperationalEventConflictError,
)
from qwenpaw.operations import SQLiteOperationalEventStore


def _event(*, agent_id: str = "default", title: str = "Synced"):
    return OperationalEvent(
        agent_id=agent_id,
        producer_id="qwenpaw.system.skills",
        event_type="skill.auto_sync",
        idempotency_key="sync:abc",
        source_type="skill_autoupdate",
        status="success",
        title=title,
        body="One skill was synchronized.",
    )


@pytest.mark.asyncio
async def test_commit_replays_identical_event_after_reopen(
    tmp_path: Path,
) -> None:
    """A restart observes the same immutable operational fact."""
    path = tmp_path / "operations.db"
    event = _event()

    first = await SQLiteOperationalEventStore(path).commit(event)
    replay = await SQLiteOperationalEventStore(path).commit(
        event.model_copy(
            update={
                "occurred_at": datetime(
                    2030,
                    1,
                    1,
                    tzinfo=timezone.utc,
                ),
            },
        ),
    )

    assert replay == first
    assert await SQLiteOperationalEventStore(path).get(first.event_id) == first


@pytest.mark.asyncio
async def test_concurrent_commit_is_idempotent(tmp_path: Path) -> None:
    """Concurrent writers expose one source fact."""
    store = SQLiteOperationalEventStore(tmp_path / "operations.db")
    event = _event()

    results = await asyncio.gather(
        store.commit(event),
        store.commit(event),
    )

    assert results == [event, event]
    assert await store.list_events(agent_id="default") == (event,)


@pytest.mark.asyncio
async def test_conflicting_reuse_is_rejected(tmp_path: Path) -> None:
    """A stable key cannot be reused for different operational content."""
    store = SQLiteOperationalEventStore(tmp_path / "operations.db")
    await store.commit(_event())

    with pytest.raises(OperationalEventConflictError):
        await store.commit(_event(title="Different"))


@pytest.mark.asyncio
async def test_list_is_agent_and_producer_scoped(tmp_path: Path) -> None:
    """Queries cannot leak another Agent's source facts."""
    store = SQLiteOperationalEventStore(tmp_path / "operations.db")
    await store.commit(_event(agent_id="agent-a"))
    await store.commit(_event(agent_id="agent-b"))

    events = await store.list_events(
        agent_id="agent-a",
        producer_id="qwenpaw.system.skills",
    )

    assert len(events) == 1
    assert events[0].agent_id == "agent-a"
