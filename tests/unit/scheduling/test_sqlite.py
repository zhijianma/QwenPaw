# -*- coding: utf-8 -*-
"""Focused tests for the Lite durable Scheduler Store."""

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from qwenpaw.kernel import (
    ScheduleCursorConflictError,
    ScheduleDefinition,
    ScheduleDefinitionNotFoundError,
    ScheduleFire,
    ScheduleFireConflictError,
    ScheduleLease,
    ScheduleLeaseConflictError,
    ScheduleLeaseStatus,
    ScheduleTrigger,
    ScheduleTriggerCursor,
    ScheduleTriggerCursorStore,
    SchedulerPort,
)
from qwenpaw.scheduling import SQLiteSchedulerStore


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _definition(
    schedule_id: str = "reports.daily",
    *,
    agent_id: str = "default",
    enabled: bool = True,
) -> ScheduleDefinition:
    return ScheduleDefinition(
        schedule_id=schedule_id,
        agent_id=agent_id,
        name="Daily report",
        objective="Prepare the daily report",
        trigger=ScheduleTrigger(kind="cron", cron="0 9 * * mon-fri"),
        runner_id="qwenpaw.system.tasks.console-agent",
        strategy_id="qwenpaw.system.tasks.default-strategy",
        enabled=enabled,
    )


def _fire(
    *,
    agent_id: str = "default",
    schedule_id: str = "reports.daily",
    idempotency_key: str = "reports.daily:2026-09-28T09:00:00Z",
) -> ScheduleFire:
    return ScheduleFire(
        agent_id=agent_id,
        schedule_id=schedule_id,
        scheduled_for=_now(),
        idempotency_key=idempotency_key,
    )


@pytest.mark.asyncio
async def test_store_satisfies_port_and_orders_definition_catalog(
    tmp_path,
) -> None:
    store = SQLiteSchedulerStore(tmp_path / "scheduler.db")

    assert isinstance(store, SchedulerPort)
    assert isinstance(store, ScheduleTriggerCursorStore)
    await store.upsert(_definition("reports.weekly"))
    await store.upsert(_definition("reports.daily"))
    await store.upsert(
        _definition("reports.daily").model_copy(
            update={"name": "Updated daily report"},
        ),
    )

    definitions = await store.list_definitions(agent_id="default")
    assert [item.schedule_id for item in definitions] == [
        "reports.daily",
        "reports.weekly",
    ]
    assert definitions[0].name == "Updated daily report"
    assert (
        await store.remove(
            agent_id="default",
            schedule_id="reports.weekly",
        )
        is True
    )
    assert (
        await store.remove(
            agent_id="default",
            schedule_id="reports.weekly",
        )
        is False
    )


@pytest.mark.asyncio
async def test_same_schedule_identity_is_isolated_by_agent(tmp_path) -> None:
    store = SQLiteSchedulerStore(tmp_path / "scheduler.db")
    await store.upsert(_definition(agent_id="agent-a"))
    await store.upsert(_definition(agent_id="agent-b"))
    scheduled_for = _now()
    first_fire = _fire(agent_id="agent-a")
    first_fire = first_fire.model_copy(
        update={"scheduled_for": scheduled_for},
    )
    second_fire = _fire(agent_id="agent-b")
    second_fire = second_fire.model_copy(
        update={"scheduled_for": scheduled_for},
    )

    first = await store.claim(
        first_fire,
        owner_id="worker.a",
        lease_seconds=30,
    )
    second = await store.claim(
        second_fire,
        owner_id="worker.b",
        lease_seconds=30,
    )

    assert first.lease_id != second.lease_id
    assert first.fire.agent_id == "agent-a"
    assert second.fire.agent_id == "agent-b"
    assert [
        item.agent_id
        for item in await store.list_definitions(agent_id="agent-a")
    ] == ["agent-a"]
    assert [
        item.agent_id
        for item in await store.list_definitions(agent_id="agent-b")
    ] == ["agent-b"]
    assert await store.remove(
        agent_id="agent-a",
        schedule_id="reports.daily",
    )
    assert await store.list_definitions(agent_id="agent-a") == ()
    assert len(await store.list_definitions(agent_id="agent-b")) == 1


@pytest.mark.asyncio
async def test_store_migrates_legacy_single_agent_tables(tmp_path) -> None:
    path = tmp_path / "scheduler.db"
    definition = _definition(agent_id="legacy-agent")
    fire = _fire(agent_id="legacy-agent")
    now = _now()
    lease = ScheduleLease(
        fire=fire,
        owner_id="legacy-worker",
        acquired_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    legacy_lease = lease.model_dump(mode="json", by_alias=True)
    legacy_lease["fire"].pop("agent_id")
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE schedule_definitions (
                schedule_id TEXT PRIMARY KEY,
                definition_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE schedule_fire_leases (
                lease_id TEXT PRIMARY KEY,
                fire_id TEXT NOT NULL UNIQUE,
                schedule_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                status TEXT NOT NULL,
                revision INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                lease_json TEXT NOT NULL,
                UNIQUE(schedule_id, idempotency_key)
            );
            """,
        )
        connection.execute(
            """
            INSERT INTO schedule_definitions (
                schedule_id, definition_json, updated_at
            ) VALUES (?, ?, ?)
            """,
            (
                definition.schedule_id,
                definition.model_dump_json(by_alias=True),
                now.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO schedule_fire_leases (
                lease_id, fire_id, schedule_id, idempotency_key,
                owner_id, status, revision, expires_at, lease_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(lease.lease_id),
                str(fire.fire_id),
                fire.schedule_id,
                fire.idempotency_key,
                lease.owner_id,
                lease.status.value,
                lease.revision,
                lease.expires_at.isoformat(),
                json.dumps(legacy_lease),
            ),
        )

    store = SQLiteSchedulerStore(path)
    assert await store.list_definitions(agent_id="legacy-agent") == (
        definition,
    )
    migrated = await store.claim(
        fire,
        owner_id="another-worker",
        lease_seconds=30,
    )
    assert migrated == lease


@pytest.mark.asyncio
async def test_concurrent_claim_replays_one_idempotent_lease(tmp_path) -> None:
    path = tmp_path / "scheduler.db"
    first = SQLiteSchedulerStore(path)
    second = SQLiteSchedulerStore(path)
    await first.upsert(_definition())
    fire = _fire()

    leases = await asyncio.gather(
        first.claim(fire, owner_id="worker.first", lease_seconds=30),
        second.claim(fire, owner_id="worker.second", lease_seconds=30),
    )

    assert leases[0] == leases[1]
    assert leases[0].owner_id in {"worker.first", "worker.second"}
    assert leases[0].revision == 1


@pytest.mark.asyncio
async def test_reconstructed_fire_replays_despite_new_observation_time(
    tmp_path,
) -> None:
    store = SQLiteSchedulerStore(tmp_path / "scheduler.db")
    await store.upsert(_definition())
    scheduled_for = _now()
    first_fire = _fire().model_copy(
        update={"scheduled_for": scheduled_for},
    )
    replayed_fire = _fire().model_copy(
        update={"scheduled_for": scheduled_for},
    )

    first = await store.claim(
        first_fire,
        owner_id="worker.first",
        lease_seconds=30,
    )
    replay = await store.claim(
        replayed_fire,
        owner_id="worker.second",
        lease_seconds=30,
    )

    assert replay == first


@pytest.mark.asyncio
async def test_claim_rejects_missing_disabled_and_conflicting_fire(
    tmp_path,
) -> None:
    store = SQLiteSchedulerStore(tmp_path / "scheduler.db")
    with pytest.raises(ScheduleDefinitionNotFoundError):
        await store.claim(
            _fire(schedule_id="missing.schedule"),
            owner_id="worker.local",
            lease_seconds=30,
        )

    await store.upsert(_definition(enabled=False))
    with pytest.raises(ScheduleLeaseConflictError, match="disabled"):
        await store.claim(
            _fire(),
            owner_id="worker.local",
            lease_seconds=30,
        )

    await store.upsert(_definition())
    fire = _fire()
    await store.claim(
        fire,
        owner_id="worker.local",
        lease_seconds=30,
    )
    with pytest.raises(ScheduleFireConflictError):
        await store.claim(
            _fire(idempotency_key=fire.idempotency_key),
            owner_id="worker.local",
            lease_seconds=30,
        )


@pytest.mark.asyncio
async def test_renew_and_complete_enforce_owner_revision_and_expiry(
    tmp_path,
) -> None:
    store = SQLiteSchedulerStore(tmp_path / "scheduler.db")
    await store.upsert(_definition())
    claimed = await store.claim(
        _fire(),
        owner_id="worker.local",
        lease_seconds=30,
    )

    with pytest.raises(ScheduleLeaseConflictError, match="owner"):
        await store.renew(
            claimed.lease_id,
            owner_id="worker.other",
            expected_revision=claimed.revision,
            lease_seconds=30,
        )
    renewed = await store.renew(
        claimed.lease_id,
        owner_id="worker.local",
        expected_revision=claimed.revision,
        lease_seconds=60,
    )
    assert renewed.revision == 2
    assert renewed.expires_at > claimed.expires_at

    with pytest.raises(ScheduleLeaseConflictError, match="revision"):
        await store.complete(
            renewed.lease_id,
            owner_id="worker.local",
            expected_revision=1,
            task_id=uuid4(),
        )
    task_id = uuid4()
    run_id = uuid4()
    completed = await store.complete(
        renewed.lease_id,
        owner_id="worker.local",
        expected_revision=renewed.revision,
        task_id=task_id,
        run_id=run_id,
    )
    assert completed.status is ScheduleLeaseStatus.COMPLETED
    assert completed.task_id == task_id
    assert completed.run_id == run_id
    assert completed.revision == 3
    with pytest.raises(ScheduleLeaseConflictError, match="status"):
        await store.renew(
            completed.lease_id,
            owner_id="worker.local",
            expected_revision=completed.revision,
            lease_seconds=30,
        )


@pytest.mark.asyncio
async def test_complete_supports_non_task_completion_reference(
    tmp_path,
) -> None:
    store = SQLiteSchedulerStore(tmp_path / "scheduler.db")
    await store.upsert(_definition())
    claimed = await store.claim(
        _fire(idempotency_key="service:memory:dream"),
        owner_id="worker.service",
        lease_seconds=30,
    )

    completed = await store.complete(
        claimed.lease_id,
        owner_id="worker.service",
        expected_revision=claimed.revision,
        completion_ref="service:memory:dream",
    )

    assert completed.status is ScheduleLeaseStatus.COMPLETED
    assert completed.task_id is None
    assert completed.completion_ref == "service:memory:dream"


@pytest.mark.asyncio
async def test_fail_and_recover_expired_create_terminal_facts(
    tmp_path,
) -> None:
    store = SQLiteSchedulerStore(tmp_path / "scheduler.db")
    await store.upsert(_definition("reports.first"))
    await store.upsert(_definition("reports.second"))
    failed_claim = await store.claim(
        _fire(
            schedule_id="reports.first",
            idempotency_key="first-fire",
        ),
        owner_id="worker.local",
        lease_seconds=30,
    )
    retry_at = _now() + timedelta(minutes=5)
    failed = await store.fail(
        failed_claim.lease_id,
        owner_id="worker.local",
        expected_revision=1,
        error_code="task_create_failed",
        retry_at=retry_at,
    )
    assert failed.status is ScheduleLeaseStatus.FAILED
    assert failed.error_code == "task_create_failed"
    assert failed.retry_at == retry_at

    expiring = await store.claim(
        _fire(
            schedule_id="reports.second",
            idempotency_key="second-fire",
        ),
        owner_id="worker.dead",
        lease_seconds=0.01,
    )
    recovered = await store.recover_expired(
        agent_id="default",
        now=expiring.expires_at + timedelta(seconds=1),
    )

    assert len(recovered) == 1
    assert recovered[0].lease_id == expiring.lease_id
    assert recovered[0].status is ScheduleLeaseStatus.FAILED
    assert recovered[0].error_code == "lease_expired"
    assert recovered[0].revision == 2
    assert (
        await store.recover_expired(
            agent_id="default",
            now=_now() + timedelta(days=1),
        )
        == ()
    )


@pytest.mark.asyncio
async def test_expired_recovery_can_be_scoped_to_one_schedule(
    tmp_path,
) -> None:
    store = SQLiteSchedulerStore(tmp_path / "scheduler.db")
    await store.upsert(_definition("reports.first"))
    await store.upsert(_definition("reports.second"))
    fires = {}
    for schedule_id in ("reports.first", "reports.second"):
        fires[schedule_id] = await store.claim(
            _fire(
                schedule_id=schedule_id,
                idempotency_key=f"{schedule_id}:fire",
            ),
            owner_id="worker.local",
            lease_seconds=0.01,
        )

    recovered = await store.recover_expired(
        agent_id="default",
        schedule_id="reports.first",
        now=_now() + timedelta(seconds=1),
    )

    assert [item.fire.schedule_id for item in recovered] == [
        "reports.first",
    ]
    second = await store.claim(
        fires["reports.second"].fire,
        owner_id="worker.other",
        lease_seconds=30,
    )
    assert second.status is ScheduleLeaseStatus.CLAIMED


@pytest.mark.asyncio
async def test_trigger_cursor_survives_reconcile_and_advances_with_cas(
    tmp_path,
) -> None:
    store = SQLiteSchedulerStore(tmp_path / "scheduler.db")
    first_at = datetime(2026, 10, 10, 9, tzinfo=timezone.utc)
    cursor = ScheduleTriggerCursor(
        agent_id="agent-a",
        schedule_id="reports.daily",
        definition_hash="sha256:first",
        next_fire_at=first_at,
    )

    created = await store.reconcile_cursor(cursor)
    replayed = await SQLiteSchedulerStore(
        tmp_path / "scheduler.db",
    ).reconcile_cursor(
        cursor.model_copy(
            update={"next_fire_at": first_at + timedelta(days=7)},
        ),
    )
    assert replayed == created

    [due] = await store.list_due_cursors(
        agent_id="agent-a",
        now=first_at,
    )
    next_at = first_at + timedelta(days=1)
    advanced = await store.advance_cursor(
        agent_id="agent-a",
        schedule_id="reports.daily",
        definition_hash="sha256:first",
        expected_revision=due.revision,
        scheduled_for=first_at,
        next_fire_at=next_at,
    )

    assert advanced.revision == 2
    assert advanced.last_fire_at == first_at
    assert advanced.next_fire_at == next_at
    assert (
        await store.list_due_cursors(
            agent_id="agent-a",
            now=first_at,
        )
        == ()
    )
    with pytest.raises(ScheduleCursorConflictError, match="revision"):
        await store.advance_cursor(
            agent_id="agent-a",
            schedule_id="reports.daily",
            definition_hash="sha256:first",
            expected_revision=due.revision,
            scheduled_for=first_at,
            next_fire_at=next_at,
        )


@pytest.mark.asyncio
async def test_trigger_retry_deferral_survives_restart_and_uses_cas(
    tmp_path,
) -> None:
    path = tmp_path / "scheduler.db"
    store = SQLiteSchedulerStore(path)
    scheduled_for = datetime(2026, 10, 10, 9, tzinfo=timezone.utc)
    original = await store.reconcile_cursor(
        ScheduleTriggerCursor(
            agent_id="agent-a",
            schedule_id="reports.daily",
            definition_hash="sha256:first",
            next_fire_at=scheduled_for,
        ),
    )
    retry_at = scheduled_for + timedelta(seconds=15)

    deferred = await store.defer_cursor(
        agent_id=original.agent_id,
        schedule_id=original.schedule_id,
        definition_hash=original.definition_hash,
        expected_revision=original.revision,
        scheduled_for=scheduled_for,
        retry_not_before=retry_at,
    )

    assert deferred.revision == 2
    assert deferred.next_fire_at == scheduled_for
    assert deferred.retry_not_before == retry_at
    assert deferred.retry_count == 1
    reopened = SQLiteSchedulerStore(path)
    assert (
        await reopened.list_due_cursors(
            agent_id="agent-a",
            now=retry_at - timedelta(microseconds=1),
        )
        == ()
    )
    assert await reopened.list_due_cursors(
        agent_id="agent-a",
        now=retry_at,
    ) == (deferred,)
    with pytest.raises(ScheduleCursorConflictError, match="revision"):
        await reopened.defer_cursor(
            agent_id=original.agent_id,
            schedule_id=original.schedule_id,
            definition_hash=original.definition_hash,
            expected_revision=original.revision,
            scheduled_for=scheduled_for,
            retry_not_before=retry_at,
        )


@pytest.mark.asyncio
async def test_existing_cursor_table_adds_retry_query_column(tmp_path) -> None:
    path = tmp_path / "scheduler.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE schedule_trigger_cursors (
                agent_id TEXT NOT NULL,
                schedule_id TEXT NOT NULL,
                definition_hash TEXT NOT NULL,
                revision INTEGER NOT NULL,
                next_fire_at TEXT,
                cursor_json TEXT NOT NULL,
                PRIMARY KEY(agent_id, schedule_id)
            )
            """,
        )
    store = SQLiteSchedulerStore(path)

    await store.list_due_cursors(agent_id="agent-a", now=_now())

    with sqlite3.connect(path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(schedule_trigger_cursors)",
            ).fetchall()
        }
    assert "retry_not_before" in columns


@pytest.mark.asyncio
async def test_definition_change_resets_cursor_without_cross_agent_leak(
    tmp_path,
) -> None:
    store = SQLiteSchedulerStore(tmp_path / "scheduler.db")
    first_at = datetime(2026, 10, 10, 9, tzinfo=timezone.utc)
    for agent_id in ("agent-a", "agent-b"):
        await store.reconcile_cursor(
            ScheduleTriggerCursor(
                agent_id=agent_id,
                schedule_id="reports.daily",
                definition_hash="sha256:first",
                next_fire_at=first_at,
            ),
        )
    changed = await store.reconcile_cursor(
        ScheduleTriggerCursor(
            agent_id="agent-a",
            schedule_id="reports.daily",
            definition_hash="sha256:changed",
            next_fire_at=first_at + timedelta(hours=1),
        ),
    )

    assert changed.revision == 2
    assert changed.definition_hash == "sha256:changed"
    assert changed.last_fire_at is None
    [agent_b] = await store.list_due_cursors(
        agent_id="agent-b",
        now=first_at,
    )
    assert agent_b.definition_hash == "sha256:first"
    assert await store.remove_cursor(
        agent_id="agent-a",
        schedule_id="reports.daily",
        expected_definition_hash="sha256:changed",
        expected_revision=changed.revision,
    )
    assert not await store.remove_cursor(
        agent_id="agent-a",
        schedule_id="reports.daily",
    )


@pytest.mark.asyncio
async def test_cursor_guarded_remove_rejects_stale_identity(tmp_path) -> None:
    store = SQLiteSchedulerStore(tmp_path / "scheduler.db")
    cursor = await store.reconcile_cursor(
        ScheduleTriggerCursor(
            agent_id="agent-a",
            schedule_id="reports.daily",
            definition_hash="sha256:current",
            next_fire_at=_now(),
        ),
    )

    assert not await store.remove_cursor(
        agent_id=cursor.agent_id,
        schedule_id=cursor.schedule_id,
        expected_definition_hash="sha256:stale",
        expected_revision=cursor.revision,
    )
    assert (
        await store.get_cursor(
            agent_id=cursor.agent_id,
            schedule_id=cursor.schedule_id,
        )
        == cursor
    )
    with pytest.raises(ValueError, match="hash and revision"):
        await store.remove_cursor(
            agent_id=cursor.agent_id,
            schedule_id=cursor.schedule_id,
            expected_revision=cursor.revision,
        )
