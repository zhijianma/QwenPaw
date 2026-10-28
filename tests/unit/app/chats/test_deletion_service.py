# -*- coding: utf-8 -*-
"""End-to-end deletion orchestration tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from qwenpaw.app.chats.application import ChatApplicationService
from qwenpaw.app.chats.deletion_service import (
    delete_chat_records,
    resume_pending_chat_deletions,
)
from qwenpaw.app.chats.manager import ChatManager
from qwenpaw.app.chats.models import ChatSpec
from qwenpaw.app.chats.repo.catalog_repo import CatalogChatRepository
from qwenpaw.app.chats.repo.json_repo import JsonChatRepository
from qwenpaw.app.chats.session import session_relative_paths
from qwenpaw.app.chats.storage import (
    begin_conversation_deletion,
    list_pending_deletions,
)
from qwenpaw.app.chats.storage.common import connect_database
from qwenpaw.app.task_tracker import TaskTracker


async def _context(tmp_path):
    workspace_dir = tmp_path / "workspace"
    application = ChatApplicationService(workspace_dir)
    repository = CatalogChatRepository(
        workspace_dir,
        agent_id="agent",
        backend="qwenpaw",
    )
    manager = ChatManager(repo=repository)
    spec = ChatSpec(
        id=str(uuid4()),
        session_id="console:deletion",
        user_id="user",
        channel="console",
    )
    await repository.upsert_chat(spec)
    legacy = JsonChatRepository(workspace_dir / "chats.json")
    await legacy.upsert_chat(spec)
    for relative in session_relative_paths(
        spec.session_id,
        spec.user_id,
        spec.channel,
    ):
        path = workspace_dir / "sessions" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    attachment = workspace_dir / "attachments" / "keep.bin"
    attachment.parent.mkdir()
    attachment.write_bytes(b"keep")
    workspace = SimpleNamespace(
        workspace_dir=workspace_dir,
        chat_application=application,
        chat_manager=manager,
        task_tracker=TaskTracker(),
    )
    return workspace, manager, spec, legacy, attachment


@pytest.mark.asyncio
async def test_delete_removes_runtime_and_legacy_but_keeps_attachment(
    tmp_path,
) -> None:
    workspace, manager, spec, legacy, attachment = await _context(tmp_path)

    with patch(
        "qwenpaw.app.chats.deletion_service.CHECKPOINT_RUNTIME."
        "delete_session_checkpoints",
        new=AsyncMock(return_value=()),
    ) as delete_checkpoints:
        assert await delete_chat_records(workspace, manager, [spec])

    assert await manager.get_chat(spec.id) is None
    assert await legacy.get_chat(spec.id) is None
    assert not workspace.chat_application.paths.conversation(spec.id).exists()
    assert not any((workspace.workspace_dir / "sessions").rglob("*.json"))
    assert attachment.read_bytes() == b"keep"
    delete_checkpoints.assert_awaited_once_with(
        workspace,
        [(spec.session_id, spec.user_id, spec.channel)],
    )
    with connect_database(
        workspace.chat_application.paths.catalog,
        readonly=True,
    ) as connection:
        lifecycle = connection.execute(
            "SELECT lifecycle FROM chat_catalog_entries "
            "WHERE conversation_id = ?",
            (spec.id,),
        ).fetchone()[0]
    assert lifecycle == "deleted"


@pytest.mark.asyncio
async def test_startup_resumes_interrupted_deletion(tmp_path) -> None:
    workspace, manager, spec, legacy, _attachment = await _context(tmp_path)
    begin_conversation_deletion(
        workspace.chat_application.paths,
        spec.id,
        spec_json=spec.model_dump_json(),
        now_us=1,
    )
    assert len(list_pending_deletions(workspace.chat_application.paths)) == 1

    with patch(
        "qwenpaw.app.chats.deletion_service.CHECKPOINT_RUNTIME."
        "delete_session_checkpoints",
        new=AsyncMock(return_value=()),
    ):
        assert await resume_pending_chat_deletions(workspace, manager) == 1

    assert not list_pending_deletions(workspace.chat_application.paths)
    assert await legacy.get_chat(spec.id) is None
    assert not workspace.chat_application.paths.conversation(spec.id).exists()
