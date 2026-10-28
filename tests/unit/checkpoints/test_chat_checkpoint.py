# -*- coding: utf-8 -*-
"""Normalized Chat checkpoint snapshot and restore coverage."""

from __future__ import annotations

import json
import hashlib
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from qwenpaw.app.chats.application import ChatApplicationService
from qwenpaw.app.chats.storage import (
    AcceptanceRequest,
    ChatStorageCoordinator,
    ChatStoragePaths,
    ConversationCreateRequest,
    accept_input,
    interrupt_nonterminal_work,
    project_conversation,
)
from qwenpaw.app.chats.storage.common import (
    connect_database,
    immediate_transaction,
)
from qwenpaw.app.task_tracker import TaskTracker
from qwenpaw.checkpoints.chat_artifact import (
    CHAT_ARTIFACT_DATABASE,
    CHAT_ARTIFACT_MANIFEST,
)
from qwenpaw.checkpoints.models import CheckpointError
from qwenpaw.checkpoints.policy import session_file_path, session_key
from qwenpaw.checkpoints.service import CheckpointService
from qwenpaw.schemas import Message, Role, RunStatus, TextContent


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="checkpoint tests require git",
)

CONVERSATION_ID = "22222222-2222-4222-8222-222222222222"
SESSION_ID = "checkpoint-chat"
USER_ID = "user"
CHANNEL = "console"


class _ActiveMaterializer:
    conversation_id = CONVERSATION_ID

    def __init__(self) -> None:
        self.flush_partial_blocks = AsyncMock()


def _write_legacy_session(workspace: Path) -> None:
    path = session_file_path(
        workspace,
        session_id=SESSION_ID,
        user_id=USER_ID,
        channel=CHANNEL,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "agent": {
                    "state": {
                        "context": [
                            {
                                "id": "legacy",
                                "name": "user",
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": "legacy"},
                                ],
                            },
                        ],
                    },
                },
            },
        ),
        encoding="utf-8",
    )


def _create_normalized_chat(
    workspace: Path,
) -> tuple[ChatStoragePaths, str]:
    paths = ChatStoragePaths(workspace)
    coordinator = ChatStorageCoordinator(paths)
    coordinator.create_conversation(
        ConversationCreateRequest(
            conversation_id=CONVERSATION_ID,
            agent_id="agent",
            owner_scope=f"{CHANNEL}:{USER_ID}",
            display_name="Original name",
            backend="qwenpaw",
            now_us=1,
        ),
    )
    with connect_database(paths.conversation(CONVERSATION_ID)) as source:
        with immediate_transaction(source):
            source.execute(
                "INSERT INTO chat_aliases("
                "alias_namespace, external_key, owner_scope, "
                "created_at_us, updated_at_us) VALUES "
                "('legacy_session', ?, ?, 2, 2)",
                (SESSION_ID, f"{CHANNEL}:{USER_ID}"),
            )
    head_turn_id = _append_turn(paths, "request-1", 10, 3)
    project_conversation(paths, CONVERSATION_ID, now_us=2)
    return paths, head_turn_id


def _append_turn(
    paths: ChatStoragePaths,
    request_id: str,
    recall_seq: int,
    now_us: int,
) -> str:
    result = accept_input(
        paths.conversation(CONVERSATION_ID),
        AcceptanceRequest(
            conversation_id=CONVERSATION_ID,
            client_request_id=request_id,
            message=Message(
                role=Role.USER,
                content=[TextContent(text=request_id).completed()],
                status=RunStatus.Completed,
            ),
            process_instance_id="checkpoint-test",
            now_us=now_us,
        ),
        recall_sequences=(recall_seq,),
    )
    interrupt_nonterminal_work(
        paths.conversation(CONVERSATION_ID),
        conversation_id=CONVERSATION_ID,
        now_us=now_us + 1,
    )
    assert result.turn_id is not None
    return result.turn_id


def _service(workspace: Path) -> CheckpointService:
    application = ChatApplicationService(workspace)
    service = CheckpointService(workspace)
    service.workspace = SimpleNamespace(
        agent_id="agent",
        chat_application=application,
        task_tracker=TaskTracker(),
    )
    return service


@pytest.mark.asyncio
async def test_snapshot_and_restore_preserve_entire_chat(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_legacy_session(workspace)
    paths, snapshot_head = _create_normalized_chat(workspace)
    service = _service(workspace)
    other_id = str(uuid4())
    ChatStorageCoordinator(paths).create_conversation(
        ConversationCreateRequest(
            conversation_id=other_id,
            agent_id="agent",
            owner_scope=f"{CHANNEL}:{USER_ID}",
            display_name="Other",
            backend="qwenpaw",
            now_us=1,
        ),
    )
    other_hash = hashlib.sha256(
        paths.conversation(other_id).read_bytes(),
    ).hexdigest()

    snapshot = await service.make_snapshot_result(
        kind="snap",
        session_id=SESSION_ID,
        user_id=USER_ID,
        channel=CHANNEL,
        name="single chat timeline",
    )
    other_session_key = session_key(
        channel=CHANNEL,
        user_id=USER_ID,
        session_id="other-session",
    )
    service.repository.run_git(
        "update-ref",
        f"refs/snap/{other_session_key}/keep",
        snapshot.commit,
    )
    service.repository.set_session_head(
        other_session_key,
        snapshot.commit,
    )

    assert service.repository.tree_has_blob(
        snapshot.commit,
        CHAT_ARTIFACT_DATABASE,
    )
    assert service.repository.tree_has_blob(
        snapshot.commit,
        CHAT_ARTIFACT_MANIFEST,
    )

    mutated_head = _append_turn(paths, "request-2", 11, 9)
    with connect_database(paths.catalog) as catalog:
        with immediate_transaction(catalog):
            catalog.execute(
                "UPDATE chat_catalog_entries SET display_name = 'Renamed', "
                "pinned = 1, source_generation = 9 "
                "WHERE conversation_id = ?",
                (CONVERSATION_ID,),
            )
    active_materializer = _ActiveMaterializer()
    service.workspace.chat_application.register_materializer(
        active_materializer,
    )

    preview = await service.restore(
        target=snapshot.commit,
        session_id=SESSION_ID,
        user_id=USER_ID,
        channel=CHANNEL,
        dry_run=True,
    )
    assert preview.conversation_format == "normalized_chat_v2"
    assert preview.head_turn_id == snapshot_head
    assert preview.view_invalidated is False
    active_materializer.flush_partial_blocks.assert_not_awaited()
    with connect_database(
        paths.conversation(CONVERSATION_ID),
        readonly=True,
    ) as source:
        live_head = source.execute(
            "SELECT head_turn_id FROM chat_meta WHERE singleton_id = 1",
        ).fetchone()[0]
        assert str(live_head) == mutated_head

    restored = await service.restore(
        target=snapshot.commit,
        session_id=SESSION_ID,
        user_id=USER_ID,
        channel=CHANNEL,
    )

    assert restored.pre_restore_ref is not None
    assert restored.view_invalidated is True
    with connect_database(
        paths.conversation(CONVERSATION_ID),
        readonly=True,
    ) as source:
        meta = source.execute(
            "SELECT head_turn_id, source_generation FROM chat_meta "
            "WHERE singleton_id = 1",
        ).fetchone()
        turns = source.execute(
            "SELECT COUNT(*) FROM chat_turns",
        ).fetchone()[0]
    assert turns == 1
    assert str(meta["head_turn_id"]) == snapshot_head
    assert int(meta["source_generation"]) > 9
    with connect_database(paths.catalog, readonly=True) as catalog:
        entry = catalog.execute(
            "SELECT display_name, pinned, source_generation "
            "FROM chat_catalog_entries WHERE conversation_id = ?",
            (CONVERSATION_ID,),
        ).fetchone()
    assert tuple(entry) == (
        "Renamed",
        1,
        int(meta["source_generation"]),
    )
    assert (
        hashlib.sha256(
            paths.conversation(other_id).read_bytes(),
        ).hexdigest()
        == other_hash
    )
    assert service.session_head(other_session_key) == snapshot.commit


@pytest.mark.asyncio
async def test_corrupt_chat_artifact_is_rejected_without_mutation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_legacy_session(workspace)
    paths, _snapshot_head = _create_normalized_chat(workspace)
    service = _service(workspace)
    snapshot = await service.make_snapshot_result(
        kind="snap",
        session_id=SESSION_ID,
        user_id=USER_ID,
        channel=CHANNEL,
        name="valid",
    )
    original = paths.conversation(CONVERSATION_ID).read_bytes()
    manifest = service.repository.read_blob(
        snapshot.commit,
        CHAT_ARTIFACT_MANIFEST,
    )
    tree = service.repository.write_workspace_tree()
    bad_blob = tmp_path / "bad.db"
    bad_blob.write_bytes(b"not sqlite")
    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_bytes(manifest)
    bad_tree = service.repository.write_workspace_tree(
        extra_files={
            CHAT_ARTIFACT_DATABASE: bad_blob,
            CHAT_ARTIFACT_MANIFEST: manifest_file,
        },
    )
    assert tree != bad_tree
    bad_commit = service.repository.run_git(
        "commit-tree",
        bad_tree,
        input_text="corrupt artifact\n",
    )
    key = session_key(
        channel=CHANNEL,
        user_id=USER_ID,
        session_id=SESSION_ID,
    )
    service.repository.run_git(
        "update-ref",
        f"refs/snap/{key}/corrupt",
        bad_commit,
    )

    with pytest.raises(
        CheckpointError,
        match="size mismatch|hash mismatch",
    ):
        await service.restore(
            target="corrupt",
            session_id=SESSION_ID,
            user_id=USER_ID,
            channel=CHANNEL,
            dry_run=True,
        )
    assert paths.conversation(CONVERSATION_ID).read_bytes() == original


@pytest.mark.asyncio
async def test_legacy_checkpoint_is_upgraded_during_restore(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_legacy_session(workspace)
    paths, _snapshot_head = _create_normalized_chat(workspace)
    service = _service(workspace)
    legacy = service.create_snapshot_unlocked(
        "snap",
        SESSION_ID,
        USER_ID,
        CHANNEL,
        "legacy only",
        "legacy only",
        None,
    )
    assert not service.repository.tree_has_blob(
        legacy.commit,
        CHAT_ARTIFACT_DATABASE,
    )

    preview = await service.restore(
        target=legacy.commit,
        session_id=SESSION_ID,
        user_id=USER_ID,
        channel=CHANNEL,
        dry_run=True,
    )
    assert preview.conversation_format == "normalized_chat_v2"
    assert preview.head_turn_id is not None

    await service.restore(
        target=legacy.commit,
        session_id=SESSION_ID,
        user_id=USER_ID,
        channel=CHANNEL,
    )
    with connect_database(
        paths.conversation(CONVERSATION_ID),
        readonly=True,
    ) as source:
        assert (
            source.execute(
                "SELECT COUNT(*) FROM chat_turns",
            ).fetchone()[0]
            == 1
        )
        assert (
            source.execute(
                "SELECT COUNT(*) FROM chat_blocks "
                "WHERE recall_seq IS NOT NULL",
            ).fetchone()[0]
            == 1
        )


@pytest.mark.asyncio
async def test_failed_restore_rolls_back_normalized_chat(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_legacy_session(workspace)
    paths, snapshot_head = _create_normalized_chat(workspace)
    service = _service(workspace)
    snapshot = await service.make_snapshot_result(
        kind="snap",
        session_id=SESSION_ID,
        user_id=USER_ID,
        channel=CHANNEL,
        name="rollback target",
    )
    mutated_head = _append_turn(paths, "request-2", 11, 12)
    assert mutated_head != snapshot_head
    original_set_head = service.repository.set_session_head
    failed = False

    def fail_once(key: str, commit: str) -> None:
        nonlocal failed
        if commit == snapshot.commit and not failed:
            failed = True
            raise RuntimeError("injected head failure")
        original_set_head(key, commit)

    monkeypatch.setattr(service.repository, "set_session_head", fail_once)

    with pytest.raises(RuntimeError, match="injected head failure"):
        await service.restore(
            target=snapshot.commit,
            session_id=SESSION_ID,
            user_id=USER_ID,
            channel=CHANNEL,
        )

    with connect_database(
        paths.conversation(CONVERSATION_ID),
        readonly=True,
    ) as source:
        meta = source.execute(
            "SELECT head_turn_id FROM chat_meta WHERE singleton_id = 1",
        ).fetchone()
        turns = source.execute(
            "SELECT COUNT(*) FROM chat_turns",
        ).fetchone()[0]
    assert str(meta["head_turn_id"]) == mutated_head
    assert turns == 2
