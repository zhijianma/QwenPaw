# -*- coding: utf-8 -*-
"""Legacy PawApp task compatibility adapter.

New task, interaction, event and artifact semantics belong to the unified
QwenPaw Runtime. This module only preserves existing ``@app.task`` behavior
while PawApps migrate to public Contributions.

Enables ``ctx.ui.push()`` to send realtime events to the frontend,
and ``paw.api.task()`` to subscribe to those events via EventSource.

Backend flow:
    task_id = await task_manager.create_task(app_id, handler, ctx, params)
    # Inside handler: await ctx.ui.push("progress", {"step": 2})

Frontend flow:
    const task = paw.api.task('/generate', { script });
    task.on('progress', (data) => setProgress(data.step));
    const result = await task.result;

HTTP endpoints (registered by pawapps router):
    POST /api/pawapp/{app_id}/task       → create task, returns {task_id}
    GET  /api/pawapp/{app_id}/task/{id}/stream → SSE event stream
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import suppress
from typing import Any, AsyncIterator, Callable, Dict, Optional
from uuid import UUID

logger = logging.getLogger(__name__)


class SSEChannel:
    """Legacy in-process Server-Sent Events delivery channel.

    Producers call ``send_event(data)``; consumers iterate with
    ``async for event in channel``.
    """

    def __init__(self, max_buffer: int = 1000):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max_buffer)
        self._closed = False

    async def send_event(self, data: Dict[str, Any]) -> None:
        """Send an event to the channel (non-blocking for producer)."""
        if self._closed:
            return
        try:
            self._queue.put_nowait(data)
        except asyncio.QueueFull:
            logger.warning("SSEChannel buffer full, dropping event")

    def close(self) -> None:
        """Mark the channel as closed."""
        self._closed = True
        # Put a sentinel to unblock consumers
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    @property
    def is_closed(self) -> bool:
        return self._closed

    async def __aiter__(self) -> AsyncIterator[str]:
        """Yield SSE-formatted strings until channel is closed."""
        while True:
            # Check if channel is closed and queue is empty
            if self._closed and self._queue.empty():
                break

            try:
                event = await asyncio.wait_for(
                    self._queue.get(),
                    timeout=30.0,
                )
            except asyncio.TimeoutError:
                # Send keepalive comment
                yield ": keepalive\n\n"
                continue

            if event is None:
                # Channel closed sentinel
                break

            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


class TaskRecord:
    """Non-durable compatibility record for one legacy PawApp task."""

    def __init__(
        self,
        task_id: str,
        app_id: str,
        channel: SSEChannel,
    ):
        self.task_id = task_id
        self.app_id = app_id
        self.channel = channel
        self.result: Any = None
        self.error: Optional[str] = None
        self.done = False
        self.created_at: float = time.monotonic()
        self.chat_id: Optional[str] = None
        self.agent_id: str = ""
        self.user_id: str = ""
        self.cancel_runtime: Optional[Callable] = None
        self.runner: Optional[asyncio.Task] = None


class TaskManager:
    """Run legacy PawApp handlers until they migrate to Runner slots."""

    def __init__(self):
        self._tasks: Dict[str, TaskRecord] = {}

    async def create_task(
        self,
        app_id: str,
        handler: Callable,
        ctx: Any,
        params: Dict[str, Any],
    ) -> str:
        """Create a new long-running task.

        The handler receives ``ctx`` (with SSE channel injected) and
        ``params``. Returns ``task_id`` for the frontend to subscribe.
        """
        task_id = str(uuid.uuid4())
        channel = SSEChannel()

        record = TaskRecord(task_id=task_id, app_id=app_id, channel=channel)
        record.agent_id = str(getattr(ctx, "agent_id", ""))
        record.user_id = str(getattr(ctx, "user_id", ""))
        record.cancel_runtime = getattr(ctx, "_cancel_task_runtime", None)
        self._tasks[task_id] = record

        bind_runtime = getattr(ctx, "bind_task_runtime", None)
        try:
            if callable(bind_runtime):
                record.chat_id = await bind_runtime(
                    sse_channel=channel,
                    invocation_id=UUID(task_id),
                )
            else:
                # Compatibility for contexts created before the OS runtime.
                # pylint: disable-next=protected-access
                ctx._sse_channel = channel
        except Exception:
            self._tasks.pop(task_id, None)
            channel.close()
            raise

        async def _run():
            try:
                result = await handler(ctx, **params)
                record.result = result
                await channel.send_event({"type": "done", "data": result})
            except Exception as exc:
                record.error = str(exc)
                await channel.send_event(
                    {
                        "type": "error",
                        "message": str(exc),
                    },
                )
                logger.error(
                    "PawApp task %s failed: %s",
                    task_id,
                    exc,
                    exc_info=True,
                )
            finally:
                record.done = True
                channel.close()

        record.runner = asyncio.create_task(_run())
        return task_id

    def get_task(self, task_id: str) -> Optional[TaskRecord]:
        """Get a task record by ID."""
        return self._tasks.get(task_id)

    async def stream(self, task_id: str) -> AsyncIterator[str]:
        """Yield SSE events for a task. Used by the streaming endpoint."""
        record = self._tasks.get(task_id)
        if record is None:
            payload = json.dumps(
                {"type": "error", "message": "Task not found"},
            )
            yield f"data: {payload}\n\n"
            return

        async for event_str in record.channel:
            yield event_str

    def cleanup_task(self, task_id: str) -> None:
        """Remove a completed task from memory."""
        self._tasks.pop(task_id, None)

    async def cancel_task(
        self,
        task_id: str,
        *,
        app_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> bool:
        """Cancel one app-owned task and its pending interactions."""
        record = self._tasks.get(task_id)
        if record is None:
            return False
        if app_id is not None and record.app_id != app_id:
            return False
        if agent_id is not None and record.agent_id != agent_id:
            return False
        if user_id is not None and record.user_id != user_id:
            return False
        if record.done:
            return True
        if callable(record.cancel_runtime):
            await record.cancel_runtime(UUID(task_id))
        record.error = "Task cancelled"
        await record.channel.send_event(
            {
                "type": "error",
                "message": record.error,
                "code": "TASK_CANCELLED",
            },
        )
        if record.runner is not None:
            record.runner.cancel()
            with suppress(asyncio.CancelledError):
                await record.runner
        return True

    def cleanup_old_tasks(
        self,
        max_age_seconds: int = 3600,
    ) -> None:
        """Remove completed tasks older than *max_age_seconds*."""
        now = time.monotonic()
        to_remove = [
            tid
            for tid, rec in self._tasks.items()
            if rec.done and (now - rec.created_at) > max_age_seconds
        ]
        for tid in to_remove:
            del self._tasks[tid]


# Module-level singleton
_task_manager: Optional[TaskManager] = None


def get_task_manager() -> TaskManager:
    """Get or create the global TaskManager singleton."""
    global _task_manager
    if _task_manager is None:
        _task_manager = TaskManager()
    return _task_manager
