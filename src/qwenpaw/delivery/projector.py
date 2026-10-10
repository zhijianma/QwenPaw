# -*- coding: utf-8 -*-
"""Project committed Task events into source-independent deliveries."""

from __future__ import annotations

from ..kernel import (
    DeliveryKind,
    DeliveryMode,
    DeliveryPolicy,
    DeliveryRequest,
    ExecutionEvent,
)
from ..tasks.service import TaskNotFoundError, TaskService

_EVENT_PAGE_SIZE = 200


class TaskDeliveryProjector:
    """Derive bounded delivery requests without changing Task facts."""

    def __init__(self, service: TaskService) -> None:
        self._service = service

    async def project(
        self,
        event: ExecutionEvent,
        policy: DeliveryPolicy,
    ) -> tuple[DeliveryRequest, ...]:
        """Return zero or more idempotent requests for one committed event."""
        task = await self._service.get_task(event.task_id)
        if task is None:
            raise TaskNotFoundError(str(event.task_id))
        agent_id = task.agent_id
        requests: list[DeliveryRequest] = []
        if (
            policy.mode is DeliveryMode.STREAM
            and event.event_type == "conversation.assistant.completed"
            and DeliveryKind.REPLY in policy.kinds
        ):
            text = event.payload.get("text")
            content = event.payload.get("content")
            has_content = isinstance(content, list) and bool(content)
            if isinstance(text, str) and (
                text or has_content or event.artifact_refs
            ):
                requests.append(
                    self._request(
                        event,
                        policy,
                        agent_id,
                        DeliveryKind.REPLY,
                        text,
                    ),
                )
        if (
            policy.mode is DeliveryMode.STREAM
            and event.event_type in {"tool.started", "tool.completed"}
            and DeliveryKind.ACTIVITY in policy.kinds
        ):
            name = str(event.payload.get("name") or "tool")
            requests.append(
                self._request(
                    event,
                    policy,
                    agent_id,
                    DeliveryKind.ACTIVITY,
                    name,
                ),
            )
        if (
            event.event_type == "run.completed"
            and DeliveryKind.RESULT in policy.kinds
            and policy.mode is not DeliveryMode.STREAM
        ):
            text = await self._completed_text(event)
            if not policy.suppresses_text(text):
                requests.append(
                    self._request(
                        event,
                        policy,
                        agent_id,
                        DeliveryKind.RESULT,
                        text or "Task completed",
                    ),
                )
        if (
            event.event_type == "approval.requested"
            and DeliveryKind.APPROVAL in policy.kinds
        ):
            action = str(event.payload.get("action") or "action")
            risk = str(event.payload.get("risk") or "unknown")
            requests.append(
                self._request(
                    event,
                    policy,
                    agent_id,
                    DeliveryKind.APPROVAL,
                    f"Approval required: {action} ({risk})",
                ),
            )
        if (
            event.event_type == "run.failed"
            and DeliveryKind.EXCEPTION in policy.kinds
        ):
            summary = str(
                event.payload.get("error_summary") or "Task failed",
            )
            requests.append(
                self._request(
                    event,
                    policy,
                    agent_id,
                    DeliveryKind.EXCEPTION,
                    summary,
                ),
            )
        if (
            event.artifact_refs
            and DeliveryKind.ARTIFACT_READY in policy.kinds
            and not self._artifacts_suppress_ready(event)
        ):
            requests.append(
                self._request(
                    event,
                    policy,
                    agent_id,
                    DeliveryKind.ARTIFACT_READY,
                    "Artifact ready",
                ),
            )
        return tuple(requests)

    @staticmethod
    def _artifacts_suppress_ready(event: ExecutionEvent) -> bool:
        """Avoid duplicate notifications for embedded or projected results."""
        suppressed = {"embedded", "result_projection"}
        return bool(event.artifact_refs) and all(
            artifact.metadata.get("delivery_disposition") in suppressed
            for artifact in event.artifact_refs
        )

    def _request(
        self,
        event: ExecutionEvent,
        policy: DeliveryPolicy,
        agent_id: str,
        kind: DeliveryKind,
        text: str,
    ) -> DeliveryRequest:
        return DeliveryRequest(
            source_event_id=event.event_id,
            idempotency_key=f"{event.event_id}:{kind.value}",
            agent_id=agent_id,
            registry_generation=event.registry_generation,
            kind=kind,
            mode=policy.mode,
            destination=policy.destination,
            chat_id=policy.destination.chat_id,
            task_id=event.task_id,
            run_id=event.run_id,
            invocation_id=event.invocation_id,
            correlation_id=event.correlation_id,
            artifact_refs=event.artifact_refs,
            evidence_refs=event.evidence_refs,
            payload={
                "text": text,
                "event_type": event.event_type,
                "source": event.payload,
            },
            created_at=event.occurred_at,
        )

    async def _completed_text(self, terminal: ExecutionEvent) -> str:
        text_parts: list[str] = []
        after_sequence = 0
        while True:
            events = await self._service.list_events(
                terminal.task_id,
                after_sequence=after_sequence,
                limit=_EVENT_PAGE_SIZE,
            )
            for event in events:
                if event.sequence > terminal.sequence:
                    return "".join(text_parts)
                if (
                    event.run_id == terminal.run_id
                    and event.event_type == "conversation.assistant.delta"
                ):
                    text = event.payload.get("text")
                    if isinstance(text, str):
                        text_parts.append(text)
            if len(events) < _EVENT_PAGE_SIZE:
                break
            after_sequence = events[-1].sequence
        return "".join(text_parts)


__all__ = ["TaskDeliveryProjector"]
