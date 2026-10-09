# -*- coding: utf-8 -*-
"""Tests for the rebuildable Model Call usage projection."""

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
        cache_eligible_input_tokens=input_tokens,
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
