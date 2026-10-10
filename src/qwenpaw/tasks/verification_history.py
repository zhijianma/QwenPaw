# -*- coding: utf-8 -*-
"""Chat-owned read adapter over authoritative Task result events."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ..kernel import (
    ConversationMessageRecord,
    ConversationTaskTranscript,
    ConversationTaskResultRecords,
    Task,
    TaskConversationHistoryPort,
    TaskResultHistoryPort,
    VerificationHistoryPort,
    VerificationRecord,
)
from ..kernel.ports import TaskStore
from .ledger import SQLiteExecutionLedger
from .results import TaskEventReader, load_task_result_projection

_TASK_PAGE_SIZE = 200


class TaskResultHistory(
    TaskResultHistoryPort,
    TaskConversationHistoryPort,
    VerificationHistoryPort,
):
    """Project Task result records without copying their source ledger."""

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
        snapshot = await self.read_for_conversation(conversation_id)
        return snapshot.verifications[:limit]

    async def scan_for_conversation(
        self,
        conversation_id: str,
    ) -> Sequence[VerificationRecord]:
        """Scan all records derived from matching Task ledgers."""
        snapshot = await self.read_for_conversation(conversation_id)
        return snapshot.verifications

    async def read_for_conversation(
        self,
        conversation_id: str,
    ) -> ConversationTaskResultRecords:
        """Return one consistent snapshot from all matching Task ledgers."""
        if not conversation_id.strip():
            raise ValueError("conversation_id cannot be empty")
        artifacts = []
        evidence = []
        verifications = []
        for task in await self._matching_tasks(conversation_id):
            projection = await load_task_result_projection(
                self._events,
                task.task_id,
            )
            artifacts.extend(projection.artifacts)
            evidence.extend(projection.evidence_records)
            verifications.extend(projection.verification_records)
        artifacts.sort(
            key=lambda record: (
                record.created_at,
                str(record.artifact.artifact_id),
            ),
            reverse=True,
        )
        evidence.sort(
            key=lambda record: (
                record.evidence.captured_at,
                str(record.evidence.evidence_id),
            ),
            reverse=True,
        )
        verifications.sort(
            key=lambda record: (
                record.occurred_at or record.verification.created_at,
                str(record.verification.verification_id),
            ),
            reverse=True,
        )
        return ConversationTaskResultRecords(
            chat_id=conversation_id,
            artifacts=tuple(artifacts),
            evidence=tuple(evidence),
            verifications=tuple(verifications),
        )

    async def _task_events(self, task: Task):
        """Read one complete Task event stream in bounded pages."""
        events = []
        after_sequence = 0
        while True:
            page = list(
                await self._events.list_events(
                    task.task_id,
                    after_sequence=after_sequence,
                    limit=_TASK_PAGE_SIZE,
                ),
            )
            events.extend(page)
            if len(page) < _TASK_PAGE_SIZE:
                return events
            after_sequence = page[-1].sequence

    @staticmethod
    def _task_messages(task: Task, events) -> list[ConversationMessageRecord]:
        """Project public message records from one ordered Task ledger."""
        messages: list[ConversationMessageRecord] = []
        open_assistant: dict[object, int] = {}
        for event in events:
            role = event.payload.get("role")
            text = event.payload.get("text")
            if role not in {"user", "assistant"} or not isinstance(text, str):
                continue
            if event.event_type == "conversation.user" and role == "user":
                messages.append(
                    ConversationMessageRecord(
                        message_id=str(event.event_id),
                        task_id=task.task_id,
                        run_id=event.run_id,
                        role="user",
                        text=text,
                        content=({"type": "text", "text": text},),
                        created_at=event.occurred_at,
                        completed_at=event.occurred_at,
                    ),
                )
                continue
            if role != "assistant" or event.event_type not in {
                "conversation.assistant.delta",
                "conversation.assistant.completed",
            }:
                continue
            index = open_assistant.get(event.run_id)
            raw_content = event.payload.get("content")
            content = (
                tuple(
                    dict(item)
                    for item in raw_content
                    if isinstance(item, dict)
                )
                if isinstance(raw_content, list)
                else ()
            )
            completed = event.event_type == "conversation.assistant.completed"
            if index is None:
                messages.append(
                    ConversationMessageRecord(
                        message_id=str(event.event_id),
                        task_id=task.task_id,
                        run_id=event.run_id,
                        role="assistant",
                        status=("completed" if completed else "in_progress"),
                        text=text,
                        content=content,
                        artifact_refs=event.artifact_refs,
                        evidence_refs=event.evidence_refs,
                        created_at=event.occurred_at,
                        completed_at=event.occurred_at,
                    ),
                )
                index = len(messages) - 1
                if not completed:
                    open_assistant[event.run_id] = index
            else:
                previous = messages[index]
                messages[index] = previous.model_copy(
                    update={
                        "status": (
                            "completed" if completed else "in_progress"
                        ),
                        "text": (
                            text if completed else f"{previous.text}{text}"
                        ),
                        "content": content or previous.content,
                        "artifact_refs": (
                            event.artifact_refs or previous.artifact_refs
                        ),
                        "evidence_refs": (
                            event.evidence_refs or previous.evidence_refs
                        ),
                        "completed_at": event.occurred_at,
                    },
                )
            if completed:
                open_assistant.pop(event.run_id, None)
        return messages

    async def read_messages_for_conversation(
        self,
        conversation_id: str,
    ) -> ConversationTaskTranscript:
        """Return public Task messages without copying Session state."""
        if not conversation_id.strip():
            raise ValueError("conversation_id cannot be empty")
        messages: list[ConversationMessageRecord] = []
        for task in await self._matching_tasks(conversation_id):
            messages.extend(
                self._task_messages(task, await self._task_events(task)),
            )
        messages.sort(
            key=lambda message: (
                message.created_at,
                str(message.task_id),
                message.message_id,
            ),
        )
        return ConversationTaskTranscript(
            chat_id=conversation_id,
            messages=tuple(messages),
        )


TaskVerificationHistory = TaskResultHistory


def lite_verification_history(
    workspace_dir: Path,
) -> TaskResultHistory:
    """Return the Lite read adapter over the shared Task SQLite ledger."""
    return lite_task_result_history(workspace_dir)


def lite_task_result_history(
    workspace_dir: Path,
) -> TaskResultHistory:
    """Return Task result history over the shared Task SQLite ledger."""
    ledger = SQLiteExecutionLedger(
        Path(workspace_dir) / ".qwenpaw" / "lite" / "tasks.db",
    )
    return TaskResultHistory(ledger, ledger)


__all__ = [
    "TaskVerificationHistory",
    "TaskResultHistory",
    "lite_task_result_history",
    "lite_verification_history",
]
