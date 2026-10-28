# -*- coding: utf-8 -*-
"""Task tracker for background runs: streaming, reconnect, multi-subscriber.

``run_key`` is typically ``ChatSpec.id`` (chat_id). Per run: task, queues,
event buffer. Reconnects get buffer replay + new events. Cleanup when task
completes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import weakref
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Optional,
)
from uuid import uuid4

if TYPE_CHECKING:
    from .chats.live import ChatLiveHub

logger = logging.getLogger(__name__)

_SENTINEL = None

# Indirection so tests can simulate a failing task creation without
# patching the process-wide ``asyncio`` module.
_create_task = asyncio.create_task

# Emitted to reconnect subscribers right after the buffered events, so
# the client can render the replayed part instantly (no token-by-token
# re-animation) and switch to live streaming afterwards.
REPLAY_END_SSE = f"data: {json.dumps({'type': 'replay_end'})}\n\n"
_RESYNC_REQUIRED_PAYLOAD = {
    "type": "resync_required",
    "reason": "subscriber_overflow",
}
RESYNC_REQUIRED_SSE = f"data: {json.dumps(_RESYNC_REQUIRED_PAYLOAD)}\n\n"

REPLAY_MAX_EVENTS = 2048
REPLAY_MAX_BYTES = 8 * 1024 * 1024
SUBSCRIBER_MAX_EVENTS = 512
SUBSCRIBER_MAX_BYTES = 2 * 1024 * 1024


def _event_size(event: str) -> int:
    return len(event.encode("utf-8"))


class _SubscriberQueue(asyncio.Queue):
    """Queue with independent payload count/byte accounting."""

    def __init__(self, *, max_events: int, max_bytes: int) -> None:
        super().__init__()
        self.max_events = max_events
        self.max_bytes = max_bytes
        self.payload_events = 0
        self.payload_bytes = 0

    def put_payload(self, event: str) -> bool:
        """Append one payload without blocking, or report overflow."""
        size = _event_size(event)
        if (
            self.payload_events + 1 > self.max_events
            or self.payload_bytes + size > self.max_bytes
        ):
            return False
        self.put_nowait(event)
        self.payload_events += 1
        self.payload_bytes += size
        return True

    def put_control(self, event: str | None) -> None:
        """Append a small lifecycle control outside the payload budget."""
        self.put_nowait(event)

    async def get(self) -> str | None:
        item = await super().get()
        self._consume(item)
        return item

    def get_nowait(self) -> str | None:
        item = super().get_nowait()
        self._consume(item)
        return item

    def _consume(self, item: str | None) -> None:
        if not isinstance(item, str):
            return
        if item in {REPLAY_END_SSE, RESYNC_REQUIRED_SSE}:
            return
        self.payload_events = max(0, self.payload_events - 1)
        self.payload_bytes = max(0, self.payload_bytes - _event_size(item))

    def close_for_resync(self) -> None:
        """Discard queued payload and terminate with one resync signal."""
        while not self.empty():
            self.get_nowait()
        self.put_control(RESYNC_REQUIRED_SSE)
        self.put_control(_SENTINEL)


@dataclass(frozen=True)
class _BufferedEvent:
    sequence: int
    data: str
    size: int


@dataclass
class _RunState:
    """Per-run state (task, queues, buffer), guarded by tracker lock."""

    task: asyncio.Future
    queues: list[_SubscriberQueue] = field(default_factory=list)
    buffer: deque[_BufferedEvent] = field(default_factory=deque)
    buffer_bytes: int = 0
    run_epoch: str = field(default_factory=lambda: str(uuid4()))
    next_event_sequence: int = 1
    start_time: Optional[datetime] = None
    finish_time: Optional[datetime] = None
    owner: object | None = None


class TaskTracker:
    """Per-agent tracker: run_key -> RunState.

    All mutations to _runs under _lock. Producer broadcasts under lock.
    Replay and per-subscriber queues are bounded independently by event count
    and UTF-8 bytes. A slow subscriber is detached with ``resync_required``;
    it never backpressures the runtime producer. A workspace reload reuses the
    same tracker so active runs remain reconnectable.
    """

    def __init__(
        self,
        *,
        replay_max_events: int = REPLAY_MAX_EVENTS,
        replay_max_bytes: int = REPLAY_MAX_BYTES,
        subscriber_max_events: int = SUBSCRIBER_MAX_EVENTS,
        subscriber_max_bytes: int = SUBSCRIBER_MAX_BYTES,
    ) -> None:
        if (
            min(
                replay_max_events,
                replay_max_bytes,
                subscriber_max_events,
                subscriber_max_bytes,
            )
            <= 0
        ):
            raise ValueError("TaskTracker limits must be positive")
        self._lock = asyncio.Lock()
        self._runs: dict[str, _RunState] = {}
        self._replay_max_events = replay_max_events
        self._replay_max_bytes = replay_max_bytes
        self._subscriber_max_events = subscriber_max_events
        self._subscriber_max_bytes = subscriber_max_bytes
        self._global_last_run_at: Optional[datetime] = None
        self._global_last_finish_at: Optional[datetime] = None
        self._chat_live_hub: ChatLiveHub | None = None

    @property
    def chat_live_hub(self) -> ChatLiveHub:
        """Lazily return normalized Chat live state for this agent."""
        if self._chat_live_hub is None:
            from .chats.live import ChatLiveHub

            self._chat_live_hub = ChatLiveHub()
        return self._chat_live_hub

    def _new_subscriber(self) -> _SubscriberQueue:
        return _SubscriberQueue(
            max_events=self._subscriber_max_events,
            max_bytes=self._subscriber_max_bytes,
        )

    def _attach_locked(
        self,
        state: _RunState,
        *,
        replay_end: bool,
    ) -> _SubscriberQueue:
        queue = self._new_subscriber()
        for event in state.buffer:
            if not queue.put_payload(event.data):
                queue.close_for_resync()
                return queue
        if replay_end:
            queue.put_control(REPLAY_END_SSE)
        state.queues.append(queue)
        return queue

    def _publish_locked(self, state: _RunState, event: str) -> None:
        size = _event_size(event)
        buffered = _BufferedEvent(
            sequence=state.next_event_sequence,
            data=event,
            size=size,
        )
        state.next_event_sequence += 1
        state.buffer.append(buffered)
        state.buffer_bytes += size
        while state.buffer and (
            len(state.buffer) > self._replay_max_events
            or state.buffer_bytes > self._replay_max_bytes
        ):
            removed = state.buffer.popleft()
            state.buffer_bytes -= removed.size

        active: list[_SubscriberQueue] = []
        for queue in state.queues:
            if queue.put_payload(event):
                active.append(queue)
            else:
                queue.close_for_resync()
        state.queues = active

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    async def get_status(self, run_key: str) -> str:
        """Return ``'idle'`` or ``'running'``."""
        async with self._lock:
            state = self._runs.get(run_key)
        if state is None or state.task.done():
            return "idle"
        return "running"

    async def get_global_status(self) -> dict:
        """Get global agent status summary.

        Returns:
            dict with keys:
                - status: 'idle' | 'running'
                - running_task_count: int
                - last_run_at: Optional[datetime]
                - last_finish_at: Optional[datetime]
        """
        async with self._lock:
            running_count = sum(
                1 for state in self._runs.values() if not state.task.done()
            )
            status = "running" if running_count > 0 else "idle"

            return {
                "status": status,
                "running_task_count": running_count,
                "last_run_at": self._global_last_run_at,
                "last_finish_at": self._global_last_finish_at,
            }

    async def has_active_tasks(self) -> bool:
        """Check if any tasks are currently running.

        Returns:
            bool: True if any tasks are active, False otherwise
        """
        async with self._lock:
            for state in self._runs.values():
                if not state.task.done():
                    return True
            return False

    async def has_active_tasks_excluding(
        self,
        excluded_task: asyncio.Future | None,
    ) -> bool:
        """Check for active tasks other than ``excluded_task``."""
        async with self._lock:
            return any(
                not state.task.done() and state.task is not excluded_task
                for state in self._runs.values()
            )

    async def list_active_tasks(self) -> list[str]:
        """List all currently running task keys.

        Returns:
            list[str]: List of active run_keys
        """
        async with self._lock:
            return [
                run_key
                for run_key, state in self._runs.items()
                if not state.task.done()
            ]

    async def snapshot_active_tasks(
        self,
        owner: object | None = None,
    ) -> dict[str, asyncio.Future]:
        """Return the currently active tasks without tracking later runs."""
        async with self._lock:
            return {
                run_key: state.task
                for run_key, state in self._runs.items()
                if not state.task.done()
                and (owner is None or state.owner is owner)
            }

    async def wait_tasks_done(
        self,
        tasks: list[asyncio.Future],
        timeout: float = 300.0,
    ) -> bool:
        """Wait for a fixed task snapshot without cancelling on timeout."""
        if not tasks:
            return True

        async def _wait_snapshot() -> None:
            await asyncio.gather(
                *(asyncio.shield(task) for task in tasks),
                return_exceptions=True,
            )

        try:
            await asyncio.wait_for(_wait_snapshot(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_all_done(self, timeout: float = 300.0) -> bool:
        """Wait for all active tasks to complete.

        Args:
            timeout: Maximum time to wait in seconds (default: 300s = 5min)

        Returns:
            bool: True if all tasks completed, False if timeout occurred
        """

        async def _wait_loop() -> None:
            while await self.has_active_tasks():
                await asyncio.sleep(0.5)

        try:
            await asyncio.wait_for(_wait_loop(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def attach(self, run_key: str) -> asyncio.Queue | None:
        """Attach to an existing run.

        Returns a new queue pre-filled with the event buffer plus a
        ``replay_end`` marker, or ``None`` if no run is active for
        *run_key*.
        """
        async with self._lock:
            state = self._runs.get(run_key)
            if state is None or state.task.done():
                return None
            return self._attach_locked(state, replay_end=True)

    async def detach_subscriber(
        self,
        run_key: str,
        queue: _SubscriberQueue,
    ) -> None:
        """Remove *queue* from *run_key*'s subscriber list.

        Idempotent if the run ended or *queue* was already removed.
        """
        async with self._lock:
            state = self._runs.get(run_key)
            if state is None:
                return
            try:
                state.queues.remove(queue)
            except ValueError:
                pass

    async def request_stop(
        self,
        run_key: str,
        *,
        reason: str = "user",
    ) -> bool:
        """Cancel the run. Returns ``True`` if it was running."""
        logger.debug("[STOP] request_stop called for run_key=%s", run_key)
        async with self._lock:
            state = self._runs.get(run_key)
            logger.debug(
                "[STOP] run_key=%s state=%s done=%s",
                run_key,
                "found" if state else "not_found",
                state.task.done() if state else "N/A",
            )
            if state is None or state.task.done():
                logger.debug(
                    "[STOP] Cannot stop run_key=%s (not running)",
                    run_key,
                )
                return False
            logger.debug(
                "[STOP] Calling task.cancel() for run_key=%s",
                run_key,
            )
            task = state.task
            task.cancel(reason)
            logger.debug("[STOP] task.cancel() called for run_key=%s", run_key)
        try:
            await task
        except asyncio.CancelledError:
            pass
        return True

    async def attach_or_start(
        self,
        run_key: str,
        payload: Any,
        stream_fn: Callable[[Any], AsyncIterator[str]],
        owner: object | None = None,
        on_finished: Callable[[str, datetime], Awaitable[Any]] | None = None,
    ) -> tuple[asyncio.Queue, bool]:
        """Attach to an existing run or start a new one.

        Returns ``(queue, is_new_run)``.
        """
        async with self._lock:
            state = self._runs.get(run_key)
            if state is not None and not state.task.done():
                return self._attach_locked(state, replay_end=False), False

            my_queue = self._new_subscriber()
            run = _RunState(
                task=asyncio.Future(),  # placeholder, replaced below
                queues=[my_queue],
                buffer=deque(),
                owner=owner,
            )

            tracker_ref = weakref.ref(self)

            async def _producer() -> None:
                start_time = datetime.now(timezone.utc)

                try:
                    tracker = tracker_ref()
                    if tracker is not None:
                        async with tracker.lock:
                            run.start_time = start_time
                            # pylint: disable=protected-access
                            tracker._global_last_run_at = start_time

                    async for sse in stream_fn(payload):
                        tracker = tracker_ref()
                        if tracker is None:
                            return
                        async with tracker.lock:
                            # pylint: disable=protected-access
                            tracker._publish_locked(run, sse)
                except asyncio.CancelledError:
                    logger.debug("run cancelled run_key=%s", run_key)
                except Exception:
                    logger.exception("run error run_key=%s", run_key)
                    err_sse = (
                        "data: "
                        f"{json.dumps({'error': 'internal server error'})}\n\n"
                    )
                    tracker = tracker_ref()
                    if tracker is not None:
                        async with tracker.lock:
                            # pylint: disable=protected-access
                            tracker._publish_locked(run, err_sse)
                finally:
                    finish_time = datetime.now(timezone.utc)
                    if on_finished is not None:
                        try:
                            await on_finished(run_key, finish_time)
                        except Exception:  # pylint: disable=broad-except
                            logger.exception(
                                "run completion callback failed run_key=%s",
                                run_key,
                            )
                    tracker = tracker_ref()
                    if tracker is not None:
                        async with tracker.lock:
                            run.finish_time = finish_time
                            # pylint: disable=protected-access
                            tracker._global_last_finish_at = finish_time
                            for q in run.queues:
                                q.put_control(_SENTINEL)
                            # pylint: disable=protected-access
                            tracker._runs.pop(
                                run_key,
                                None,
                            )

            # Register the run only once the producer task exists.
            # Registering a placeholder first would leak a permanent
            # "running" entry -- the status counters read
            # ``not state.task.done()``, and a bare Future is never done --
            # if task creation ever failed. This is defensive hardening:
            # the window is synchronous and a running loop cannot be
            # closed, so the failure is not reachable in practice.
            run.task = _create_task(_producer())
            self._runs[run_key] = run
            return my_queue, True

    async def stream_from_queue(
        self,
        queue: _SubscriberQueue,
        run_key: str,
    ) -> AsyncGenerator[str, None]:
        """Yield SSE strings from *queue* until the sentinel ``None``.

        Always detaches *queue* from *run_key* when this stream ends or is
        closed (including client disconnect), so reconnects do not leak queues.
        """
        try:
            while True:
                try:
                    event = await queue.get()
                    if event is _SENTINEL:
                        break
                    yield event
                except asyncio.CancelledError:
                    break
        finally:
            await self.detach_subscriber(run_key, queue)
