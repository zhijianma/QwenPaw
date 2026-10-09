# -*- coding: utf-8 -*-
"""Rebuildable Lite token-usage projection over Model Call facts."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ..kernel import ModelCallAttempt, ModelCallRecord, ModelCallResult
from ..utils.io_utils import run_sync_io
from .models import TokenUsageRecord


class UsageProjectionConflictError(RuntimeError):
    """Raised when one attempt ID maps to different usage evidence."""


class UsageProjectionStatus(BaseModel):
    """Content-free reconciliation state for the derived index."""

    model_config = ConfigDict(frozen=True)

    cutover_date: date
    indexed_attempts: int
    last_rebuild_at: datetime | None = None
    last_rebuild_count: int = 0


class LiteUsageProjection:
    """SQLite projection that is disposable and attempt-idempotent."""

    def __init__(
        self,
        database_path: Path,
        *,
        initial_cutover_date: date | None = None,
    ) -> None:
        self._database_path = Path(database_path)
        self._initial_cutover_date = initial_cutover_date

    def _connect(self) -> sqlite3.Connection:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS usage_projection_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS model_usage_attempts (
                attempt_id TEXT PRIMARY KEY,
                completed_at TEXT NOT NULL,
                usage_date TEXT NOT NULL,
                agent_id TEXT,
                conversation_id TEXT,
                turn_id TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                context_window_tokens INTEGER,
                compaction_threshold REAL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                cache_read_tokens INTEGER NOT NULL,
                cache_write_tokens INTEGER NOT NULL,
                cache_eligible_input_tokens INTEGER NOT NULL,
                cache_observed INTEGER NOT NULL,
                usage_observed INTEGER NOT NULL DEFAULT 1,
                cost_micros INTEGER,
                cost_unknown INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_model_usage_query
            ON model_usage_attempts (
                usage_date,
                agent_id,
                conversation_id,
                turn_id,
                provider_id,
                model_id
            );
            """,
        )
        self._ensure_column(
            connection,
            "context_window_tokens",
            "INTEGER",
        )
        self._ensure_column(
            connection,
            "compaction_threshold",
            "REAL",
        )
        self._ensure_column(
            connection,
            "usage_observed",
            "INTEGER NOT NULL DEFAULT 1",
        )
        self._ensure_cutover(connection)
        return connection

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        column: str,
        declaration: str,
    ) -> None:
        """Add one nullable projection column for an existing Lite index."""
        columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(model_usage_attempts)",
            ).fetchall()
        }
        if column not in columns:
            connection.execute(
                f"ALTER TABLE model_usage_attempts "
                f"ADD COLUMN {column} {declaration}",
            )

    def _ensure_cutover(self, connection: sqlite3.Connection) -> date:
        row = connection.execute(
            "SELECT value FROM usage_projection_meta WHERE key = ?",
            ("cutover_date",),
        ).fetchone()
        if row is not None:
            return date.fromisoformat(str(row["value"]))
        cutover = self._initial_cutover_date or (
            datetime.now(timezone.utc).date() + timedelta(days=1)
        )
        connection.execute(
            "INSERT INTO usage_projection_meta (key, value) VALUES (?, ?)",
            ("cutover_date", cutover.isoformat()),
        )
        connection.commit()
        return cutover

    @staticmethod
    def _values(
        attempt: ModelCallAttempt,
        result: ModelCallResult,
    ) -> tuple[object, ...]:
        usage_observed = result.usage_measurement == "provider_reported"
        completed_at = result.completed_at.astimezone(timezone.utc)
        return (
            str(attempt.attempt_id),
            completed_at.isoformat(),
            completed_at.date().isoformat(),
            attempt.agent_id,
            attempt.conversation_id,
            str(attempt.invocation_id),
            attempt.provider_id,
            attempt.model_id,
            attempt.context_window_tokens,
            attempt.compaction_threshold,
            result.input_tokens or 0,
            result.output_tokens or 0,
            result.cache_read_tokens,
            result.cache_write_tokens,
            result.cache_eligible_input_tokens,
            int(result.cache_observed),
            int(usage_observed),
            result.cost_micros,
            int(result.cost_unknown),
        )

    def _record_sync(
        self,
        attempt: ModelCallAttempt,
        result: ModelCallResult,
    ) -> date:
        values = self._values(attempt, result)
        with self._connect() as connection:
            cutover = self._ensure_cutover(connection)
            connection.execute(
                """
                INSERT OR IGNORE INTO model_usage_attempts (
                    attempt_id, completed_at, usage_date, agent_id,
                    conversation_id, turn_id, provider_id, model_id,
                    context_window_tokens, compaction_threshold,
                    input_tokens, output_tokens, cache_read_tokens,
                    cache_write_tokens, cache_eligible_input_tokens,
                    cache_observed, usage_observed, cost_micros, cost_unknown
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                values,
            )
            row = connection.execute(
                """
                SELECT attempt_id, completed_at, usage_date, agent_id,
                       conversation_id, turn_id, provider_id, model_id,
                       context_window_tokens, compaction_threshold,
                       input_tokens, output_tokens, cache_read_tokens,
                       cache_write_tokens, cache_eligible_input_tokens,
                       cache_observed, usage_observed, cost_micros,
                       cost_unknown
                FROM model_usage_attempts WHERE attempt_id = ?
                """,
                (str(attempt.attempt_id),),
            ).fetchone()
            if row is None or tuple(row) != values:
                raise UsageProjectionConflictError(
                    "model usage attempt already has different evidence",
                )
            return cutover

    async def record(
        self,
        attempt: ModelCallAttempt,
        result: ModelCallResult,
    ) -> date:
        """Project one immutable Model Call result idempotently."""
        return await run_sync_io(self._record_sync, attempt, result)

    @staticmethod
    def _matches(
        row: sqlite3.Row,
        *,
        model_name: str | None,
        provider_id: str | None,
        agent_id: str | None,
        conversation_id: str | None,
        turn_id: str | None,
    ) -> bool:
        return all(
            (
                model_name is None or row["model_id"] == model_name,
                provider_id is None or row["provider_id"] == provider_id,
                agent_id is None or row["agent_id"] == agent_id,
                conversation_id is None
                or row["conversation_id"] == conversation_id,
                turn_id is None or row["turn_id"] == turn_id,
            ),
        )

    def _query_sync(
        self,
        start_date: date,
        end_date: date,
        model_name: str | None,
        provider_id: str | None,
        agent_id: str | None,
        conversation_id: str | None,
        turn_id: str | None,
        include_shadow: bool,
    ) -> tuple[date, list[TokenUsageRecord]]:
        with self._connect() as connection:
            cutover = self._ensure_cutover(connection)
            rows = connection.execute(
                """
                SELECT * FROM model_usage_attempts
                WHERE usage_date >= ? AND usage_date <= ?
                ORDER BY usage_date, attempt_id
                """,
                (
                    (
                        start_date
                        if include_shadow
                        else max(start_date, cutover)
                    ).isoformat(),
                    end_date.isoformat(),
                ),
            ).fetchall()
        grouped: dict[tuple[object, ...], TokenUsageRecord] = {}
        for row in rows:
            if not self._matches(
                row,
                model_name=model_name,
                provider_id=provider_id,
                agent_id=agent_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
            ):
                continue
            key = (
                row["usage_date"],
                row["agent_id"],
                row["conversation_id"],
                row["turn_id"],
                row["provider_id"],
                row["model_id"],
            )
            current = grouped.get(key)
            context_window = int(row["context_window_tokens"] or 0)
            usage_observed = bool(row["usage_observed"])
            observed_context_window = context_window if usage_observed else 0
            context_input = (
                (
                    int(row["cache_eligible_input_tokens"])
                    if bool(row["cache_observed"])
                    else int(row["input_tokens"])
                )
                if usage_observed and context_window > 0
                else 0
            )
            context_ratio = (
                context_input / context_window * 100
                if context_window > 0
                else None
            )
            current_context_input = (
                current.context_input_tokens if current else 0
            )
            current_context_window = (
                current.context_window_tokens if current else 0
            )
            total_context_input = current_context_input + context_input
            total_context_window = (
                current_context_window + observed_context_window
            )
            current_max = current.max_context_usage_ratio if current else None
            maxima = (current_max, context_ratio)
            max_context_ratio = (
                max(value for value in maxima if value is not None)
                if any(value is not None for value in maxima)
                else None
            )
            threshold = row["compaction_threshold"]
            near_compaction = int(
                context_ratio is not None
                and threshold is not None
                and context_ratio >= float(threshold) * 100,
            )
            grouped[key] = TokenUsageRecord(
                date=str(row["usage_date"]),
                provider_id=str(row["provider_id"]),
                model=str(row["model_id"]),
                prompt_tokens=(
                    (current.prompt_tokens if current else 0)
                    + int(row["input_tokens"])
                ),
                completion_tokens=(
                    (current.completion_tokens if current else 0)
                    + int(row["output_tokens"])
                ),
                cache_read_tokens=(
                    (current.cache_read_tokens if current else 0)
                    + int(row["cache_read_tokens"])
                ),
                cache_write_tokens=(
                    (current.cache_write_tokens if current else 0)
                    + int(row["cache_write_tokens"])
                ),
                cache_eligible_input_tokens=(
                    (current.cache_eligible_input_tokens if current else 0)
                    + int(row["cache_eligible_input_tokens"])
                ),
                cache_observed_calls=(
                    (current.cache_observed_calls if current else 0)
                    + int(row["cache_observed"])
                ),
                context_input_tokens=total_context_input,
                context_window_tokens=total_context_window,
                context_observed_calls=(
                    (current.context_observed_calls if current else 0)
                    + int(observed_context_window > 0)
                ),
                near_compaction_calls=(
                    (current.near_compaction_calls if current else 0)
                    + near_compaction
                ),
                cost_micros=(
                    (current.cost_micros if current else 0)
                    + int(row["cost_micros"] or 0)
                ),
                cost_unknown_calls=(
                    (current.cost_unknown_calls if current else 0)
                    + int(row["cost_unknown"])
                ),
                usage_observed_calls=(
                    (current.usage_observed_calls if current else 0)
                    + int(usage_observed)
                ),
                usage_unobserved_calls=(
                    (current.usage_unobserved_calls if current else 0)
                    + int(not usage_observed)
                ),
                context_usage_ratio=(
                    total_context_input / total_context_window * 100
                    if total_context_window > 0
                    else None
                ),
                max_context_usage_ratio=max_context_ratio,
                call_count=(current.call_count if current else 0) + 1,
                agent_id=row["agent_id"],
                chat_id=row["conversation_id"],
                turn_id=str(row["turn_id"]),
            )
        return cutover, list(grouped.values())

    async def query(
        self,
        start_date: date,
        end_date: date,
        *,
        model_name: str | None = None,
        provider_id: str | None = None,
        agent_id: str | None = None,
        conversation_id: str | None = None,
        turn_id: str | None = None,
        include_shadow: bool = False,
    ) -> tuple[date, list[TokenUsageRecord]]:
        """Return post-cutover rows and the immutable cutover date."""
        return await run_sync_io(
            self._query_sync,
            start_date,
            end_date,
            model_name,
            provider_id,
            agent_id,
            conversation_id,
            turn_id,
            include_shadow,
        )

    def _status_sync(self) -> UsageProjectionStatus:
        with self._connect() as connection:
            cutover = self._ensure_cutover(connection)
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM model_usage_attempts",
                ).fetchone()[0],
            )
            rebuilt = connection.execute(
                "SELECT value FROM usage_projection_meta WHERE key = ?",
                ("last_rebuild_at",),
            ).fetchone()
            rebuilt_count = connection.execute(
                "SELECT value FROM usage_projection_meta WHERE key = ?",
                ("last_rebuild_count",),
            ).fetchone()
        return UsageProjectionStatus(
            cutover_date=cutover,
            indexed_attempts=count,
            last_rebuild_at=(
                datetime.fromisoformat(str(rebuilt["value"]))
                if rebuilt is not None
                else None
            ),
            last_rebuild_count=(
                int(rebuilt_count["value"]) if rebuilt_count is not None else 0
            ),
        )

    async def status(self) -> UsageProjectionStatus:
        """Return content-free index and reconciliation state."""
        return await run_sync_io(self._status_sync)

    def _rebuild_sync(self, records: Sequence[ModelCallRecord]) -> int:
        values = [
            projected
            for record in records
            if record.result is not None
            for projected in [self._values(record.attempt, record.result)]
        ]
        rebuilt_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute("DELETE FROM model_usage_attempts")
            connection.executemany(
                """
                INSERT INTO model_usage_attempts (
                    attempt_id, completed_at, usage_date, agent_id,
                    conversation_id, turn_id, provider_id, model_id,
                    context_window_tokens, compaction_threshold,
                    input_tokens, output_tokens, cache_read_tokens,
                    cache_write_tokens, cache_eligible_input_tokens,
                    cache_observed, usage_observed, cost_micros, cost_unknown
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                values,
            )
            connection.executemany(
                """
                INSERT INTO usage_projection_meta (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (
                    ("last_rebuild_at", rebuilt_at),
                    ("last_rebuild_count", str(len(values))),
                ),
            )
        return len(values)

    async def rebuild(self, records: Sequence[ModelCallRecord]) -> int:
        """Replace the disposable index from authoritative records."""
        return await run_sync_io(self._rebuild_sync, records)


__all__ = [
    "LiteUsageProjection",
    "UsageProjectionConflictError",
    "UsageProjectionStatus",
]
