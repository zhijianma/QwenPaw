# -*- coding: utf-8 -*-
"""Replay legacy JSON Inbox rows through Operational Delivery."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from ..inbox import SQLiteInboxProjectionStore
from ..kernel import (
    OperationalEvent,
    OperationalSeverity,
    OperationalStatus,
)
from ..kernel.models import utc_now
from ..portability.compatibility_safety import redact_sensitive_text
from ..utils.logging import sanitize_log_value
from .inbox_store import query_events
from .legacy_inbox_observation import SQLiteLegacyInboxObservationStore
from .operational_delivery import (
    operational_delivery_service_for_workspace,
)

logger = logging.getLogger(__name__)

_FAILURE_STATUSES = {
    "cancelled",
    "error",
    "fail",
    "failed",
    "timeout",
}
_EVENT_TYPE_CHARACTERS = re.compile(r"[^a-z0-9_.-]+")


@dataclass(frozen=True, slots=True)
class LegacyInboxMigrationReport:
    """Summarize one non-destructive legacy Inbox replay."""

    attempted: int
    migrated: int
    failed: int


class LegacyInboxMigration:  # pylint: disable=too-few-public-methods
    """One-shot Workspace service for replay-safe Inbox migration."""

    def __init__(self, workspace: Any) -> None:
        self._workspace = workspace
        self.report: LegacyInboxMigrationReport | None = None

    async def start(self) -> None:
        """Read this Agent's legacy rows without modifying the JSON file."""
        events, _, _ = await query_events(
            limit=5000,
            offset=0,
            agent_id=self._workspace.agent_id,
        )
        self.report = await migrate_legacy_inbox_events(
            self._workspace,
            events,
        )


async def migrate_legacy_inbox_events(
    workspace: Any,
    events: Iterable[dict[str, Any]],
) -> LegacyInboxMigrationReport:
    """Project valid legacy rows while leaving failed rows visible."""
    rows = list(events)
    scan_started_at = utc_now()
    inbox_database = workspace.workspace_dir / ".qwenpaw" / "lite" / "inbox.db"
    projection = SQLiteInboxProjectionStore(inbox_database)
    observation_store = SQLiteLegacyInboxObservationStore(inbox_database)
    source_fingerprint = _source_fingerprint(rows)
    try:
        service = await operational_delivery_service_for_workspace(workspace)
    except Exception as exc:
        scan_error = _failure_reason(exc)
        try:
            await observation_store.record_scan(
                agent_id=workspace.agent_id,
                started_at=scan_started_at,
                completed_at=utc_now(),
                attempted=len(rows),
                migrated=0,
                failed=len(rows),
                source_fingerprint=source_fingerprint,
                scan_error=scan_error,
            )
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception(
                "Failed to persist legacy Inbox migration observation",
            )
        raise
    attempted = 0
    migrated = 0
    failed = 0
    last_failure_reason = None
    for row in rows:
        attempted += 1
        try:
            event = _operational_event(workspace, row)
            result = await service.publish(event)
            if bool(row.get("read")) and not result.inbox_item.read:
                await projection.mark_read(
                    result.inbox_item.item_id,
                    agent_id=workspace.agent_id,
                    expected_revision=result.inbox_item.revision,
                )
            migrated += 1
        except Exception as exc:  # pylint: disable=broad-exception-caught
            failed += 1
            last_failure_reason = _failure_reason(exc)
            logger.warning(
                "Failed to migrate legacy Inbox event %s for agent %s",
                sanitize_log_value(row.get("id", "<missing>")),
                sanitize_log_value(workspace.agent_id),
                exc_info=True,
            )
    report = LegacyInboxMigrationReport(
        attempted=attempted,
        migrated=migrated,
        failed=failed,
    )
    await observation_store.record_scan(
        agent_id=workspace.agent_id,
        started_at=scan_started_at,
        completed_at=utc_now(),
        attempted=report.attempted,
        migrated=report.migrated,
        failed=report.failed,
        source_fingerprint=source_fingerprint,
        scan_error=last_failure_reason,
    )
    return report


def _source_fingerprint(rows: list[dict[str, Any]]) -> str:
    """Hash complete source rows without persisting their content."""
    row_digests = []
    for row in rows:
        encoded = json.dumps(
            row,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        row_digests.append(hashlib.sha256(encoded).digest())
    digest = hashlib.sha256()
    for row_digest in sorted(row_digests):
        digest.update(row_digest)
    return digest.hexdigest()


def _failure_reason(exc: Exception) -> str:
    """Return a bounded credential-redacted failure description."""
    return (
        f"{type(exc).__name__}: " f"{redact_sensitive_text(exc, limit=400)}"
    )[:500]


def _operational_event(
    workspace: Any,
    row: dict[str, Any],
) -> OperationalEvent:
    row_agent_id = _required_text(
        row.get("agent_id") or workspace.agent_id,
        "agent id",
        500,
    )
    if row_agent_id != workspace.agent_id:
        raise ValueError("legacy event belongs to another agent")
    legacy_id = _required_text(row.get("id"), "legacy event id", 500)
    source_type = _required_text(
        row.get("source_type") or "legacy",
        "source type",
        100,
    )
    source_id = _optional_text(row.get("source_id"), 500)
    raw_event_type = _required_text(
        row.get("event_type") or "legacy_event",
        "event type",
        500,
    )
    source_status = _required_text(
        row.get("status") or "success",
        "source status",
        100,
    )
    title = _required_text(
        row.get("title") or "Legacy Inbox event",
        "title",
        500,
    )
    body = _optional_text(row.get("body"), 20_000)
    payload = row.get("payload")
    source_payload = dict(payload) if isinstance(payload, dict) else {}
    source_payload.update(
        {
            "legacy_event_id": legacy_id,
            "legacy_created_at": row.get("created_at"),
            "legacy_event_type": raw_event_type,
        },
    )
    digest = hashlib.sha256(legacy_id.encode("utf-8")).hexdigest()
    return OperationalEvent(
        agent_id=workspace.agent_id,
        producer_id="qwenpaw.legacy.inbox",
        event_type=_event_type(raw_event_type),
        idempotency_key=f"legacy:{digest}",
        registry_generation=workspace.capability_registry.generation,
        source_type=source_type,
        source_id=source_id,
        status=(
            OperationalStatus.ERROR
            if source_status.lower() in _FAILURE_STATUSES
            else OperationalStatus.SUCCESS
        ),
        source_status=source_status,
        severity=_severity(row.get("severity")),
        title=title,
        body=body,
        payload=source_payload,
        occurred_at=_occurred_at(row.get("created_at")),
    )


def _event_type(value: str) -> str:
    normalized = _EVENT_TYPE_CHARACTERS.sub("-", value.strip().lower())
    normalized = normalized.strip("._-")
    return normalized[:200] or "legacy_event"


def _severity(value: Any) -> OperationalSeverity:
    try:
        return OperationalSeverity(str(value or "info").lower())
    except ValueError:
        return OperationalSeverity.INFO


def _occurred_at(value: Any) -> datetime:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is not None:
                return parsed
        except ValueError:
            pass
    return datetime.fromtimestamp(0, tz=timezone.utc)


def _required_text(value: Any, label: str, limit: int) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        raise ValueError(f"{label} is required")
    return text[:limit]


def _optional_text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    return str(value)[:limit]


__all__ = [
    "LegacyInboxMigration",
    "LegacyInboxMigrationReport",
    "migrate_legacy_inbox_events",
]
