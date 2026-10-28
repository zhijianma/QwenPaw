# -*- coding: utf-8 -*-
"""Catalog ownership and lifecycle repository tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from qwenpaw.app.chats.storage import (
    CatalogEntryNotFoundError,
    CatalogRepository,
    CatalogStateConflictError,
    ChatStoragePaths,
)


CONVERSATION_ID = "11111111-1111-4111-8111-111111111111"
RELATIVE_PATH = f"conversations/{CONVERSATION_ID}.db"


def _repository(tmp_path: Path) -> CatalogRepository:
    paths = ChatStoragePaths(tmp_path)
    paths.ensure_layout()
    return CatalogRepository(paths.catalog)


def _reserve(repository: CatalogRepository) -> None:
    repository.reserve_conversation(
        conversation_id=CONVERSATION_ID,
        agent_id="agent",
        owner_scope="owner",
        display_name="Original",
        physical_relpath=RELATIVE_PATH,
        now_us=1,
    )


def test_catalog_reservation_is_idempotent_only_for_same_identity(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)

    _reserve(repository)
    _reserve(repository)

    entry = repository.get(CONVERSATION_ID)
    assert entry is not None
    assert entry.lifecycle == "creating"
    with pytest.raises(CatalogStateConflictError, match="already exists"):
        repository.reserve_conversation(
            conversation_id=CONVERSATION_ID,
            agent_id="other",
            owner_scope="owner",
            display_name="Other",
            physical_relpath=RELATIVE_PATH,
            now_us=2,
        )


def test_catalog_presentation_updates_are_ready_only_and_versioned(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _reserve(repository)
    with pytest.raises(CatalogStateConflictError, match="not ready"):
        repository.rename(CONVERSATION_ID, "Blocked", now_us=2)

    ready = repository.mark_ready(CONVERSATION_ID, now_us=3)
    assert ready.lifecycle == "ready"
    repository.create_group(
        group_id="group",
        agent_id="agent",
        name="Group",
        ordinal=0,
        kind="custom",
        now_us=4,
    )
    repository.rename(CONVERSATION_ID, "Renamed", now_us=5)
    repository.set_pinned(CONVERSATION_ID, True, now_us=6)
    repository.set_archived_at(CONVERSATION_ID, 7, now_us=7)
    updated = repository.set_group(CONVERSATION_ID, "group", now_us=8)

    assert updated.display_name == "Renamed"
    assert updated.pinned is True
    assert updated.archived_at_us == 7
    assert updated.group_id == "group"
    assert updated.catalog_generation == 4
    assert updated.source_generation == 0


def test_catalog_group_foreign_key_rejects_unknown_group(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _reserve(repository)
    repository.mark_ready(CONVERSATION_ID, now_us=2)

    with pytest.raises(sqlite3.IntegrityError):
        repository.set_group(CONVERSATION_ID, "missing", now_us=3)
    entry = repository.get(CONVERSATION_ID)
    assert entry is not None
    assert entry.group_id is None
    assert entry.catalog_generation == 0


def test_catalog_rejects_invalid_transition_and_missing_entry(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    with pytest.raises(CatalogEntryNotFoundError):
        repository.mark_ready(CONVERSATION_ID, now_us=1)
    with pytest.raises(CatalogEntryNotFoundError):
        repository.set_pinned(CONVERSATION_ID, True, now_us=1)

    _reserve(repository)
    repository.mark_ready(CONVERSATION_ID, now_us=2)
    assert repository.mark_ready(CONVERSATION_ID, now_us=3).updated_at_us == 2
    with pytest.raises(ValueError, match="blank"):
        repository.rename(CONVERSATION_ID, "  ", now_us=4)
