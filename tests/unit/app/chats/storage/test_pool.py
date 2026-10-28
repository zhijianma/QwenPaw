# -*- coding: utf-8 -*-
"""Bounded Chat connection pool tests."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from qwenpaw.app.chats.storage import (
    ChatStoragePaths,
    ConversationConnectionPool,
    initialize_conversation_database,
)


IDS = (
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
    "33333333-3333-4333-8333-333333333333",
)


def _initialize(paths: ChatStoragePaths, conversation_id: str) -> None:
    initialize_conversation_database(
        paths.conversation(conversation_id),
        conversation_id=conversation_id,
        agent_id="agent",
        owner_scope="owner",
        backend="qwenpaw",
        created_at_us=1,
    )


@pytest.mark.asyncio
async def test_pool_serializes_same_conversation_operations(
    tmp_path: Path,
) -> None:
    paths = ChatStoragePaths(tmp_path)
    paths.ensure_layout()
    _initialize(paths, IDS[0])
    pool = ConversationConnectionPool(paths, max_connections=1)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []

    async def first() -> None:
        async with pool.connection(IDS[0]):
            order.append("first-enter")
            first_entered.set()
            await release_first.wait()
            order.append("first-exit")

    async def second() -> None:
        await first_entered.wait()
        async with pool.connection(IDS[0]):
            order.append("second-enter")

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(second())
    await first_entered.wait()
    await asyncio.sleep(0)
    assert order == ["first-enter"]
    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert order == ["first-enter", "first-exit", "second-enter"]
    await pool.close()


@pytest.mark.asyncio
async def test_pool_evicts_only_idle_least_recent_connection(
    tmp_path: Path,
) -> None:
    paths = ChatStoragePaths(tmp_path)
    paths.ensure_layout()
    for conversation_id in IDS:
        _initialize(paths, conversation_id)
    pool = ConversationConnectionPool(paths, max_connections=2)

    async with pool.connection(IDS[0]) as first_connection:
        assert first_connection.execute("SELECT 1").fetchone()[0] == 1
    async with pool.connection(IDS[1]):
        pass
    async with pool.connection(IDS[2]):
        pass

    assert pool.cached_count == 2
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        first_connection.execute("SELECT 1")
    await pool.close()


@pytest.mark.asyncio
async def test_pool_allows_active_overflow_then_returns_to_limit(
    tmp_path: Path,
) -> None:
    paths = ChatStoragePaths(tmp_path)
    paths.ensure_layout()
    for conversation_id in IDS:
        _initialize(paths, conversation_id)
    pool = ConversationConnectionPool(paths, max_connections=1)

    async with pool.connection(IDS[0]):
        async with pool.connection(IDS[1]):
            assert pool.cached_count == 2
        assert pool.cached_count == 1
    assert pool.cached_count == 1
    await pool.close()


@pytest.mark.asyncio
async def test_pool_invalidate_waits_for_active_lease(tmp_path: Path) -> None:
    paths = ChatStoragePaths(tmp_path)
    paths.ensure_layout()
    _initialize(paths, IDS[0])
    pool = ConversationConnectionPool(paths, max_connections=1)
    invalidated = asyncio.Event()

    async with pool.connection(IDS[0]) as connection:

        async def invalidate() -> None:
            await pool.invalidate(IDS[0])
            invalidated.set()

        task = asyncio.create_task(invalidate())
        await asyncio.sleep(0)
        assert not invalidated.is_set()
        assert connection.execute("SELECT 1").fetchone()[0] == 1
    await task
    assert invalidated.is_set()
    assert pool.cached_count == 0
    await pool.close()


@pytest.mark.asyncio
async def test_pool_rejects_invalid_and_missing_conversations(
    tmp_path: Path,
) -> None:
    paths = ChatStoragePaths(tmp_path)
    paths.ensure_layout()
    pool = ConversationConnectionPool(paths)

    with pytest.raises(ValueError):
        async with pool.connection("../escape"):
            pass
    with pytest.raises(FileNotFoundError):
        async with pool.connection(IDS[0]):
            pass
    await pool.close()
    with pytest.raises(RuntimeError, match="closed"):
        async with pool.connection(IDS[0]):
            pass
