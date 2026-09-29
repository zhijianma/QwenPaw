# -*- coding: utf-8 -*-
"""Tests for durable Task side-effect accounting."""

import asyncio
import hashlib
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from qwenpaw.kernel.models import (
    ActorRef,
    ActorType,
    ApprovalDecisionValue,
    PlanStep,
    RiskLevel,
    SideEffectDisposition,
    SideEffectStatus,
    ToolEffect,
)
from qwenpaw.tasks.ledger import SQLiteExecutionLedger
from qwenpaw.tasks.service import (
    SideEffectRecoveryRequiredError,
    SideEffectRequestConflictError,
    TaskService,
)
from qwenpaw.tasks.side_effects import TaskSideEffectBroker


async def _running_broker(
    database_path: Path,
) -> tuple[TaskSideEffectBroker, SQLiteExecutionLedger]:
    store = SQLiteExecutionLedger(database_path)
    service = TaskService(store=store, registry_generation=3)
    task = await service.create_task(
        objective="Write one durable file",
        agent_id="default",
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Write", objective="Write the file"),),
    )
    _, run = await service.start_task(
        task.task_id,
        runner_id="runner.local",
    )
    return (
        TaskSideEffectBroker(
            service=service,
            task_id=task.task_id,
            run_id=run.run_id,
        ),
        store,
    )


@pytest.mark.asyncio
async def test_side_effect_reservation_replays_without_reexecution(
    tmp_path: Path,
) -> None:
    broker, store = await _running_broker(tmp_path / "ledger.db")
    request_hash = hashlib.sha256(b"same request").hexdigest()
    invocation_id = uuid4()

    first = await broker.begin(
        action="Write",
        target="README.md",
        effect=ToolEffect.LOCAL_WRITE,
        idempotency_key="tool:invocation:call-1",
        request_hash=request_hash,
        invocation_id=invocation_id,
        correlation_id=invocation_id,
        policy_decision="allow",
    )
    completed = await broker.finish(
        first.record.record_id,
        status=SideEffectStatus.SUCCEEDED,
        result_digest=hashlib.sha256(b"written").hexdigest(),
    )
    replay = await broker.begin(
        action="Write",
        target="README.md",
        effect=ToolEffect.LOCAL_WRITE,
        idempotency_key="tool:invocation:call-1",
        request_hash=request_hash,
        invocation_id=invocation_id,
        correlation_id=invocation_id,
        policy_decision="allow",
    )

    assert first.disposition is SideEffectDisposition.EXECUTE
    assert completed.status is SideEffectStatus.SUCCEEDED
    assert replay.disposition is SideEffectDisposition.REPLAY
    assert replay.record.record_id == first.record.record_id
    records = await store.list_side_effects(first.record.task_id)
    assert records == [completed]
    events = await store.list_events(first.record.task_id)
    assert [event.event_type for event in events][-2:] == [
        "side_effect.prepared",
        "side_effect.succeeded",
    ]
    assert events[-2].source == "runner.local"
    assert events[-2].correlation_id == invocation_id
    assert events[-1].source == "runner.local"
    assert events[-1].correlation_id == invocation_id


@pytest.mark.asyncio
async def test_prepared_record_blocks_retry_and_key_reuse_conflicts(
    tmp_path: Path,
) -> None:
    broker, _ = await _running_broker(tmp_path / "ledger.db")
    request_hash = hashlib.sha256(b"request").hexdigest()
    first = await broker.begin(
        action="Bash",
        target="deploy",
        effect=ToolEffect.PROCESS,
        idempotency_key="tool:invocation:call-2",
        request_hash=request_hash,
    )
    pending_retry = await broker.begin(
        action="Bash",
        target="deploy",
        effect=ToolEffect.PROCESS,
        idempotency_key="tool:invocation:call-2",
        request_hash=request_hash,
    )

    assert first.disposition is SideEffectDisposition.EXECUTE
    assert pending_retry.disposition is SideEffectDisposition.BLOCKED
    with pytest.raises(SideEffectRequestConflictError):
        await broker.begin(
            action="Bash",
            target="other",
            effect=ToolEffect.PROCESS,
            idempotency_key="tool:invocation:call-2",
            request_hash=hashlib.sha256(b"other").hexdigest(),
        )


@pytest.mark.asyncio
async def test_parallel_terminal_updates_are_idempotent(
    tmp_path: Path,
) -> None:
    broker, store = await _running_broker(tmp_path / "ledger.db")
    reservation = await broker.begin(
        action="Write",
        target="README.md",
        effect=ToolEffect.LOCAL_WRITE,
        idempotency_key="tool:invocation:call-parallel",
        request_hash=hashlib.sha256(b"parallel").hexdigest(),
    )

    results = await asyncio.gather(
        broker.finish(
            reservation.record.record_id,
            status=SideEffectStatus.SUCCEEDED,
        ),
        broker.finish(
            reservation.record.record_id,
            status=SideEffectStatus.SUCCEEDED,
        ),
    )

    assert {result.status for result in results} == {
        SideEffectStatus.SUCCEEDED,
    }
    events = await store.list_events(reservation.record.task_id)
    assert [event.event_type for event in events].count(
        "side_effect.succeeded",
    ) == 1


@pytest.mark.asyncio
async def test_orphan_recovery_uses_approval_checkpoint(
    tmp_path: Path,
) -> None:
    broker, store = await _running_broker(tmp_path / "ledger.db")
    service = broker.service
    actor = ActorRef(type=ActorType.USER, id="local-user")
    first_approval = await service.request_approval(
        broker.task_id,
        action="file.write",
        risk=RiskLevel.MEDIUM,
        requester=actor,
    )
    assert first_approval.checkpoint_id is not None
    await service.decide_approval(
        broker.task_id,
        first_approval.approval_id,
        decision=ApprovalDecisionValue.APPROVED,
        actor=actor,
        reason="Proceed",
    )
    side_effect = await broker.begin(
        action="Write",
        target="README.md",
        effect=ToolEffect.LOCAL_WRITE,
        idempotency_key="tool:lost-runtime:call-1",
        request_hash=hashlib.sha256(b"lost runtime").hexdigest(),
    )
    second_approval = await service.request_approval(
        broker.task_id,
        action="shell.execute",
        risk=RiskLevel.HIGH,
        requester=actor,
    )

    failed = await service.recover_orphaned_task(broker.task_id)

    assert failed.status.value == "failed"
    recovered_effect = await store.get_side_effect(
        side_effect.record.record_id,
    )
    assert recovered_effect is not None
    assert recovered_effect.status is SideEffectStatus.UNCERTAIN
    approval_record = await service.get_approval(
        second_approval.approval_id,
    )
    assert approval_record is not None
    assert approval_record[1] is not None
    assert approval_record[1].decision is ApprovalDecisionValue.CANCELLED
    with pytest.raises(SideEffectRecoveryRequiredError):
        await service.resume_task(broker.task_id)
    authorized = await service.authorize_side_effect_retry(
        broker.task_id,
        side_effect.record.record_id,
        actor=actor,
        reason="Verified that the write was not applied",
    )
    resumed_task, resumed_run = await service.resume_task(
        broker.task_id,
    )
    assert authorized.status is SideEffectStatus.FAILED
    assert authorized.recovery_actor == actor
    assert resumed_task.status.value == "running"
    assert resumed_run.attempt == 2
    assert resumed_run.checkpoint_id == second_approval.checkpoint_id


@pytest.mark.asyncio
async def test_v1_ledger_is_upgraded_with_side_effect_projection(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "ledger.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA user_version = 1")

    ledger = SQLiteExecutionLedger(database_path)
    await ledger.initialize()

    assert await ledger.schema_version() == 2
    with sqlite3.connect(database_path) as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'side_effect_records'",
        ).fetchone()
    assert table == ("side_effect_records",)
