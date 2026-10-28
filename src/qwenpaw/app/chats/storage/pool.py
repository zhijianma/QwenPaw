# -*- coding: utf-8 -*-
"""Bounded per-conversation connection cache and serialization locks."""

from __future__ import annotations

import asyncio
import sqlite3
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator

from .common import connect_database
from .paths import ChatStoragePaths


@dataclass
class _ConnectionEntry:
    """One cached connection and its same-process transaction fence."""

    connection: sqlite3.Connection
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class ConversationConnectionPool:
    """Keep a small LRU of SQLite connections without evicting active users."""

    def __init__(
        self,
        paths: ChatStoragePaths,
        *,
        max_connections: int = 16,
    ) -> None:
        if max_connections < 1:
            raise ValueError("max_connections must be positive")
        self._paths = paths
        self._max_connections = max_connections
        self._entries: OrderedDict[str, _ConnectionEntry] = OrderedDict()
        self._guard = asyncio.Lock()
        self._closed = False

    @property
    def cached_count(self) -> int:
        """Return the current connection count for metrics and tests."""
        return len(self._entries)

    @asynccontextmanager
    async def connection(
        self,
        conversation_id: str,
    ) -> AsyncIterator[sqlite3.Connection]:
        """Lease one serialized connection for a short database operation."""
        entry = await self._reserve(conversation_id)
        await entry.lock.acquire()
        try:
            yield entry.connection
        finally:
            entry.lock.release()
            await self._release(conversation_id, entry)

    async def invalidate(self, conversation_id: str) -> None:
        """Close one idle connection before checkpoint restore or deletion."""
        async with self._guard:
            entry = self._entries.get(conversation_id)
            if entry is None:
                return
            entry.users += 1
        await entry.lock.acquire()
        try:
            async with self._guard:
                current = self._entries.get(conversation_id)
                if current is entry:
                    self._entries.pop(conversation_id)
                    entry.connection.close()
        finally:
            entry.lock.release()
            async with self._guard:
                entry.users -= 1

    async def close(self) -> None:
        """Reject new leases, wait for current leases, then close all."""
        async with self._guard:
            if self._closed:
                return
            self._closed = True
            entries = list(self._entries.items())
            for _, entry in entries:
                entry.users += 1
        for conversation_id, entry in entries:
            await entry.lock.acquire()
            try:
                async with self._guard:
                    current = self._entries.get(conversation_id)
                    if current is entry:
                        self._entries.pop(conversation_id)
                        entry.connection.close()
            finally:
                entry.lock.release()
                async with self._guard:
                    entry.users -= 1

    async def _reserve(self, conversation_id: str) -> _ConnectionEntry:
        async with self._guard:
            if self._closed:
                raise RuntimeError("Conversation connection pool is closed")
            entry = self._entries.get(conversation_id)
            if entry is None:
                path = self._paths.conversation(conversation_id)
                if not path.is_file():
                    raise FileNotFoundError(path)
                entry = _ConnectionEntry(connect_database(path))
                self._entries[conversation_id] = entry
            entry.users += 1
            self._entries.move_to_end(conversation_id)
            self._evict_idle()
            return entry

    async def _release(
        self,
        conversation_id: str,
        entry: _ConnectionEntry,
    ) -> None:
        async with self._guard:
            entry.users -= 1
            current = self._entries.get(conversation_id)
            if current is entry:
                self._entries.move_to_end(conversation_id)
            self._evict_idle()

    def _evict_idle(self) -> None:
        """Evict oldest idle entries; active concurrency may exceed the cap."""
        while len(self._entries) > self._max_connections:
            victim_id = next(
                (
                    conversation_id
                    for conversation_id, entry in self._entries.items()
                    if entry.users == 0 and not entry.lock.locked()
                ),
                None,
            )
            if victim_id is None:
                return
            victim = self._entries.pop(victim_id)
            victim.connection.close()
