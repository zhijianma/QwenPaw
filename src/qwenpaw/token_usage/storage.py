# -*- coding: utf-8 -*-
"""File I/O, migration and compaction for token usage data.

Handles:
- Loading the JSON file (with corrupt-file safety).
- Atomic writes (write to .tmp, then os.replace) to prevent torn reads.
- One-time migration of the old flat format (root keys = date strings)
  to the new three-section format: {"daily": {}, "sessions": {}, "agents": {}}.
- Daily rollup: entries older than DAILY_RETENTION_DAYS are aggregated into
  a monthly bucket and removed from "daily" to keep the file bounded.
- Session pruning: session entries not updated within SESSION_RETENTION_DAYS
  are removed.

Retention defaults (tuneable at module level):
  DAILY_RETENTION_DAYS   = 90   keep last 90 days at day granularity
  SESSION_RETENTION_DAYS = 30   keep sessions active within 30 days
"""

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiofiles

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tuneable retention windows
# ---------------------------------------------------------------------------
DAILY_RETENTION_DAYS: int = 90   # days to keep at daily granularity
SESSION_RETENTION_DAYS: int = 30  # days since last_updated before session pruned

# Sentinel key that is present in the new format but not in the old format.
_NEW_FORMAT_KEY = "daily"


def _is_old_format(data: dict) -> bool:
    """Return True if ``data`` is the legacy flat format.

    The old format stored date strings (YYYY-MM-DD) directly at root level.
    The new format has a ``"daily"`` key at root level.
    """
    if _NEW_FORMAT_KEY in data:
        return False
    # If every key looks like a date (YYYY-MM-DD) or the dict is empty,
    # treat it as old format.
    return all(_looks_like_date(k) for k in data) if data else True


def _looks_like_date(s: str) -> bool:
    """Heuristic: YYYY-MM-DD has exactly 10 chars with dashes at pos 4 and 7."""
    return (
        len(s) == 10
        and s[4] == "-"
        and s[7] == "-"
        and s[:4].isdigit()
        and s[5:7].isdigit()
        and s[8:].isdigit()
    )


def migrate_if_needed(data: dict) -> tuple[dict, bool]:
    """Migrate old-format data to new format if necessary.

    Args:
        data: Raw dict loaded from the JSON file.

    Returns:
        A ``(migrated_data, needs_save)`` tuple.
        ``needs_save`` is True when the data was migrated and should be
        written back to disk.
    """
    if not _is_old_format(data):
        # Already new format; ensure all three top-level sections exist.
        changed = False
        for section in ("daily", "sessions", "agents"):
            if section not in data:
                data[section] = {}
                changed = True
        return data, changed

    # Wrap all existing date-keyed entries under "daily".
    migrated: dict = {
        "daily": dict(data),  # shallow copy – values are already dicts
        "sessions": {},
        "agents": {},
    }
    logger.info(
        "token_usage: migrated %d date entries from legacy flat format",
        len(data),
    )
    return migrated, True


async def load_data(path: Path) -> dict:
    """Load token usage data from *path*.

    Returns an empty new-format dict if the file does not exist or is
    unreadable / corrupt.
    """
    if not path.exists():
        return {"daily": {}, "sessions": {}, "agents": {}}

    try:
        async with aiofiles.open(path, mode="r", encoding="utf-8") as f:
            raw = await f.read()
        data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "token_usage: failed to read %s: %s — starting with empty data",
            path,
            exc,
        )
        return {"daily": {}, "sessions": {}, "agents": {}}

    data, _ = migrate_if_needed(data)
    return data


def save_data_sync(path: Path, data: dict) -> None:
    """Persist *data* to *path* using an atomic write (tmp → replace).

    This is intentionally synchronous so it can be called from the buffer
    flush task without blocking the event loop via ``asyncio.to_thread``.
    """
    tmp_path = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        with open(tmp_path, mode="w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp_path, path)
    except OSError as exc:
        logger.warning(
            "token_usage: failed to write %s: %s",
            path,
            exc,
        )
        # Clean up orphaned tmp file if it was created.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Compaction helpers
# ---------------------------------------------------------------------------

def _merge_into_monthly(
    monthly: dict,
    month_key: str,
    composite_key: str,
    entry: dict,
) -> None:
    """Accumulate *entry* into ``monthly[month_key][composite_key]``."""
    month_bucket = monthly.setdefault(month_key, {})
    agg = month_bucket.setdefault(
        composite_key,
        {
            "provider_id": entry.get("provider_id", ""),
            "model_name": entry.get("model_name", composite_key),
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "call_count": 0,
        },
    )
    agg["prompt_tokens"] += entry.get("prompt_tokens", 0)
    agg["completion_tokens"] += entry.get("completion_tokens", 0)
    agg["call_count"] += entry.get("call_count", 0)


def rollup_daily(
    data: dict,
    retention_days: int = DAILY_RETENTION_DAYS,
    reference_date: Optional[date] = None,
) -> bool:
    """Roll up daily entries older than *retention_days* into monthly buckets.

    Old entries are merged into ``data["monthly"]`` (created if absent) and
    removed from ``data["daily"]``.

    Args:
        data: The full in-memory data dict (mutated in-place).
        retention_days: Days to keep at daily granularity.  Default: 90.
        reference_date: Cutoff reference.  Default: today (local).

    Returns:
        True if any entries were rolled up (data was modified).
    """
    if reference_date is None:
        reference_date = date.today()
    cutoff = reference_date - timedelta(days=retention_days)

    daily: dict = data.get("daily", {})
    monthly: dict = data.setdefault("monthly", {})

    to_remove: list[str] = []
    for date_str, by_key in daily.items():
        try:
            d = date.fromisoformat(date_str)
        except ValueError:
            continue
        if d >= cutoff:
            continue  # within retention window — keep as daily
        # Aggregate each model entry into the monthly bucket.
        month_key = date_str[:7]  # "YYYY-MM"
        for composite_key, entry in by_key.items():
            _merge_into_monthly(monthly, month_key, composite_key, entry)
        to_remove.append(date_str)

    for date_str in to_remove:
        del daily[date_str]

    if to_remove:
        logger.info(
            "token_usage: rolled up %d daily entries into monthly buckets",
            len(to_remove),
        )
    return bool(to_remove)


def prune_sessions(
    data: dict,
    retention_days: int = SESSION_RETENTION_DAYS,
    reference_date: Optional[date] = None,
) -> bool:
    """Remove stale session entries whose *last_updated* is too old.

    Args:
        data: The full in-memory data dict (mutated in-place).
        retention_days: Keep sessions updated within this many days.
        reference_date: Cutoff reference.  Default: today (UTC).

    Returns:
        True if any sessions were pruned.
    """
    if reference_date is None:
        reference_date = datetime.now(tz=timezone.utc).date()
    cutoff = reference_date - timedelta(days=retention_days)

    sessions: dict = data.get("sessions", {})
    to_remove: list[str] = []
    for sid, sess in sessions.items():
        last_updated = sess.get("last_updated")
        if last_updated is None:
            # No timestamp — prune conservatively.
            to_remove.append(sid)
            continue
        try:
            # ISO-8601 with or without timezone offset.
            ts = datetime.fromisoformat(last_updated)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            session_date = ts.date()
        except ValueError:
            to_remove.append(sid)
            continue
        if session_date < cutoff:
            to_remove.append(sid)

    for sid in to_remove:
        del sessions[sid]

    if to_remove:
        logger.info(
            "token_usage: pruned %d stale session entries", len(to_remove)
        )
    return bool(to_remove)


__all__ = [
    "load_data",
    "save_data_sync",
    "migrate_if_needed",
    "rollup_daily",
    "prune_sessions",
    "DAILY_RETENTION_DAYS",
    "SESSION_RETENTION_DAYS",
]
