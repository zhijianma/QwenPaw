# -*- coding: utf-8 -*-
"""Tests for normalized Chat backup artifacts and manifests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from qwenpaw.app.chats.storage import (
    CHAT_STORAGE_MANIFEST,
    ChatStorageCoordinator,
    ChatStoragePaths,
    ChatStorageSchemaError,
    ConversationCreateRequest,
    is_live_chat_sqlite_file,
    snapshot_workspace_chat_storage,
    validate_workspace_chat_storage,
)


def _create_conversation(workspace: Path) -> tuple[str, ChatStoragePaths]:
    paths = ChatStoragePaths(workspace)
    conversation_id = str(uuid4())
    coordinator = ChatStorageCoordinator(paths)
    coordinator.create_conversation(
        ConversationCreateRequest(
            conversation_id=conversation_id,
            agent_id="agent",
            owner_scope="owner",
            display_name="Chat",
            backend="qwenpaw",
            now_us=1,
        ),
    )
    return conversation_id, paths


def test_snapshot_captures_wal_and_writes_verified_manifest(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    destination = tmp_path / "snapshot"
    conversation_id, paths = _create_conversation(workspace)
    connection = sqlite3.connect(paths.conversation(conversation_id))
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            "UPDATE chat_meta SET source_generation = 7 "
            "WHERE singleton_id = 1",
        )
        connection.commit()
        assert Path(f"{paths.conversation(conversation_id)}-wal").exists()
        snapshot = snapshot_workspace_chat_storage(
            workspace,
            destination,
        )
    finally:
        connection.close()

    assert snapshot is not None
    assert snapshot.conversation_ids == (conversation_id,)
    snapshot_paths = ChatStoragePaths(destination)
    assert snapshot_paths.catalog.is_file()
    assert snapshot_paths.conversation(conversation_id).is_file()
    assert not Path(
        f"{snapshot_paths.conversation(conversation_id)}-wal",
    ).exists()
    manifest = json.loads(snapshot.manifest_path.read_text(encoding="utf-8"))
    conversation = next(
        record
        for record in manifest["files"]
        if record["kind"] == "conversation"
    )
    assert conversation["conversation_id"] == conversation_id
    assert conversation["source_generation"] == 7
    assert validate_workspace_chat_storage(destination) == snapshot


def test_validation_rejects_tampered_conversation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    destination = tmp_path / "snapshot"
    conversation_id, _paths = _create_conversation(workspace)
    snapshot_workspace_chat_storage(workspace, destination)
    artifact = ChatStoragePaths(destination).conversation(conversation_id)
    artifact.write_bytes(artifact.read_bytes() + b"tampered")

    with pytest.raises(ChatStorageSchemaError, match="size mismatch"):
        validate_workspace_chat_storage(destination)


def test_legacy_archive_without_manifest_remains_importable(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    destination = tmp_path / "snapshot"
    conversation_id, _paths = _create_conversation(workspace)
    snapshot_workspace_chat_storage(workspace, destination)
    manifest = ChatStoragePaths(destination).root / CHAT_STORAGE_MANIFEST
    manifest.unlink()

    snapshot = validate_workspace_chat_storage(
        destination,
        require_manifest=False,
    )

    assert snapshot is not None
    assert snapshot.conversation_ids == (conversation_id,)


def test_archive_traversal_skips_only_live_chat_sqlite_files(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    conversation_id, paths = _create_conversation(workspace)
    note = paths.root / "note.txt"
    note.write_text("keep", encoding="utf-8")

    assert is_live_chat_sqlite_file(workspace, paths.catalog)
    assert is_live_chat_sqlite_file(workspace, paths.recall)
    assert is_live_chat_sqlite_file(
        workspace,
        Path(f"{paths.conversation(conversation_id)}-wal"),
    )
    assert not is_live_chat_sqlite_file(workspace, note)
