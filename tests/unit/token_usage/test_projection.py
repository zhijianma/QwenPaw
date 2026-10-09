# -*- coding: utf-8 -*-
"""Tests for the rebuildable Model Call usage projection."""

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from qwenpaw.kernel import (
    ModelCallAttempt,
    ModelCallRecord,
    ModelCallResult,
    ModelCallStatus,
    ModelRouteReason,
    RouteDecision,
)
from qwenpaw.token_usage.manager import TokenUsageManager
from qwenpaw.token_usage.projection import (
    LiteUsageProjection,
    UsageProjectionConflictError,
)


def _record(
    *,
    completed_at: datetime,
    input_tokens: int = 100,
    output_tokens: int = 20,
    context_window_tokens: int | None = 200,
    compaction_threshold: float | None = 0.8,
    cache_eligible_input_tokens: int | None = None,
) -> ModelCallRecord:
    attempt_id = uuid4()
    invocation_id = uuid4()
    route_id = uuid4()
    manifest_id = uuid4()
    route = RouteDecision(
        route_decision_id=route_id,
        attempt_id=attempt_id,
        invocation_id=invocation_id,
        correlation_id=invocation_id,
        conversation_id="chat-1",
        registry_generation=1,
        context_manifest_id=manifest_id,
        model_call_index=1,
        attempt_index=1,
        provider_id="provider-a",
        model_id="model-a",
        reason=ModelRouteReason.PRIMARY,
    )
    attempt = ModelCallAttempt(
        attempt_id=attempt_id,
        route_decision_id=route_id,
        invocation_id=invocation_id,
        correlation_id=invocation_id,
        agent_id="agent-a",
        conversation_id="chat-1",
        registry_generation=1,
        context_manifest_id=manifest_id,
        model_call_index=1,
        attempt_index=1,
        provider_id="provider-a",
        model_id="model-a",
        context_window_tokens=context_window_tokens,
        compaction_threshold=compaction_threshold,
    )
    result = ModelCallResult(
        attempt_id=attempt_id,
        invocation_id=invocation_id,
        conversation_id="chat-1",
        status=ModelCallStatus.SUCCEEDED,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        usage_measurement="provider_reported",
        cache_read_tokens=60,
        cache_eligible_input_tokens=(
            cache_eligible_input_tokens
            if cache_eligible_input_tokens is not None
            else input_tokens
        ),
        cache_observed=True,
        completed_at=completed_at,
    )
    return ModelCallRecord(route=route, attempt=attempt, result=result)


@pytest.mark.asyncio
async def test_projection_is_attempt_idempotent(tmp_path: Path) -> None:
    cutover = date(2026, 10, 9)
    projection = LiteUsageProjection(
        tmp_path / "usage.sqlite3",
        initial_cutover_date=cutover,
    )
    record = _record(
        completed_at=datetime(2026, 10, 9, 1, tzinfo=timezone.utc),
    )
    assert record.result is not None

    assert await projection.record(record.attempt, record.result) == cutover
    assert await projection.record(record.attempt, record.result) == cutover
    status = await projection.status()

    assert status.indexed_attempts == 1


@pytest.mark.asyncio
async def test_projection_rejects_attempt_conflict(tmp_path: Path) -> None:
    projection = LiteUsageProjection(
        tmp_path / "usage.sqlite3",
        initial_cutover_date=date(2026, 10, 9),
    )
    record = _record(
        completed_at=datetime(2026, 10, 9, 1, tzinfo=timezone.utc),
    )
    assert record.result is not None
    await projection.record(record.attempt, record.result)
    changed = record.result.model_copy(update={"input_tokens": 101})

    with pytest.raises(
        UsageProjectionConflictError,
        match="different evidence",
    ):
        await projection.record(record.attempt, changed)


@pytest.mark.asyncio
async def test_projection_queries_only_at_or_after_cutover(
    tmp_path: Path,
) -> None:
    projection = LiteUsageProjection(
        tmp_path / "usage.sqlite3",
        initial_cutover_date=date(2026, 10, 9),
    )
    before = _record(
        completed_at=datetime(2026, 10, 8, 23, tzinfo=timezone.utc),
    )
    after = _record(
        completed_at=datetime(2026, 10, 9, 1, tzinfo=timezone.utc),
    )
    assert before.result is not None
    assert after.result is not None
    await projection.record(before.attempt, before.result)
    await projection.record(after.attempt, after.result)

    cutover, rows = await projection.query(
        date(2026, 10, 8),
        date(2026, 10, 9),
    )

    assert cutover == date(2026, 10, 9)
    assert len(rows) == 1
    assert rows[0].date == "2026-10-09"
    assert rows[0].prompt_tokens == 100
    assert rows[0].context_input_tokens == 100
    assert rows[0].context_window_tokens == 200
    assert rows[0].context_observed_calls == 1
    assert rows[0].context_usage_ratio == 50
    assert rows[0].max_context_usage_ratio == 50
    assert rows[0].near_compaction_calls == 0


@pytest.mark.asyncio
async def test_context_usage_uses_cache_eligible_input_and_threshold(
    tmp_path: Path,
) -> None:
    projection = LiteUsageProjection(
        tmp_path / "usage.sqlite3",
        initial_cutover_date=date(2026, 10, 9),
    )
    record = _record(
        completed_at=datetime(2026, 10, 9, 1, tzinfo=timezone.utc),
        input_tokens=100,
        cache_eligible_input_tokens=180,
        context_window_tokens=200,
        compaction_threshold=0.8,
    )
    assert record.result is not None
    await projection.record(record.attempt, record.result)

    _, rows = await projection.query(
        date(2026, 10, 9),
        date(2026, 10, 9),
    )

    assert rows[0].context_input_tokens == 180
    assert rows[0].context_usage_ratio == 90
    assert rows[0].max_context_usage_ratio == 90
    assert rows[0].near_compaction_calls == 1


@pytest.mark.asyncio
async def test_existing_projection_schema_adds_context_columns(
    tmp_path: Path,
) -> None:
    database = tmp_path / "usage.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE model_usage_attempts (
                attempt_id TEXT PRIMARY KEY,
                completed_at TEXT NOT NULL,
                usage_date TEXT NOT NULL,
                agent_id TEXT,
                conversation_id TEXT,
                turn_id TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                cache_read_tokens INTEGER NOT NULL,
                cache_write_tokens INTEGER NOT NULL,
                cache_eligible_input_tokens INTEGER NOT NULL,
                cache_observed INTEGER NOT NULL,
                cost_micros INTEGER,
                cost_unknown INTEGER NOT NULL
            )
            """,
        )
    projection = LiteUsageProjection(
        database,
        initial_cutover_date=date(2026, 10, 9),
    )

    await projection.status()

    with sqlite3.connect(database) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(model_usage_attempts)",
            ).fetchall()
        }
    assert "context_window_tokens" in columns
    assert "compaction_threshold" in columns


@pytest.mark.asyncio
async def test_projection_rebuild_replaces_disposable_index(
    tmp_path: Path,
) -> None:
    projection = LiteUsageProjection(
        tmp_path / "usage.sqlite3",
        initial_cutover_date=date(2026, 10, 9),
    )
    old = _record(
        completed_at=datetime(2026, 10, 9, 1, tzinfo=timezone.utc),
    )
    replacement = _record(
        completed_at=datetime(2026, 10, 10, 1, tzinfo=timezone.utc),
        input_tokens=300,
    )
    assert old.result is not None
    await projection.record(old.attempt, old.result)

    assert await projection.rebuild([replacement]) == 1
    status = await projection.status()
    _, rows = await projection.query(
        date(2026, 10, 9),
        date(2026, 10, 10),
    )

    assert status.indexed_attempts == 1
    assert status.last_rebuild_count == 1
    assert status.last_rebuild_at is not None
    assert len(rows) == 1
    assert rows[0].prompt_tokens == 300


@pytest.mark.asyncio
async def test_manager_merges_cutover_without_double_counting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "qwenpaw.token_usage.manager.WORKING_DIR",
        tmp_path,
    )
    monkeypatch.setattr(
        "qwenpaw.token_usage.manager.TOKEN_USAGE_FILE",
        "usage.json",
    )
    manager = TokenUsageManager(
        projection_path=tmp_path / "projection.sqlite3",
        projection_cutover_date=date(2026, 10, 9),
    )
    record = _record(
        completed_at=datetime(2026, 10, 9, 1, tzinfo=timezone.utc),
    )
    assert record.result is not None
    assert not await manager.project_model_call(
        record.attempt,
        record.result,
    )
    await manager.record(
        "provider-a",
        "model-a",
        100,
        20,
        date(2026, 10, 9),
        agent_id="agent-a",
        conversation_id="chat-1",
        turn_id=str(record.attempt.invocation_id),
    )
    await manager.record(
        "manual",
        "unscoped",
        7,
        3,
        date(2026, 10, 9),
    )

    summary = await manager.get_summary(
        date(2026, 10, 9),
        date(2026, 10, 9),
    )

    assert summary.total_prompt_tokens == 107
    assert summary.total_completion_tokens == 23
    assert summary.total_calls == 2
    assert summary.total_context_input_tokens == 100
    assert summary.total_context_window_tokens == 200
    assert summary.context_observed_calls == 1
    assert summary.context_usage_ratio == 50
    assert summary.max_context_usage_ratio == 50


@pytest.mark.asyncio
async def test_pre_cutover_shadow_adds_context_without_duplicate_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "qwenpaw.token_usage.manager.WORKING_DIR",
        tmp_path,
    )
    manager = TokenUsageManager(
        projection_path=tmp_path / "projection.sqlite3",
        projection_cutover_date=date(2026, 10, 10),
    )
    record = _record(
        completed_at=datetime(2026, 10, 9, 1, tzinfo=timezone.utc),
        cache_eligible_input_tokens=180,
    )
    assert record.result is not None
    assert await manager.project_model_call(record.attempt, record.result)
    await manager.record(
        "provider-a",
        "model-a",
        100,
        20,
        date(2026, 10, 9),
        cache_read_tokens=60,
        cache_eligible_input_tokens=180,
        cache_observed=True,
        agent_id="agent-a",
        conversation_id="chat-1",
        turn_id=str(record.attempt.invocation_id),
    )

    summary = await manager.get_summary(
        date(2026, 10, 9),
        date(2026, 10, 9),
    )

    assert summary.total_prompt_tokens == 100
    assert summary.total_calls == 1
    assert summary.total_context_input_tokens == 180
    assert summary.context_usage_ratio == 90
    assert summary.near_compaction_calls == 1


@pytest.mark.asyncio
async def test_projection_failure_does_not_create_undeduplicated_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "qwenpaw.token_usage.manager.WORKING_DIR",
        tmp_path,
    )
    manager = TokenUsageManager(
        projection_path=tmp_path / "projection.sqlite3",
    )
    manager._projection.record = AsyncMock(  # pylint: disable=protected-access
        side_effect=OSError("disk unavailable"),
    )
    record = _record(
        completed_at=datetime(2026, 10, 9, 1, tzinfo=timezone.utc),
    )
    assert record.result is not None

    record_legacy = await manager.project_model_call(
        record.attempt,
        record.result,
    )

    assert record_legacy is False
