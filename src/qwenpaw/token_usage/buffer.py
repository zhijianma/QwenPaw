# -*- coding: utf-8 -*-
"""In-memory write buffer with async producer-consumer for token usage.

Design
------
Producer (hot path — model wrapper, called after every LLM response)::

    buffer.enqueue(event)   # synchronous put_nowait, ~100 ns, never blocks

Consumer (single background coroutine)::

    event = await _queue.get()
    _accumulate(event)       # mutates _disk_cache in-place, no lock needed
                             # because only one consumer ever touches the cache

Flush task (every ``flush_interval`` seconds)::

    if _dirty:
        save_data_sync(_disk_cache)   # write only — no disk read needed
        _dirty = False

Key properties
--------------
* ``enqueue()`` is **synchronous** — zero ``await``, no contention.
* ``_disk_cache`` is the **authoritative** in-memory state and is only
  mutated by the single consumer coroutine — no lock required.
* Flush writes ``_disk_cache`` directly — no read-before-write.
* On startup the cache is seeded from disk (one-time read).
* ``get_merged_data()`` returns the cache plus any events still in the
  queue that have not been consumed yet.
* ``stop()`` drains the queue fully before the final disk write.
"""

import asyncio
import copy
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import NamedTuple, Optional

from .storage import load_data, prune_sessions, rollup_daily, save_data_sync

logger = logging.getLogger(__name__)

_DEFAULT_FLUSH_INTERVAL = 10  # seconds
_QUEUE_MAX = 10_000           # backpressure ceiling — discard if exceeded


class _UsageEvent(NamedTuple):
    """Immutable record placed on the queue by the producer."""

    provider_id: str
    model_name: str
    prompt_tokens: int
    completion_tokens: int
    date_str: str        # YYYY-MM-DD, pre-computed by producer
    now_iso: str         # ISO-8601 timestamp, pre-computed by producer
    session_id: str      # empty string if unknown
    agent_id: str        # empty string if unknown


class TokenUsageBuffer:
    """Async producer-consumer buffer that periodically flushes to disk.

    ``_disk_cache`` mirrors the full three-section JSON schema and is
    always kept up-to-date by the consumer.  The flush task writes it
    directly without reading from disk first.
    """

    def __init__(
        self,
        path: Path,
        flush_interval: int = _DEFAULT_FLUSH_INTERVAL,
    ) -> None:
        self._path = path
        self._flush_interval = flush_interval

        # Authoritative in-memory state — only mutated by the consumer.
        # Seeded from disk on first start().
        self._disk_cache: dict = {"daily": {}, "sessions": {}, "agents": {}}
        self._cache_loaded = False

        self._dirty: bool = False
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._consumer_task: Optional[asyncio.Task] = None
        self._flush_task: Optional[asyncio.Task] = None
        self._stopped = False
        # Track which calendar date compaction was last run so it fires at
        # most once per day regardless of flush frequency.
        self._last_compact_date: Optional[date] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start consumer and flush tasks (call once from async context)."""
        if self._consumer_task is not None:
            return
        self._stopped = False
        self._consumer_task = asyncio.create_task(
            self._consumer_loop(), name="token-usage-consumer"
        )
        self._flush_task = asyncio.create_task(
            self._flush_loop(), name="token-usage-flush"
        )

    async def stop(self) -> None:
        """Drain queue, stop tasks, perform final flush."""
        self._stopped = True

        # Cancel periodic flush first so it does not race with drain.
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None

        # Drain all queued events through the consumer.
        if self._consumer_task is not None:
            # Signal consumer to finish by waiting for queue to empty.
            await self._queue.join()
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass
            self._consumer_task = None

        # Final flush to disk.
        await self._flush_once(force=True)

    # ------------------------------------------------------------------
    # Producer — hot path, synchronous, never blocks
    # ------------------------------------------------------------------

    def enqueue(self, event: _UsageEvent) -> None:
        """Put an event on the queue.  Synchronous — no ``await`` required.

        If the queue is full (> ``_QUEUE_MAX`` items), the event is
        silently dropped rather than blocking the caller.  This is an
        extreme edge case (burst of 10 000 LLM calls before the consumer
        can process them).
        """
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(
                "token_usage: queue full (%d), dropping event for %s:%s",
                _QUEUE_MAX,
                event.provider_id,
                event.model_name,
            )

    # ------------------------------------------------------------------
    # Read — returns cache merged with any not-yet-consumed queue events
    # ------------------------------------------------------------------

    async def get_merged_data(self) -> dict:
        """Return a consistent view of all known token usage.

        Combines ``_disk_cache`` (fully processed events) with a snapshot
        of events currently sitting in the queue (not yet consumed).
        The merge is purely in-memory — no disk I/O.
        """
        if not self._cache_loaded:
            await self._seed_cache()

        # Deep-copy the cache so the caller can freely iterate it while
        # the consumer continues mutating the original.
        result = copy.deepcopy(self._disk_cache)

        # Peek at pending queue items and fold them in.
        pending = list(self._queue._queue)  # type: ignore[attr-defined]
        for ev in pending:
            _apply_event(result, ev)

        return result

    # ------------------------------------------------------------------
    # Consumer — single coroutine, no lock needed
    # ------------------------------------------------------------------

    async def _consumer_loop(self) -> None:
        """Drain events from the queue one by one, updating ``_disk_cache``."""
        # Ensure cache is loaded before processing events.
        if not self._cache_loaded:
            await self._seed_cache()
        try:
            while True:
                event = await self._queue.get()
                try:
                    _apply_event(self._disk_cache, event)
                    self._dirty = True
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            # Drain whatever is left synchronously before exiting.
            while not self._queue.empty():
                try:
                    event = self._queue.get_nowait()
                    _apply_event(self._disk_cache, event)
                    self._dirty = True
                    self._queue.task_done()
                except asyncio.QueueEmpty:
                    break

    # ------------------------------------------------------------------
    # Flush — write _disk_cache to disk (no read needed)
    # ------------------------------------------------------------------

    async def _flush_once(self, force: bool = False) -> None:
        """Write ``_disk_cache`` to disk if dirty, and run daily compaction."""
        if not self._dirty and not force:
            return
        self._dirty = False

        # Run compaction at most once per calendar day.
        today = date.today()
        if self._last_compact_date != today:
            compacted = rollup_daily(self._disk_cache)
            compacted |= prune_sessions(self._disk_cache)
            self._last_compact_date = today
            if compacted:
                # Compaction changed the cache — ensure it is written even if
                # no new events arrived (force the write below).
                force = True

        snapshot = copy.deepcopy(self._disk_cache)
        await asyncio.to_thread(save_data_sync, self._path, snapshot)
        logger.debug("token_usage: flushed cache to disk")

    async def _flush_loop(self) -> None:
        """Periodically flush the cache to disk."""
        try:
            while not self._stopped:
                await asyncio.sleep(self._flush_interval)
                try:
                    await self._flush_once()
                except Exception:
                    logger.exception("token_usage: error during periodic flush")
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Cache seeding (one-time startup read)
    # ------------------------------------------------------------------

    async def _seed_cache(self) -> None:
        """Load existing data from disk into ``_disk_cache`` (once)."""
        if self._cache_loaded:
            return
        self._disk_cache = await load_data(self._path)
        self._cache_loaded = True
        logger.debug("token_usage: cache seeded from disk")


# ---------------------------------------------------------------------------
# Pure helper — stateless, no I/O, applied by both consumer and get_merged_data
# ---------------------------------------------------------------------------

def _apply_event(cache: dict, ev: _UsageEvent) -> None:
    """Accumulate a single usage event into *cache* in-place."""
    composite_key = f"{ev.provider_id}:{ev.model_name}"

    # ---- daily ----
    day_bucket = cache["daily"].setdefault(ev.date_str, {})
    entry = day_bucket.setdefault(
        composite_key,
        {
            "provider_id": ev.provider_id,
            "model_name": ev.model_name,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "call_count": 0,
        },
    )
    entry["prompt_tokens"] += ev.prompt_tokens
    entry["completion_tokens"] += ev.completion_tokens
    entry["call_count"] += 1

    # ---- session ----
    if ev.session_id:
        sess = cache["sessions"].setdefault(
            ev.session_id,
            {
                "agent_id": ev.agent_id,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "call_count": 0,
                "last_updated": ev.now_iso,
            },
        )
        sess["prompt_tokens"] += ev.prompt_tokens
        sess["completion_tokens"] += ev.completion_tokens
        sess["call_count"] += 1
        sess["last_updated"] = ev.now_iso
        if not sess.get("agent_id") and ev.agent_id:
            sess["agent_id"] = ev.agent_id

    # ---- agent ----
    if ev.agent_id:
        ag = cache["agents"].setdefault(
            ev.agent_id,
            {"prompt_tokens": 0, "completion_tokens": 0, "call_count": 0},
        )
        ag["prompt_tokens"] += ev.prompt_tokens
        ag["completion_tokens"] += ev.completion_tokens
        ag["call_count"] += 1


__all__ = ["TokenUsageBuffer", "_UsageEvent"]
