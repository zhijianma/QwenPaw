# -*- coding: utf-8 -*-
"""Application orchestration for recoverable Chat deletion."""

from __future__ import annotations

import logging
import time
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from pydantic import ValidationError

from ...checkpoints.runtime import RUNTIME as CHECKPOINT_RUNTIME
from ...utils.io_utils import get_path_lock, run_sync_io
from .models import ChatSpec
from .repo.json_repo import JsonChatRepository
from .session import session_relative_paths
from .storage import (
    DELETE_CHECKPOINTS,
    DELETE_LEGACY,
    DELETE_RUNTIME_STOPPED,
    ChatDeletionTarget,
    begin_conversation_deletion,
    list_pending_deletions,
    purge_conversation_storage,
    record_deletion_step,
)

if TYPE_CHECKING:
    from .manager import ChatManager


logger = logging.getLogger(__name__)


def _now_us() -> int:
    return time.time_ns() // 1_000


def _target_spec(target: ChatDeletionTarget) -> ChatSpec:
    try:
        return ChatSpec.model_validate_json(target.spec_json)
    except ValidationError as exc:
        raise RuntimeError(
            f"Deletion job lost Chat identity: {target.conversation_id}",
        ) from exc


async def _delete_legacy_session_files(
    workspace: Path,
    spec: ChatSpec,
    manager: "ChatManager",
) -> bool:
    remaining = await manager.list_chats(archived=None)
    shared = any(
        chat.session_id == spec.session_id
        and chat.user_id == spec.user_id
        and chat.channel == spec.channel
        for chat in remaining
    )
    if shared:
        return False
    sessions_root = workspace / "sessions"
    for relative in session_relative_paths(
        spec.session_id,
        spec.user_id,
        spec.channel,
    ):
        pure = PurePosixPath(relative)
        path = sessions_root.joinpath(*pure.parts)
        if not path.is_relative_to(sessions_root):
            raise RuntimeError("Legacy session path escaped its root")
        async with get_path_lock(path):
            await run_sync_io(path.unlink, missing_ok=True)
    return True


async def _delete_legacy_metadata(
    workspace: Path,
    spec: ChatSpec,
    manager: "ChatManager",
) -> None:
    legacy_path = workspace / "chats.json"
    if legacy_path.is_file():
        legacy = JsonChatRepository(legacy_path)
        await legacy.delete_chats([spec.id])
    owns_session = await _delete_legacy_session_files(
        workspace,
        spec,
        manager,
    )
    if owns_session:
        await manager.close_session_resources(spec.session_id)


async def _resume_target(
    workspace,
    manager: "ChatManager",
    target: ChatDeletionTarget,
) -> None:
    application = getattr(workspace, "chat_application", None)
    if application is None:
        raise RuntimeError("Chat V2 deletion requires its application")
    spec = _target_spec(target)
    completed = target.completed_steps
    if not completed & DELETE_RUNTIME_STOPPED:
        await workspace.task_tracker.request_stop(
            target.conversation_id,
            reason="chat_deleted",
        )
        await workspace.task_tracker.chat_live_hub.invalidate_conversation(
            target.conversation_id,
        )
        target = await run_sync_io(
            record_deletion_step,
            application.paths,
            target.conversation_id,
            DELETE_RUNTIME_STOPPED,
            now_us=_now_us(),
        )
        completed = target.completed_steps
    if not completed & DELETE_CHECKPOINTS:
        await CHECKPOINT_RUNTIME.delete_session_checkpoints(
            workspace,
            [(spec.session_id, spec.user_id, spec.channel)],
        )
        target = await run_sync_io(
            record_deletion_step,
            application.paths,
            target.conversation_id,
            DELETE_CHECKPOINTS,
            now_us=_now_us(),
        )
        completed = target.completed_steps
    if not completed & DELETE_LEGACY:
        await _delete_legacy_metadata(
            Path(workspace.workspace_dir),
            spec,
            manager,
        )
        await run_sync_io(
            record_deletion_step,
            application.paths,
            target.conversation_id,
            DELETE_LEGACY,
            now_us=_now_us(),
        )
    await run_sync_io(
        purge_conversation_storage,
        application.paths,
        target.conversation_id,
        now_us=_now_us(),
    )


async def delete_chat_records(
    workspace,
    manager: "ChatManager",
    specs: list[ChatSpec],
) -> bool:
    """Delete Chat records and resume safely after any completed step."""
    application = getattr(workspace, "chat_application", None)
    if application is None:
        deleted = await manager.delete_chats([spec.id for spec in specs])
        if deleted and getattr(workspace, "workspace_dir", None) is not None:
            await CHECKPOINT_RUNTIME.delete_session_checkpoints(
                workspace,
                [
                    (spec.session_id, spec.user_id, spec.channel)
                    for spec in specs
                ],
            )
        return deleted
    targets: list[ChatDeletionTarget] = []
    for spec in specs:
        target = await run_sync_io(
            begin_conversation_deletion,
            application.paths,
            spec.id,
            spec_json=spec.model_dump_json(),
            now_us=_now_us(),
        )
        if target is not None:
            targets.append(target)
    for target in targets:
        await _resume_target(workspace, manager, target)
    return bool(targets)


async def resume_pending_chat_deletions(
    workspace,
    manager: "ChatManager",
) -> int:
    """Resume durable deletion jobs during workspace initialization."""
    application = getattr(workspace, "chat_application", None)
    if application is None:
        return 0
    targets = await run_sync_io(
        list_pending_deletions,
        application.paths,
    )
    completed = 0
    for target in targets:
        try:
            await _resume_target(workspace, manager, target)
        except Exception:  # pylint: disable=broad-except
            logger.exception(
                "Could not resume Chat deletion: %s",
                target.conversation_id,
            )
        else:
            completed += 1
    return completed
