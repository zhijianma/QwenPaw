# -*- coding: utf-8 -*-
"""Accelerate frozen long-run and concurrent Chat live workloads."""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import platform
import time
import tracemalloc
from pathlib import Path
from typing import Any

from qwenpaw.app.chats.acceptance_datasets import (
    CONCURRENT_DATASET_SPEC,
    LIVE_RUN_DATASET_SPEC,
)
from qwenpaw.app.chats.live import (
    LIVE_REPLAY_MAX_BYTES,
    LIVE_REPLAY_MAX_EVENTS,
    ChatLiveCursor,
    ChatLiveHub,
    ChatLivePath,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--git-commit", default="unknown")
    parser.add_argument(
        "--wall-clock-seconds",
        type=int,
        default=0,
        help="Pace events in real time for this duration; zero accelerates",
    )
    return parser.parse_args()


async def _long_run_volume() -> dict[str, Any]:
    hub = ChatLiveHub()
    path = ChatLivePath(
        conversation_id="long-run-chat",
        run_id="long-run",
        turn_id="turn",
    )
    await hub.start_run(path, {})
    gc.collect()
    tracemalloc.start()
    baseline_bytes, _ = tracemalloc.get_traced_memory()
    started = time.perf_counter()
    for index in range(LIVE_RUN_DATASET_SPEC.expected_event_count):
        await hub.publish(
            path,
            kind="block_append",
            payload={"delta": "x", "index": index},
        )
    duration = time.perf_counter() - started
    retained_bytes, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    snapshot = await hub.resource_snapshot(path.conversation_id)
    if snapshot is None:
        raise AssertionError("long-run live state disappeared")
    if (
        snapshot.next_sequence
        != LIVE_RUN_DATASET_SPEC.expected_event_count + 1
    ):
        raise AssertionError("long-run event sequence changed")
    if snapshot.replay_events > LIVE_REPLAY_MAX_EVENTS:
        raise AssertionError("long-run replay count exceeded its limit")
    if snapshot.replay_bytes > LIVE_REPLAY_MAX_BYTES:
        raise AssertionError("long-run replay bytes exceeded their limit")
    return {
        "simulated_duration_seconds": LIVE_RUN_DATASET_SPEC.duration_seconds,
        "event_count": LIVE_RUN_DATASET_SPEC.expected_event_count,
        "execution_seconds": duration,
        "events_per_execution_second": (
            LIVE_RUN_DATASET_SPEC.expected_event_count / duration
        ),
        "replay_events": snapshot.replay_events,
        "replay_bytes": snapshot.replay_bytes,
        "subscriber_count": snapshot.subscribers,
        "python_traced_retained_delta_bytes": (
            retained_bytes - baseline_bytes
        ),
        "python_traced_peak_delta_bytes": peak_bytes - baseline_bytes,
        "correctness": "passed",
    }


async def _wall_clock_long_run(duration_seconds: int) -> dict[str, Any]:
    if duration_seconds < 1:
        raise ValueError("wall-clock duration must be positive")
    event_count = (
        duration_seconds * LIVE_RUN_DATASET_SPEC.batched_events_per_second
    )
    hub = ChatLiveHub()
    path = ChatLivePath(
        conversation_id="wall-clock-chat",
        run_id="wall-clock",
        turn_id="turn",
    )
    await hub.start_run(path, {})
    subscription = await hub.subscribe(path.conversation_id)
    last_cursor: ChatLiveCursor | None = None
    reconnects = 0
    gc.collect()
    tracemalloc.start()
    baseline_bytes, _ = tracemalloc.get_traced_memory()
    started = time.perf_counter()
    next_reconnect = started + 60
    for index in range(event_count):
        target = started + (
            (index + 1) / LIVE_RUN_DATASET_SPEC.batched_events_per_second
        )
        await asyncio.sleep(max(0.0, target - time.perf_counter()))
        published = await hub.publish(
            path,
            kind="block_append",
            payload={"delta": "x", "index": index},
        )
        received = await subscription.queue.get()
        if received != published:
            raise AssertionError("wall-clock subscriber lost an event")
        last_cursor = published.cursor
        now = time.perf_counter()
        if now >= next_reconnect and index + 1 < event_count:
            await hub.unsubscribe(subscription)
            subscription = await hub.subscribe(
                path.conversation_id,
                last_cursor,
            )
            if subscription.sync.resync_reason is not None:
                raise AssertionError("wall-clock reconnect required resync")
            reconnects += 1
            next_reconnect += 60
            print(
                json.dumps(
                    {
                        "elapsed_seconds": now - started,
                        "events": index + 1,
                        "reconnects": reconnects,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
    retained_bytes, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    snapshot = await hub.resource_snapshot(path.conversation_id)
    if snapshot is None:
        raise AssertionError("wall-clock live state disappeared")
    if snapshot.replay_events > LIVE_REPLAY_MAX_EVENTS:
        raise AssertionError("wall-clock replay count exceeded its limit")
    if snapshot.replay_bytes > LIVE_REPLAY_MAX_BYTES:
        raise AssertionError("wall-clock replay bytes exceeded their limit")
    return {
        "simulated_duration_seconds": duration_seconds,
        "event_count": event_count,
        "execution_seconds": time.perf_counter() - started,
        "reconnects": reconnects,
        "replay_events": snapshot.replay_events,
        "replay_bytes": snapshot.replay_bytes,
        "subscriber_count": snapshot.subscribers,
        "python_traced_retained_delta_bytes": (
            retained_bytes - baseline_bytes
        ),
        "python_traced_peak_delta_bytes": peak_bytes - baseline_bytes,
        "correctness": "passed",
    }


async def _concurrent_runs() -> dict[str, Any]:
    hub = ChatLiveHub(subscriber_max_events=2)
    paths: list[ChatLivePath] = []
    fast = []
    slow = []
    for index in range(CONCURRENT_DATASET_SPEC.active_chats):
        path = ChatLivePath(
            conversation_id=f"concurrent-{index}",
            run_id=f"run-{index}",
            turn_id=f"turn-{index}",
        )
        paths.append(path)
        await hub.start_run(path, {})
        fast.append(await hub.subscribe(path.conversation_id))
        slow.append(await hub.subscribe(path.conversation_id))
    started = time.perf_counter()
    for sequence in range(5):
        for path, subscription in zip(paths, fast):
            await hub.publish(
                path,
                kind="block_append",
                payload={"delta": str(sequence)},
            )
            event = await subscription.queue.get()
            if event is None or event.cursor.event_sequence != sequence + 1:
                raise AssertionError("fast subscriber lost ordering")
    overflowed = 0
    for subscription in slow:
        event = await subscription.queue.get()
        if event is None or event.kind != "resync_required":
            raise AssertionError("slow subscriber was not isolated")
        if await subscription.queue.get() is not None:
            raise AssertionError("overflowed subscriber stayed connected")
        overflowed += 1
    return {
        "active_chats": len(paths),
        "subscribers_per_run": CONCURRENT_DATASET_SPEC.subscribers_per_run,
        "published_events_per_chat": 5,
        "overflowed_slow_subscribers": overflowed,
        "execution_seconds": time.perf_counter() - started,
        "correctness": "passed",
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    wall_clock_seconds = int(args.wall_clock_seconds)
    long_run = (
        await _wall_clock_long_run(wall_clock_seconds)
        if wall_clock_seconds
        else await _long_run_volume()
    )
    return {
        "schema_version": 1,
        "recorded_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(),
        ),
        "git_commit": args.git_commit,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "method": (
            "wall-clock paced live/reconnect soak"
            if wall_clock_seconds
            else "accelerated event-volume test; not a two-hour wall-clock soak"
        ),
        "long_run": long_run,
        "concurrent": await _concurrent_runs(),
    }


def main() -> int:
    """Run the frozen workloads and save raw measurements."""
    args = _parse_args()
    payload = asyncio.run(_run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
