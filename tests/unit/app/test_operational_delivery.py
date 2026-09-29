# -*- coding: utf-8 -*-
"""Workspace assembly tests for operational Delivery and Inbox."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from qwenpaw.app.operational_delivery import (
    operational_event_publisher_for_workspace,
    operational_delivery_service_for_workspace,
)
from qwenpaw.app.mail.monitor import publish_mail_event
from qwenpaw.app.crons.heartbeat import publish_heartbeat_event
from qwenpaw.app.crons.manager import publish_cron_event
from qwenpaw.agents.memory.reme_inbox import emit_job_result
from qwenpaw.inbox import SQLiteInboxProjectionStore
from qwenpaw.kernel import OperationalEvent
from qwenpaw.operations import SQLiteOperationalEventStore
from qwenpaw.plugins.generations import GenerationRegistry


@pytest.mark.asyncio
async def test_workspace_service_commits_and_projects_operational_fact(
    tmp_path: Path,
) -> None:
    """The assembly pins the workspace generation and durable stores."""
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        capability_registry=GenerationRegistry(),
    )
    service = await operational_delivery_service_for_workspace(workspace)
    generation = workspace.capability_registry.generation

    result = await service.publish(
        OperationalEvent(
            agent_id="default",
            producer_id="qwenpaw.system.skills",
            event_type="auto_sync",
            idempotency_key="sync:digest",
            source_type="skill_autoupdate",
            source_status="skipped",
            status="success",
            title="Auto Sync: 1 skill(s) synced",
            body="s1 → default",
            payload={"synced": ["s1"]},
            registry_generation=generation,
        ),
    )

    assert result.inbox_item.source_event_id == result.event.event_id
    assert result.inbox_item.source_status == "skipped"
    items = await SQLiteInboxProjectionStore(
        tmp_path / ".qwenpaw" / "lite" / "inbox.db",
    ).list_items(agent_id="default")
    assert items == (result.inbox_item,)


@pytest.mark.asyncio
async def test_mail_adapter_preserves_source_metadata_and_deduplicates(
    tmp_path: Path,
) -> None:
    """Mail facts share Delivery while retaining released Inbox fields."""
    workspace = SimpleNamespace(
        agent_id="mail-agent",
        workspace_dir=tmp_path,
        capability_registry=GenerationRegistry(),
    )
    payload = {
        "uid": 42,
        "folder": "INBOX",
        "from": "sender@example.com",
    }

    for _ in range(2):
        await publish_mail_event(
            workspace,
            agent_id="mail-agent",
            event_type="new_email",
            status="success",
            severity="warning",
            title="New email: status",
            body="From: sender@example.com",
            payload=payload,
        )

    data_dir = tmp_path / ".qwenpaw" / "lite"
    items = await SQLiteInboxProjectionStore(
        data_dir / "inbox.db",
    ).list_items(agent_id="mail-agent")

    assert len(items) == 1
    assert items[0].source_type == "mail"
    assert items[0].source_id == "_mail_monitor"
    assert items[0].event_type == "new_email"
    assert items[0].severity == "warning"
    assert items[0].source_payload == payload


@pytest.mark.asyncio
async def test_mail_adapter_rejects_cross_agent_workspace(
    tmp_path: Path,
) -> None:
    """A monitor cannot publish into a different Agent's projection."""
    workspace = SimpleNamespace(
        agent_id="agent-a",
        workspace_dir=tmp_path,
        capability_registry=GenerationRegistry(),
    )

    with pytest.raises(ValueError, match="does not own"):
        await publish_mail_event(
            workspace,
            agent_id="agent-b",
            event_type="new_email",
            status="success",
            title="New email",
            body="",
        )


@pytest.mark.asyncio
async def test_reme_result_uses_injected_operational_publisher(
    tmp_path: Path,
) -> None:
    """Memory job policy commits through the plugin-facing host service."""
    workspace = SimpleNamespace(
        agent_id="memory-agent",
        workspace_dir=tmp_path,
        capability_registry=GenerationRegistry(),
    )
    publisher = operational_event_publisher_for_workspace(
        workspace,
        producer_id="qwenpaw.system.memory",
    )
    response = SimpleNamespace(
        success=True,
        answer="Daily memory digest generated.",
        metadata={"digest_path": "memory/2026-09-29/digest.md"},
    )
    memory_config = SimpleNamespace(
        daily_paper_inbox_push_enabled=True,
    )

    emitted = await emit_job_result(
        agent_id="memory-agent",
        memory_config=memory_config,
        name="daily_paper",
        response=response,
        kwargs={"date": "2026-09-29"},
        append_event=publisher,
    )

    data_dir = tmp_path / ".qwenpaw" / "lite"
    events = await SQLiteOperationalEventStore(
        data_dir / "operations.db",
    ).list_events(
        agent_id="memory-agent",
        producer_id="qwenpaw.system.memory",
    )
    items = await SQLiteInboxProjectionStore(
        data_dir / "inbox.db",
    ).list_items(agent_id="memory-agent")

    assert emitted is True
    assert len(events) == 1
    assert events[0].source_type == "memory"
    assert events[0].source_id == "daily_paper"
    assert len(items) == 1
    assert items[0].source_event_id == events[0].event_id


@pytest.mark.asyncio
async def test_legacy_schedule_adapters_share_operational_delivery(
    tmp_path: Path,
) -> None:
    """Cron fallbacks retain source identity without the JSON Inbox."""
    workspace = SimpleNamespace(
        agent_id="schedule-agent",
        workspace_dir=tmp_path,
        capability_registry=GenerationRegistry(),
    )
    await publish_cron_event(
        workspace,
        agent_id="schedule-agent",
        source_id="cron-1",
        event_type="cron_result",
        status="success",
        severity="info",
        title="Cron result",
        body="Completed",
        payload={"job_id": "cron-1"},
    )
    await publish_heartbeat_event(
        workspace,
        agent_id="schedule-agent",
        event_type="heartbeat_result",
        status="success",
        severity="info",
        title="Heartbeat result",
        body="Completed",
        payload={"run_id": "heartbeat-1"},
    )

    data_dir = tmp_path / ".qwenpaw" / "lite"
    events = await SQLiteOperationalEventStore(
        data_dir / "operations.db",
    ).list_events(agent_id="schedule-agent")
    items = await SQLiteInboxProjectionStore(
        data_dir / "inbox.db",
    ).list_items(agent_id="schedule-agent")

    assert {event.producer_id for event in events} == {
        "qwenpaw.system.cron",
        "qwenpaw.system.heartbeat",
    }
    assert {item.source_type for item in items} == {"cron", "heartbeat"}
