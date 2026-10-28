# -*- coding: utf-8 -*-
"""Recoverable cross-database Chat creation tests."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from qwenpaw.app.chats.storage import (
    CONVERSATION_SCHEMA_VERSION,
    CatalogEntry,
    CatalogRepository,
    ChatStorageCoordinator,
    ChatStoragePaths,
    ChatStorageSchemaError,
    ConversationCreateRequest,
    initialize_conversation_database,
    initialize_recall_database,
    validate_conversation_database,
)
from qwenpaw.app.chats.storage.common import connect_database


CONVERSATION_ID = "11111111-1111-4111-8111-111111111111"


def _request(now_us: int = 1) -> ConversationCreateRequest:
    return ConversationCreateRequest(
        conversation_id=CONVERSATION_ID,
        agent_id="agent",
        owner_scope="owner",
        display_name="Chat",
        backend="qwenpaw",
        now_us=now_us,
    )


def test_create_conversation_publishes_validated_database(
    tmp_path: Path,
) -> None:
    paths = ChatStoragePaths(tmp_path)
    coordinator = ChatStorageCoordinator(paths)

    entry = coordinator.create_conversation(_request())

    target = paths.conversation(CONVERSATION_ID)
    assert entry.lifecycle == "ready"
    assert entry.physical_relpath == f"conversations/{CONVERSATION_ID}.db"
    assert validate_conversation_database(target) == (
        CONVERSATION_SCHEMA_VERSION
    )
    assert not target.with_name(f".{target.name}.creating").exists()
    assert not target.with_name(f"{target.name}-wal").exists()
    assert not target.with_name(f"{target.name}-shm").exists()


def test_current_storage_contract_contains_no_branch_schema(
    tmp_path: Path,
) -> None:
    paths = ChatStoragePaths(tmp_path)
    ChatStorageCoordinator(paths).create_conversation(_request())
    initialize_recall_database(paths.recall)

    for path in (
        paths.catalog,
        paths.conversation(CONVERSATION_ID),
        paths.recall,
    ):
        with connect_database(path, readonly=True) as connection:
            schema = "\n".join(
                str(row[0] or "")
                for row in connection.execute(
                    "SELECT sql FROM sqlite_schema ORDER BY name",
                )
            ).lower()
        assert "chat_branches" not in schema
        assert "branch_id" not in schema
        assert "parent_branch_id" not in schema


def test_create_conversation_is_idempotent_after_publish(
    tmp_path: Path,
) -> None:
    paths = ChatStoragePaths(tmp_path)
    coordinator = ChatStorageCoordinator(paths)

    coordinator.create_conversation(_request())
    second = coordinator.create_conversation(_request(now_us=2))

    assert second.lifecycle == "ready"
    assert second.created_at_us == 1


def test_create_recovers_when_catalog_publish_failed_after_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = ChatStoragePaths(tmp_path)
    catalog = CatalogRepository(paths.catalog)
    coordinator = ChatStorageCoordinator(paths, catalog=catalog)
    original = catalog.mark_ready
    calls = 0

    def fail_once(conversation_id: str, *, now_us: int) -> CatalogEntry:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected publish failure")
        return original(conversation_id, now_us=now_us)

    monkeypatch.setattr(catalog, "mark_ready", fail_once)
    with pytest.raises(RuntimeError, match="injected"):
        coordinator.create_conversation(_request())

    assert paths.conversation(CONVERSATION_ID).is_file()
    pending = catalog.get(CONVERSATION_ID)
    assert pending is not None
    assert pending.lifecycle == "creating"
    recovered = coordinator.create_conversation(_request(now_us=2))
    assert recovered.lifecycle == "ready"


def test_create_resumes_a_valid_temporary_database(tmp_path: Path) -> None:
    paths = ChatStoragePaths(tmp_path)
    paths.ensure_layout()
    target = paths.conversation(CONVERSATION_ID)
    temporary = target.with_name(f".{target.name}.creating")
    initialize_conversation_database(
        temporary,
        conversation_id=CONVERSATION_ID,
        agent_id="agent",
        owner_scope="owner",
        backend="qwenpaw",
        created_at_us=1,
    )

    entry = ChatStorageCoordinator(paths).create_conversation(
        _request(now_us=2),
    )
    assert entry.lifecycle == "ready"
    assert target.is_file()
    assert not temporary.exists()


def test_create_rejects_existing_database_with_wrong_backend(
    tmp_path: Path,
) -> None:
    paths = ChatStoragePaths(tmp_path)
    coordinator = ChatStorageCoordinator(paths)
    coordinator.create_conversation(_request())

    wrong = replace(_request(now_us=2), backend="other")
    with pytest.raises(ChatStorageSchemaError, match="backend"):
        coordinator.create_conversation(wrong)
