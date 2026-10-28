# -*- coding: utf-8 -*-
"""Bounded realtime hub for normalized Chat runs."""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Mapping
from uuid import uuid4


LIVE_REPLAY_MAX_EVENTS = 2048
LIVE_REPLAY_MAX_BYTES = 8 * 1024 * 1024
LIVE_SUBSCRIBER_MAX_EVENTS = 512
LIVE_SUBSCRIBER_MAX_BYTES = 2 * 1024 * 1024
LIVE_TERMINAL_RETENTION_S = 60.0


@dataclass(frozen=True)
class ChatLivePath:
    """Identity shared by every event in one run."""

    conversation_id: str
    run_id: str
    turn_id: str

    def as_dict(self) -> dict[str, str]:
        """Return the wire representation."""
        return {
            "conversation_id": self.conversation_id,
            "run_id": self.run_id,
            "turn_id": self.turn_id,
        }


@dataclass(frozen=True)
class ChatLiveCursor:
    """Ephemeral cursor valid only within one process run epoch."""

    run_epoch: str
    event_sequence: int

    def as_dict(self) -> dict[str, str | int]:
        """Return the wire representation."""
        return {
            "run_epoch": self.run_epoch,
            "event_sequence": self.event_sequence,
        }


@dataclass(frozen=True)
class ChatLiveEvent:
    """One ordered live mutation or control event."""

    path: ChatLivePath
    cursor: ChatLiveCursor
    kind: str
    payload: Mapping[str, Any]
    entity_id: str | None = None
    entity_version: int | None = None
    expected_offset: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the stable SSE data envelope."""
        return {
            "path": self.path.as_dict(),
            "cursor": self.cursor.as_dict(),
            "kind": self.kind,
            "payload": self.payload,
            "entity_id": self.entity_id,
            "entity_version": self.entity_version,
            "expected_offset": self.expected_offset,
        }

    @property
    def encoded_bytes(self) -> int:
        """Return deterministic payload size used by resource budgets."""
        return len(
            json.dumps(
                self.as_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8"),
        )


@dataclass(frozen=True)
class ChatLiveSync:
    """Immutable active-view reference captured at subscription barrier."""

    path: ChatLivePath | None
    cursor: ChatLiveCursor | None
    view: Any
    resync_reason: str | None = None
    terminal: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Return the stable sync envelope sent before queued events."""
        return {
            "path": self.path.as_dict() if self.path is not None else None,
            "cursor": (
                self.cursor.as_dict() if self.cursor is not None else None
            ),
            "kind": "sync",
            "view": self.view,
            "resync_reason": self.resync_reason,
            "terminal": self.terminal,
        }


class _LiveQueue(asyncio.Queue):
    """Subscriber queue with explicit count and byte accounting."""

    def __init__(self, *, max_events: int, max_bytes: int) -> None:
        super().__init__()
        self.max_events = max_events
        self.max_bytes = max_bytes
        self.event_count = 0
        self.event_bytes = 0

    def put_event(self, event: ChatLiveEvent) -> bool:
        size = event.encoded_bytes
        if (
            self.event_count + 1 > self.max_events
            or self.event_bytes + size > self.max_bytes
        ):
            return False
        self.put_nowait(event)
        self.event_count += 1
        self.event_bytes += size
        return True

    def put_terminal(self) -> None:
        self.put_nowait(None)

    async def get(self) -> ChatLiveEvent | None:
        item = await super().get()
        self._consume(item)
        return item

    def get_nowait(self) -> ChatLiveEvent | None:
        item = super().get_nowait()
        self._consume(item)
        return item

    def _consume(self, item: ChatLiveEvent | None) -> None:
        if item is None:
            return
        self.event_count = max(0, self.event_count - 1)
        self.event_bytes = max(0, self.event_bytes - item.encoded_bytes)

    def replace_with_resync(self, event: ChatLiveEvent) -> None:
        while not self.empty():
            self.get_nowait()
        self.put_nowait(event)
        self.put_terminal()


@dataclass
class ChatLiveSubscription:
    """Subscription result: send sync first, then consume the queue."""

    sync: ChatLiveSync
    queue: _LiveQueue


@dataclass(frozen=True)
class ChatLiveResourceSnapshot:
    """Immutable counters for one live run's bounded resources."""

    next_sequence: int
    replay_events: int
    replay_bytes: int
    subscribers: int
    terminal: bool


@dataclass
class _LiveRun:
    path: ChatLivePath
    run_epoch: str
    view: Any
    next_sequence: int = 1
    replay: deque[ChatLiveEvent] = field(default_factory=deque)
    replay_bytes: int = 0
    subscribers: list[_LiveQueue] = field(default_factory=list)
    terminal: bool = False
    terminal_at: float | None = None
    cleanup_handle: asyncio.TimerHandle | None = None


class ChatLiveHub:
    """Coordinate subscribe-before-sync with bounded fan-out resources."""

    def __init__(
        self,
        *,
        replay_max_events: int = LIVE_REPLAY_MAX_EVENTS,
        replay_max_bytes: int = LIVE_REPLAY_MAX_BYTES,
        subscriber_max_events: int = LIVE_SUBSCRIBER_MAX_EVENTS,
        subscriber_max_bytes: int = LIVE_SUBSCRIBER_MAX_BYTES,
        terminal_retention_s: float = LIVE_TERMINAL_RETENTION_S,
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
            raise ValueError("ChatLiveHub limits must be positive")
        self._lock = asyncio.Lock()
        self._runs: dict[str, _LiveRun] = {}
        self._replay_max_events = replay_max_events
        self._replay_max_bytes = replay_max_bytes
        self._subscriber_max_events = subscriber_max_events
        self._subscriber_max_bytes = subscriber_max_bytes
        self._terminal_retention_s = terminal_retention_s

    def _queue(self) -> _LiveQueue:
        return _LiveQueue(
            max_events=self._subscriber_max_events,
            max_bytes=self._subscriber_max_bytes,
        )

    async def start_run(self, path: ChatLivePath, view: Any) -> ChatLiveCursor:
        """Register one run and its copy-on-write active view."""
        async with self._lock:
            key = path.conversation_id
            current = self._runs.get(key)
            if current is not None and not current.terminal:
                if current.path == path:
                    return ChatLiveCursor(current.run_epoch, 0)
                raise RuntimeError("Conversation already has an active run")
            if current is not None and current.cleanup_handle is not None:
                current.cleanup_handle.cancel()
            run = _LiveRun(path=path, run_epoch=str(uuid4()), view=view)
            self._runs[key] = run
            return ChatLiveCursor(run.run_epoch, 0)

    async def publish(
        self,
        path: ChatLivePath,
        *,
        kind: str,
        payload: Mapping[str, Any],
        view: Any | None = None,
        entity_id: str | None = None,
        entity_version: int | None = None,
        expected_offset: int | None = None,
        terminal: bool = False,
    ) -> ChatLiveEvent:
        """Publish without awaiting subscribers or retaining unbounded data."""
        async with self._lock:
            run = self._runs.get(path.conversation_id)
            if run is None or run.path != path or run.terminal:
                raise RuntimeError("Chat live run is not active")
            if view is not None:
                run.view = view
            event = ChatLiveEvent(
                path=path,
                cursor=ChatLiveCursor(
                    run_epoch=run.run_epoch,
                    event_sequence=run.next_sequence,
                ),
                kind=kind,
                payload=dict(payload),
                entity_id=entity_id,
                entity_version=entity_version,
                expected_offset=expected_offset,
            )
            run.next_sequence += 1
            run.replay.append(event)
            run.replay_bytes += event.encoded_bytes
            while run.replay and (
                len(run.replay) > self._replay_max_events
                or run.replay_bytes > self._replay_max_bytes
            ):
                removed = run.replay.popleft()
                run.replay_bytes -= removed.encoded_bytes

            active: list[_LiveQueue] = []
            for queue in run.subscribers:
                if queue.put_event(event):
                    active.append(queue)
                    continue
                resync = ChatLiveEvent(
                    path=path,
                    cursor=event.cursor,
                    kind="resync_required",
                    payload={"reason": "subscriber_overflow"},
                )
                queue.replace_with_resync(resync)
            run.subscribers = active
            if terminal:
                run.terminal = True
                run.terminal_at = time.monotonic()
                for queue in run.subscribers:
                    queue.put_terminal()
                run.subscribers.clear()
                loop = asyncio.get_running_loop()
                key = path.conversation_id
                run.cleanup_handle = loop.call_later(
                    self._terminal_retention_s,
                    self._schedule_expiry,
                    key,
                    run.run_epoch,
                )
            return event

    def _schedule_expiry(
        self,
        key: str,
        run_epoch: str,
    ) -> None:
        asyncio.create_task(self._expire_if_current(key, run_epoch))

    async def _expire_if_current(
        self,
        key: str,
        run_epoch: str,
    ) -> None:
        async with self._lock:
            run = self._runs.get(key)
            if run is not None and run.terminal and run.run_epoch == run_epoch:
                self._runs.pop(key, None)

    async def subscribe(
        self,
        conversation_id: str,
        cursor: ChatLiveCursor | None = None,
    ) -> ChatLiveSubscription:
        """Atomically register, then capture barrier and immutable view."""
        async with self._lock:
            run = self._runs.get(conversation_id)
            queue = self._queue()
            if run is None:
                return ChatLiveSubscription(
                    sync=ChatLiveSync(
                        path=None,
                        cursor=None,
                        view=None,
                        resync_reason="epoch_lost",
                        terminal=True,
                    ),
                    queue=queue,
                )
            barrier = ChatLiveCursor(
                run_epoch=run.run_epoch,
                event_sequence=run.next_sequence - 1,
            )
            reason = self._cursor_resync_reason(run, cursor, barrier)
            if not run.terminal:
                run.subscribers.append(queue)
            else:
                queue.put_terminal()
            return ChatLiveSubscription(
                sync=ChatLiveSync(
                    path=run.path,
                    cursor=barrier,
                    view=run.view,
                    resync_reason=reason,
                    terminal=run.terminal,
                ),
                queue=queue,
            )

    async def resource_snapshot(
        self,
        conversation_id: str,
    ) -> ChatLiveResourceSnapshot | None:
        """Return immutable resource counters for rollout observability."""
        async with self._lock:
            run = self._runs.get(conversation_id)
            if run is None:
                return None
            return ChatLiveResourceSnapshot(
                next_sequence=run.next_sequence,
                replay_events=len(run.replay),
                replay_bytes=run.replay_bytes,
                subscribers=len(run.subscribers),
                terminal=run.terminal,
            )

    @staticmethod
    def _cursor_resync_reason(
        run: _LiveRun,
        cursor: ChatLiveCursor | None,
        barrier: ChatLiveCursor,
    ) -> str | None:
        if cursor is None:
            return None
        if cursor.run_epoch != run.run_epoch:
            return "epoch_lost"
        if cursor.event_sequence > barrier.event_sequence:
            return "cursor_ahead"
        oldest = run.replay[0].cursor.event_sequence if run.replay else 0
        if cursor.event_sequence < oldest - 1:
            return "cursor_evicted"
        return None

    async def unsubscribe(self, subscription: ChatLiveSubscription) -> None:
        """Detach a subscriber idempotently."""
        path = subscription.sync.path
        if path is None:
            return
        async with self._lock:
            run = self._runs.get(path.conversation_id)
            if run is None or run.path != path:
                return
            try:
                run.subscribers.remove(subscription.queue)
            except ValueError:
                pass

    async def cleanup_expired(self, now: float | None = None) -> int:
        """Remove terminal summaries after their reconnect grace period."""
        instant = time.monotonic() if now is None else now
        async with self._lock:
            expired = [
                key
                for key, run in self._runs.items()
                if run.terminal
                and run.terminal_at is not None
                and instant - run.terminal_at >= self._terminal_retention_s
            ]
            for key in expired:
                run = self._runs.pop(key, None)
                if run is not None and run.cleanup_handle is not None:
                    run.cleanup_handle.cancel()
            return len(expired)

    async def invalidate_conversation(self, conversation_id: str) -> int:
        """Close subscribers and forget a deleted Chat."""
        async with self._lock:
            keys = [key for key in self._runs if key == conversation_id]
            for key in keys:
                run = self._runs.pop(key)
                if run.cleanup_handle is not None:
                    run.cleanup_handle.cancel()
                for queue in run.subscribers:
                    queue.put_terminal()
                run.subscribers.clear()
            return len(keys)
