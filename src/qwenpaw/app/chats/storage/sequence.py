# -*- coding: utf-8 -*-
"""Monotonic workspace recall sequence allocation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .catalog import initialize_catalog_database
from .common import connect_database, immediate_transaction


@dataclass(frozen=True)
class RecallSequenceLease:
    """One non-reusable contiguous recall sequence range."""

    start: int
    end: int

    def values(self) -> range:
        """Return every allocated sequence in ascending order."""
        return range(self.start, self.end + 1)


def reserve_recall_sequences(
    catalog_path: Path,
    *,
    workspace_id: str,
    count: int,
    high_water_floor: int = 0,
    lease_owner: str,
    now_us: int,
    lease_duration_us: int = 60_000_000,
) -> RecallSequenceLease | None:
    """Advance the high-water mark and reserve a range without reuse."""
    if count < 0:
        raise ValueError("count must not be negative")
    if high_water_floor < 0:
        raise ValueError("high_water_floor must not be negative")
    initialize_catalog_database(catalog_path)
    with connect_database(catalog_path) as connection:
        with immediate_transaction(connection):
            row = connection.execute(
                "SELECT high_water FROM chat_recall_sequences "
                "WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            previous = int(row["high_water"]) if row is not None else 0
            base = max(previous, high_water_floor)
            end = base + count
            connection.execute(
                "INSERT INTO chat_recall_sequences("
                "workspace_id, high_water, lease_owner, lease_end, "
                "lease_expires_at_us, updated_at_us) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(workspace_id) DO UPDATE SET "
                "high_water = excluded.high_water, "
                "lease_owner = excluded.lease_owner, "
                "lease_end = excluded.lease_end, "
                "lease_expires_at_us = excluded.lease_expires_at_us, "
                "updated_at_us = excluded.updated_at_us",
                (
                    workspace_id,
                    end,
                    lease_owner if count else None,
                    end if count else None,
                    now_us + lease_duration_us if count else None,
                    now_us,
                ),
            )
    if count == 0:
        return None
    return RecallSequenceLease(start=base + 1, end=end)
