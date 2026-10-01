# -*- coding: utf-8 -*-
"""Chat-owned read adapter over authoritative Task verification events."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ..kernel import Task, VerificationHistoryPort, VerificationRecord
from ..kernel.ports import TaskStore
from .ledger import SQLiteExecutionLedger
from .results import TaskEventReader, load_task_result_projection

_TASK_PAGE_SIZE = 200


class TaskVerificationHistory(VerificationHistoryPort):
    """Project verification records without copying their source ledger."""

    def __init__(
        self,
        tasks: TaskStore,
        events: TaskEventReader,
    ) -> None:
        self._tasks = tasks
        self._events = events

    @staticmethod
    def _belongs_to_conversation(
        task: Task,
        conversation_id: str,
    ) -> bool:
        return conversation_id in (
            task.metadata.get("conversation_id"),
            task.metadata.get("chat_id"),
        )

    async def _matching_tasks(self, conversation_id: str) -> list[Task]:
        matches: list[Task] = []
        cursor = None
        while True:
            tasks = list(
                await self._tasks.list_tasks(
                    cursor=cursor,
                    limit=_TASK_PAGE_SIZE,
                ),
            )
            matches.extend(
                task
                for task in tasks
                if self._belongs_to_conversation(task, conversation_id)
            )
            if len(tasks) < _TASK_PAGE_SIZE:
                return matches
            cursor = str(tasks[-1].task_id)

    async def list_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 100,
    ) -> Sequence[VerificationRecord]:
        """Return newest records derived from matching Task ledgers."""
        if not conversation_id.strip():
            raise ValueError("conversation_id cannot be empty")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        records = []
        for task in await self._matching_tasks(conversation_id):
            projection = await load_task_result_projection(
                self._events,
                task.task_id,
            )
            records.extend(projection.verification_records)
        records.sort(
            key=lambda record: (
                record.occurred_at or record.verification.created_at,
                str(record.verification.verification_id),
            ),
            reverse=True,
        )
        return records[:limit]


def lite_verification_history(
    workspace_dir: Path,
) -> TaskVerificationHistory:
    """Return the Lite read adapter over the shared Task SQLite ledger."""
    ledger = SQLiteExecutionLedger(
        Path(workspace_dir) / ".qwenpaw" / "lite" / "tasks.db",
    )
    return TaskVerificationHistory(ledger, ledger)


__all__ = [
    "TaskVerificationHistory",
    "lite_verification_history",
]
