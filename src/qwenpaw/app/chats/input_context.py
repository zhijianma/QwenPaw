# -*- coding: utf-8 -*-
"""Transport-neutral preparation of durable Chat input context."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ...services.project_directory import (
    normalize_project_dir_list,
    session_project_dirs_raw_from_meta,
)

logger = logging.getLogger(__name__)


# pylint: disable-next=too-many-return-statements
async def persist_pending_project_dirs(
    workspace: Any,
    chat: Any,
    native_payload: dict[str, Any],
) -> Any:
    """Validate and bind first-turn project directories to one ChatSpec."""
    meta = native_payload.get("meta")
    if not isinstance(meta, dict):
        return chat
    request_context = meta.get("request_context")
    if not isinstance(request_context, dict):
        return chat

    raw_list = request_context.pop("session_project_dirs", None)
    raw_single = request_context.pop("session_project_dir", None)

    def leave_for_runtime() -> None:
        if raw_list is not None:
            request_context["session_project_dirs"] = raw_list
        if raw_single is not None:
            request_context["session_project_dir"] = raw_single

    pending: list[Any] | None = None
    if isinstance(raw_list, list) and raw_list:
        pending = raw_list
    elif isinstance(raw_single, str) and raw_single.strip():
        pending = [raw_single]
    if pending is None:
        return chat
    if session_project_dirs_raw_from_meta(getattr(chat, "meta", None)):
        return chat

    def validate() -> list[dict[str, str | None]]:
        entries: list[dict[str, str | None]] = []
        for path, label in normalize_project_dir_list(pending):
            if not path.is_dir():
                logger.warning(
                    "Ignoring pending project dir that is not a "
                    "directory: %s",
                    path,
                )
                continue
            entries.append({"path": str(path), "label": label})
        return entries

    entries = await asyncio.to_thread(validate)
    if not entries:
        return chat
    updated = await workspace.chat_manager.set_session_project_dirs(
        chat.id,
        entries,
    )
    if updated is None:
        leave_for_runtime()
        return chat
    return updated


__all__ = ["persist_pending_project_dirs"]
