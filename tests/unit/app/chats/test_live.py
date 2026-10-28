# -*- coding: utf-8 -*-
"""Realtime Chat hub ordering and bounded-resource tests."""

from __future__ import annotations

import asyncio

import pytest

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


def _path(run_id: str = "run-1") -> ChatLivePath:
    return ChatLivePath(
        conversation_id="chat-1",
        run_id=run_id,
        turn_id="turn-1",
    )


@pytest.mark.asyncio
async def test_subscribe_registers_before_sync_and_receives_after_barrier():
    hub = ChatLiveHub()
    path = _path()
    await hub.start_run(path, {"blocks": ("stable",)})

    subscription = await hub.subscribe(path.conversation_id)
    published = await hub.publish(
        path,
        kind="block_append",
        payload={"delta": "next"},
        view={"blocks": ("stable", "next")},
        entity_id="block-1",
        entity_version=2,
        expected_offset=6,
    )

    assert subscription.sync.cursor is not None
    assert subscription.sync.cursor.event_sequence == 0
    assert subscription.sync.view == {"blocks": ("stable",)}
    assert await subscription.queue.get() == published


@pytest.mark.asyncio
async def test_slow_subscriber_isolated_without_blocking_fast_subscriber():
    hub = ChatLiveHub(subscriber_max_events=2, subscriber_max_bytes=4096)
    path = _path()
    await hub.start_run(path, {})
    slow = await hub.subscribe(path.conversation_id)
    fast = await hub.subscribe(path.conversation_id)
    fast_events = []

    async def consume_fast():
        for _ in range(5):
            fast_events.append(await fast.queue.get())

    consumer = asyncio.create_task(consume_fast())
    for index in range(5):
        await hub.publish(
            path,
            kind="block_append",
            payload={"delta": str(index)},
        )
        await asyncio.sleep(0)
    await asyncio.wait_for(consumer, timeout=1)

    overflow = await slow.queue.get()
    assert overflow is not None
    assert overflow.kind == "resync_required"
    assert await slow.queue.get() is None
    assert [event.cursor.event_sequence for event in fast_events] == [
        1,
        2,
        3,
        4,
        5,
    ]


@pytest.mark.asyncio
async def test_replay_and_subscriber_resources_have_dual_limits():
    hub = ChatLiveHub(
        replay_max_events=2,
        replay_max_bytes=900,
        subscriber_max_events=10,
        subscriber_max_bytes=900,
    )
    path = _path()
    await hub.start_run(path, {})
    subscription = await hub.subscribe(path.conversation_id)
    for index in range(5):
        await hub.publish(
            path,
            kind="block_append",
            payload={"delta": "x" * 200, "index": index},
        )
        await subscription.queue.get()

    key = path.conversation_id
    run = hub._runs[key]  # pylint: disable=protected-access
    assert len(run.replay) <= 2
    assert run.replay_bytes <= 900
    assert subscription.queue.event_count == 0
    assert subscription.queue.event_bytes == 0
    snapshot = await hub.resource_snapshot(
        path.conversation_id,
    )
    assert snapshot is not None
    assert snapshot.replay_events <= 2
    assert snapshot.replay_bytes <= 900
    assert snapshot.subscribers == 1


@pytest.mark.asyncio
async def test_cursor_gap_reasons_and_terminal_cleanup():
    hub = ChatLiveHub(replay_max_events=2, terminal_retention_s=10)
    path = _path()
    epoch = await hub.start_run(path, {})
    for index in range(3):
        await hub.publish(path, kind="entity_state", payload={"index": index})

    old = await hub.subscribe(
        path.conversation_id,
        ChatLiveCursor(epoch.run_epoch, 0),
    )
    assert old.sync.resync_reason == "cursor_evicted"
    wrong_epoch = await hub.subscribe(
        path.conversation_id,
        ChatLiveCursor("previous-process", 1),
    )
    assert wrong_epoch.sync.resync_reason == "epoch_lost"

    terminal = await hub.publish(
        path,
        kind="terminal",
        payload={"state": "completed"},
        terminal=True,
    )
    assert await old.queue.get() == terminal
    assert await old.queue.get() is None
    terminal_sync = await hub.subscribe(
        path.conversation_id,
    )
    assert terminal_sync.sync.terminal is True
    assert terminal_sync.sync.cursor == terminal.cursor
    assert await terminal_sync.queue.get() is None

    key = path.conversation_id
    run = hub._runs[key]  # pylint: disable=protected-access
    assert run.terminal_at is not None
    assert await hub.cleanup_expired(run.terminal_at + 10) == 1
    lost = await hub.subscribe(path.conversation_id)
    assert lost.sync.resync_reason == "epoch_lost"


@pytest.mark.asyncio
async def test_live_allows_only_one_active_run_per_chat():
    hub = ChatLiveHub()
    first = _path("run-1")
    second = ChatLivePath(
        conversation_id=first.conversation_id,
        run_id="run-2",
        turn_id="turn-2",
    )
    await hub.start_run(first, {"state": "first"})
    with pytest.raises(RuntimeError, match="active run"):
        await hub.start_run(second, {"state": "second"})


@pytest.mark.asyncio
async def test_terminal_summary_expires_without_external_cleanup():
    hub = ChatLiveHub(terminal_retention_s=0.01)
    path = _path()
    await hub.start_run(path, {})
    await hub.publish(
        path,
        kind="terminal",
        payload={"state": "completed"},
        terminal=True,
    )
    with pytest.raises(RuntimeError, match="not active"):
        await hub.publish(
            path,
            kind="block_append",
            payload={"text": "late"},
        )

    await asyncio.sleep(0.03)

    expired = await hub.subscribe(path.conversation_id)
    assert expired.sync.resync_reason == "epoch_lost"


@pytest.mark.asyncio
async def test_delete_invalidates_one_chat_and_closes_its_subscribers():
    hub = ChatLiveHub()
    first = _path("run-1")
    second = ChatLivePath(
        conversation_id="chat-2",
        run_id="run-2",
        turn_id="turn-2",
    )
    await hub.start_run(first, {})
    await hub.start_run(second, {})
    first_subscription = await hub.subscribe(
        first.conversation_id,
    )
    second_subscription = await hub.subscribe(
        second.conversation_id,
    )

    assert await hub.invalidate_conversation(first.conversation_id) == 1

    assert await first_subscription.queue.get() is None
    assert second_subscription.queue.empty()
    lost = await hub.subscribe(first.conversation_id)
    assert lost.sync.resync_reason == "epoch_lost"


@pytest.mark.asyncio
async def test_two_hour_event_volume_keeps_replay_storage_bounded():
    """Accelerate the frozen two-hour event volume without wall-clock sleep."""
    hub = ChatLiveHub()
    path = _path("long-run")
    await hub.start_run(path, {})

    for index in range(LIVE_RUN_DATASET_SPEC.expected_event_count):
        await hub.publish(
            path,
            kind="block_append",
            payload={"delta": "x", "index": index},
        )

    key = path.conversation_id
    run = hub._runs[key]  # pylint: disable=protected-access
    assert run.next_sequence == LIVE_RUN_DATASET_SPEC.expected_event_count + 1
    assert len(run.replay) <= LIVE_REPLAY_MAX_EVENTS
    assert run.replay_bytes <= LIVE_REPLAY_MAX_BYTES
    assert run.subscribers == []


@pytest.mark.asyncio
async def test_frozen_concurrent_runs_isolate_slow_subscribers():
    hub = ChatLiveHub(subscriber_max_events=2)
    fast_subscriptions = []
    slow_subscriptions = []
    paths = []
    for index in range(CONCURRENT_DATASET_SPEC.active_chats):
        path = ChatLivePath(
            conversation_id=f"chat-{index}",
            run_id=f"run-{index}",
            turn_id=f"turn-{index}",
        )
        paths.append(path)
        await hub.start_run(path, {})
        fast_subscriptions.append(
            await hub.subscribe(path.conversation_id),
        )
        slow_subscriptions.append(
            await hub.subscribe(path.conversation_id),
        )

    for sequence in range(5):
        for path, fast in zip(paths, fast_subscriptions):
            await hub.publish(
                path,
                kind="block_append",
                payload={"delta": str(sequence)},
            )
            event = await fast.queue.get()
            assert event is not None
            assert event.cursor.event_sequence == sequence + 1

    for subscription in slow_subscriptions:
        overflow = await subscription.queue.get()
        assert overflow is not None
        assert overflow.kind == "resync_required"
        assert await subscription.queue.get() is None
