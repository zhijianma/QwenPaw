# -*- coding: utf-8 -*-
"""Process helper for durable Scheduler retry recovery tests."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from qwenpaw.kernel import (
    RetryPolicy,
    ScheduleDefinition,
    ScheduleTrigger,
    ScheduleTriggerCursor,
)
from qwenpaw.scheduling import (
    DurableScheduleTriggerWorker,
    ScheduleOccurrenceHandling,
    ScheduleTriggerDisposition,
    SQLiteSchedulerStore,
    schedule_definition_hash,
)

_AGENT_ID = "agent-process-recovery"
_SCHEDULE_ID = "reports.process-recovery"
_SCHEDULED_FOR = datetime(2026, 10, 9, tzinfo=timezone.utc)
_RETRY_DELAY = timedelta(seconds=30)


def _definition() -> ScheduleDefinition:
    return ScheduleDefinition(
        schedule_id=_SCHEDULE_ID,
        agent_id=_AGENT_ID,
        name="Process recovery report",
        objective="Verify durable trigger recovery",
        trigger=ScheduleTrigger(
            kind="interval",
            interval_seconds=60,
            start_at=_SCHEDULED_FOR,
        ),
        runner_id="qwenpaw.system.tasks.console-agent",
        retry_policy=RetryPolicy(
            backoff_seconds=_RETRY_DELAY.total_seconds(),
        ),
        misfire_grace_seconds=600,
    )


async def _leave_deferred_cursor(
    database_path: Path,
    ready_path: Path,
) -> None:
    store = SQLiteSchedulerStore(database_path)
    definition = _definition()
    await store.upsert(definition)
    await store.reconcile_cursor(
        ScheduleTriggerCursor(
            agent_id=definition.agent_id,
            schedule_id=definition.schedule_id,
            definition_hash=schedule_definition_hash(definition),
            next_fire_at=_SCHEDULED_FOR,
        ),
    )

    async def request_retry(
        _definition_value: ScheduleDefinition,
        _scheduled_for: datetime,
    ) -> ScheduleOccurrenceHandling:
        return ScheduleOccurrenceHandling.RETRY

    report = await DurableScheduleTriggerWorker(
        catalog=store,
        cursors=store,
    ).tick(
        agent_id=_AGENT_ID,
        now=_SCHEDULED_FOR,
        handle=request_retry,
    )
    outcome = report.outcomes[0]
    if outcome.disposition is not ScheduleTriggerDisposition.RETRY_PENDING:
        raise RuntimeError("retry cursor was not durably deferred")
    ready_path.write_text(
        json.dumps(
            {
                "retry_not_before": outcome.retry_not_before.isoformat(),
                "scheduled_for": outcome.scheduled_for.isoformat(),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    await asyncio.Event().wait()


async def _recover_deferred_cursor(database_path: Path) -> None:
    store = SQLiteSchedulerStore(database_path)
    retry_not_before = _SCHEDULED_FOR + _RETRY_DELAY

    async def fail_if_called(
        _definition_value: ScheduleDefinition,
        _scheduled_for: datetime,
    ) -> None:
        raise RuntimeError("handler ran before retry cooldown expired")

    early = await DurableScheduleTriggerWorker(
        catalog=store,
        cursors=store,
    ).tick(
        agent_id=_AGENT_ID,
        now=retry_not_before - timedelta(microseconds=1),
        handle=fail_if_called,
    )
    if early.outcomes:
        raise RuntimeError("retry cursor became due before its cooldown")

    handled: list[datetime] = []

    async def handle(
        _definition_value: ScheduleDefinition,
        scheduled_for: datetime,
    ) -> None:
        handled.append(scheduled_for)

    recovered = await DurableScheduleTriggerWorker(
        catalog=store,
        cursors=store,
    ).tick(
        agent_id=_AGENT_ID,
        now=retry_not_before,
        handle=handle,
    )
    cursor = await store.get_cursor(
        agent_id=_AGENT_ID,
        schedule_id=_SCHEDULE_ID,
    )
    if cursor is None:
        raise RuntimeError("durable retry cursor disappeared")
    outcome = recovered.outcomes[0]
    print(
        json.dumps(
            {
                "disposition": outcome.disposition.value,
                "handled": [value.isoformat() for value in handled],
                "last_fire_at": cursor.last_fire_at.isoformat(),
                "retry_count": cursor.retry_count,
                "retry_not_before": cursor.retry_not_before,
                "scheduled_for": outcome.scheduled_for.isoformat(),
            },
            default=str,
            sort_keys=True,
        ),
        flush=True,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("defer", "recover"))
    parser.add_argument("database_path", type=Path)
    parser.add_argument("--ready-path", type=Path)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.mode == "defer":
        if args.ready_path is None:
            raise ValueError("defer mode requires --ready-path")
        asyncio.run(
            _leave_deferred_cursor(
                args.database_path,
                args.ready_path,
            ),
        )
        return
    asyncio.run(_recover_deferred_cursor(args.database_path))


if __name__ == "__main__":
    main()
