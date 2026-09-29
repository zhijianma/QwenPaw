# -*- coding: utf-8 -*-
"""Tests for durable provider-scoped Memory state."""

import asyncio
from pathlib import Path

import pytest

from qwenpaw.kernel import MemoryStateConflictError, MemoryStateScope
from qwenpaw.runtime.memory_state import SQLiteMemoryStateStore


def _store(
    database_path: Path,
    *,
    provider_id: str = "example.memory.provider",
    owner_id: str = "chat-1",
) -> SQLiteMemoryStateStore:
    return SQLiteMemoryStateStore(
        database_path,
        provider_id=provider_id,
        scope=MemoryStateScope.CONVERSATION,
        owner_id=owner_id,
    )


@pytest.mark.asyncio
async def test_memory_state_survives_store_reopen(tmp_path: Path) -> None:
    database_path = tmp_path / "memory.sqlite3"
    first = _store(database_path)

    created = await first.write(
        "summary",
        {"text": "stable", "items": [1, 2]},
        expected_revision=0,
    )
    restored = await _store(database_path).read("summary")

    assert created.revision == 1
    assert restored is not None
    assert restored.key == "summary"
    assert restored.value == {"items": [1, 2], "text": "stable"}
    assert restored.owner_id == "chat-1"


@pytest.mark.asyncio
async def test_memory_state_compare_and_swap_allows_one_writer(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "memory.sqlite3"
    left = _store(database_path)
    right = _store(database_path)

    results = await asyncio.gather(
        left.write("cursor", 1, expected_revision=0),
        right.write("cursor", 2, expected_revision=0),
        return_exceptions=True,
    )

    assert sum(not isinstance(item, BaseException) for item in results) == 1
    conflicts = [
        item for item in results if isinstance(item, MemoryStateConflictError)
    ]
    assert len(conflicts) == 1
    current = await left.read("cursor")
    assert current is not None
    assert current.revision == 1
    assert current.value in (1, 2)


@pytest.mark.asyncio
async def test_memory_state_isolates_provider_and_owner(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "memory.sqlite3"
    source = _store(database_path)
    other_provider = _store(
        database_path,
        provider_id="other.memory.provider",
    )
    other_owner = _store(database_path, owner_id="chat-2")

    await source.write("shared-key", "source", expected_revision=0)

    assert await other_provider.read("shared-key") is None
    assert await other_owner.read("shared-key") is None


@pytest.mark.asyncio
async def test_memory_state_update_and_delete_require_current_revision(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "memory.sqlite3")
    created = await store.write("summary", "one", expected_revision=0)
    updated = await store.write(
        "summary",
        "two",
        expected_revision=created.revision,
    )

    with pytest.raises(MemoryStateConflictError):
        await store.delete("summary", expected_revision=created.revision)

    await store.delete("summary", expected_revision=updated.revision)
    assert await store.read("summary") is None


@pytest.mark.asyncio
async def test_memory_state_rejects_non_json_and_oversized_values(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "memory.sqlite3")

    with pytest.raises(ValueError, match="must be JSON"):
        await store.write("invalid", {1, 2}, expected_revision=0)
    with pytest.raises(ValueError, match="256 KiB"):
        await store.write(
            "oversized",
            "x" * (256 * 1024),
            expected_revision=0,
        )
