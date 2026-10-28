# -*- coding: utf-8 -*-
"""Application backup coverage for normalized Chat storage."""

# pylint: disable=protected-access

from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from qwenpaw.app.chats.storage import (
    CHAT_STORAGE_MANIFEST,
    RECALL_SCHEMA_VERSION,
    ChatStorageCoordinator,
    ChatStoragePaths,
    ConversationCreateRequest,
    initialize_recall_database,
    validate_recall_database,
)
from qwenpaw.backup._ops import restore
from qwenpaw.backup._ops.create_helpers import add_agent_workspaces
from qwenpaw.backup.models import BackupValidationError


def _workspace(tmp_path: Path) -> tuple[Path, str, ChatStoragePaths]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "agent.json").write_text("{}", encoding="utf-8")
    paths = ChatStoragePaths(workspace)
    conversation_id = str(uuid4())
    ChatStorageCoordinator(paths).create_conversation(
        ConversationCreateRequest(
            conversation_id=conversation_id,
            agent_id="agent",
            owner_scope="owner",
            display_name="Chat",
            backend="qwenpaw",
            now_us=1,
        ),
    )
    initialize_recall_database(paths.recall)
    return workspace, conversation_id, paths


def _stage(
    archive: Path,
    destination: Path,
) -> Path:
    with zipfile.ZipFile(archive, "r") as zf:
        restore._stage_agents(
            zf,
            ["agent"],
            {"agent"},
            {"agent": (destination, True)},
            [],
            {},
            [],
        )
    return destination.with_name(f"{destination.name}.restore_tmp")


def test_backup_snapshots_chat_databases_and_rebuilds_recall(
    tmp_path: Path,
) -> None:
    workspace, conversation_id, paths = _workspace(tmp_path)
    connection = sqlite3.connect(paths.conversation(conversation_id))
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            "UPDATE chat_meta SET source_generation = 5 "
            "WHERE singleton_id = 1",
        )
        connection.commit()
        assert Path(f"{paths.conversation(conversation_id)}-wal").exists()
        archive = tmp_path / "backup.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            assert add_agent_workspaces(
                zf,
                [
                    (
                        "agent",
                        SimpleNamespace(workspace_dir=workspace),
                    ),
                ],
            )
    finally:
        connection.close()

    prefix = "data/workspaces/agent/.qwenpaw/chat/"
    with zipfile.ZipFile(archive, "r") as zf:
        names = set(zf.namelist())
        assert f"{prefix}chat_catalog.db" in names
        assert f"{prefix}conversations/{conversation_id}.db" in names
        assert f"{prefix}{CHAT_STORAGE_MANIFEST}" in names
        assert f"{prefix}chat_recall.db" not in names
        assert not any(name.endswith(("-wal", "-shm")) for name in names)

    staged = _stage(archive, tmp_path / "restored")
    staged_paths = ChatStoragePaths(staged)
    assert staged_paths.conversation(conversation_id).is_file()
    assert not (staged_paths.root / CHAT_STORAGE_MANIFEST).exists()
    assert (
        validate_recall_database(staged_paths.recall) == RECALL_SCHEMA_VERSION
    )


def test_restore_rejects_tampered_chat_artifact(
    tmp_path: Path,
) -> None:
    workspace, conversation_id, _paths = _workspace(tmp_path)
    source_archive = tmp_path / "source.zip"
    with zipfile.ZipFile(source_archive, "w") as zf:
        add_agent_workspaces(
            zf,
            [("agent", SimpleNamespace(workspace_dir=workspace))],
        )
    tampered_archive = tmp_path / "tampered.zip"
    target = (
        "data/workspaces/agent/.qwenpaw/chat/conversations/"
        f"{conversation_id}.db"
    )
    with zipfile.ZipFile(source_archive, "r") as source:
        with zipfile.ZipFile(tampered_archive, "w") as destination:
            for info in source.infolist():
                payload = source.read(info.filename)
                if info.filename == target:
                    payload += b"tampered"
                destination.writestr(info, payload)

    with pytest.raises(BackupValidationError) as error:
        _stage(tampered_archive, tmp_path / "restored")

    assert error.value.code == "chat_storage_invalid"


def test_restore_accepts_pre_manifest_chat_archive(
    tmp_path: Path,
) -> None:
    _workspace_path, conversation_id, paths = _workspace(tmp_path)
    archive = tmp_path / "legacy.zip"
    prefix = "data/workspaces/agent/"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(f"{prefix}agent.json", json.dumps({}))
        zf.write(
            paths.catalog,
            f"{prefix}.qwenpaw/chat/chat_catalog.db",
        )
        zf.write(
            paths.conversation(conversation_id),
            f"{prefix}.qwenpaw/chat/conversations/{conversation_id}.db",
        )

    staged = _stage(archive, tmp_path / "restored")

    assert ChatStoragePaths(staged).conversation(conversation_id).is_file()
