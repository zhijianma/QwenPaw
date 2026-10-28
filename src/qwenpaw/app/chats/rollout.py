# -*- coding: utf-8 -*-
"""Lightweight rollout health derived from normalized Chat control data."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .storage import ChatStoragePaths
from .storage.common import connect_database


@dataclass(frozen=True)
class ChatRolloutStatus:
    """One read-only snapshot suitable for rollout gates and dashboards."""

    lifecycle: dict[str, int]
    migrations: dict[str, int]
    projection_repairs: dict[str, int]
    deletion_jobs: dict[str, int]
    runtime_intents: dict[str, int]
    recall_projection_states: dict[str, int]
    recall_projection_lag: int
    stop_reasons: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def healthy(self) -> bool:
        """Return whether no hard rollout stop condition is present."""
        return not self.stop_reasons

    def as_dict(self) -> dict[str, object]:
        """Return a stable JSON-compatible payload."""
        return {**asdict(self), "healthy": self.healthy}


def _counts(connection, table: str, column: str) -> dict[str, int]:
    rows = connection.execute(
        f"SELECT {column}, COUNT(*) AS total FROM {table} "
        f"GROUP BY {column}",
    ).fetchall()
    return {str(row[column]): int(row["total"]) for row in rows}


def read_chat_rollout_status(paths: ChatStoragePaths) -> ChatRolloutStatus:
    """Read control-plane counters without parsing any transcript body."""
    with connect_database(paths.catalog, readonly=True) as catalog:
        lifecycle = _counts(catalog, "chat_catalog_entries", "lifecycle")
        migrations = _counts(catalog, "chat_migrations", "state")
        repairs = _counts(catalog, "chat_catalog_repairs", "state")
        deletions = _counts(catalog, "chat_deletion_jobs", "state")
        intents = _counts(catalog, "chat_runtime_intents", "state")
        catalog_generations = {
            str(row["conversation_id"]): int(row["source_generation"])
            for row in catalog.execute(
                "SELECT conversation_id, source_generation "
                "FROM chat_catalog_entries WHERE lifecycle = 'ready'",
            )
        }
    recall_generations: dict[str, tuple[int, str]] = {}
    recall_states: dict[str, int] = {}
    if paths.recall.is_file():
        with connect_database(paths.recall, readonly=True) as recall:
            recall_states = _counts(
                recall,
                "chat_recall_projection_state",
                "state",
            )
            recall_generations = {
                str(row["conversation_id"]): (
                    int(row["source_generation"]),
                    str(row["state"]),
                )
                for row in recall.execute(
                    "SELECT conversation_id, source_generation, state "
                    "FROM chat_recall_projection_state",
                )
            }
    recall_lag = sum(
        recall_generations.get(conversation_id) != (generation, "ready")
        for conversation_id, generation in catalog_generations.items()
    )
    stop_reasons: list[str] = []
    if lifecycle.get("quarantined", 0):
        stop_reasons.append("quarantined_chat")
    if migrations.get("failed_terminal", 0):
        stop_reasons.append("terminal_migration_failure")
    if repairs.get("running", 0):
        stop_reasons.append("projection_repair_running")
    if recall_states.get("failed", 0):
        stop_reasons.append("recall_projection_failed")
    warnings: list[str] = []
    if migrations.get("failed_retryable", 0):
        warnings.append("retryable_migration_failure")
    if repairs.get("pending", 0):
        warnings.append("projection_repair_pending")
    if deletions.get("pending", 0) or deletions.get("running", 0):
        warnings.append("deletion_in_progress")
    if intents.get("starting", 0):
        warnings.append("acceptance_reconciliation_pending")
    if recall_lag:
        warnings.append("recall_projection_lag")
    return ChatRolloutStatus(
        lifecycle=lifecycle,
        migrations=migrations,
        projection_repairs=repairs,
        deletion_jobs=deletions,
        runtime_intents=intents,
        recall_projection_states=recall_states,
        recall_projection_lag=recall_lag,
        stop_reasons=tuple(stop_reasons),
        warnings=tuple(warnings),
    )


__all__ = ["ChatRolloutStatus", "read_chat_rollout_status"]
