# -*- coding: utf-8 -*-
"""Feature gates for the normalized Chat persistence rollout."""

from __future__ import annotations

import os
from pathlib import Path

from .storage import ChatStoragePaths
from .storage.common import connect_database


CHAT_STORAGE_V2_ENV = "QWENPAW_CHAT_STORAGE_V2"
CHAT_V2_UI_ENV = "QWENPAW_CHAT_V2_UI"


class ChatStorageRollbackBlockedError(RuntimeError):
    """Raised when legacy routing would split a ready V2 workspace."""


def _enabled(name: str, *, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def chat_storage_v2_enabled() -> bool:
    """Return whether this process participates in the V2 Chat rollout."""
    return _enabled(CHAT_STORAGE_V2_ENV, default="1")


def chat_v2_ui_enabled() -> bool:
    """Return whether paginated/live/Copy UI APIs are exposed."""
    return _enabled(CHAT_V2_UI_ENV, default="1")


def assert_chat_storage_route_safe(workspace: Path) -> None:
    """Fail closed when an operator disables V2 after irreversible cutover."""
    if chat_storage_v2_enabled():
        return
    catalog_path = ChatStoragePaths(workspace).catalog
    if not catalog_path.is_file():
        return
    try:
        with connect_database(catalog_path, readonly=True) as connection:
            ready = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM chat_catalog_entries "
                "WHERE lifecycle = 'ready') OR EXISTS(SELECT 1 FROM "
                "chat_migrations WHERE state = 'ready_v2')",
            ).fetchone()[0]
    except Exception as exc:
        raise ChatStorageRollbackBlockedError(
            "Cannot verify normalized Chat rollback safety; keep "
            "QWENPAW_CHAT_STORAGE_V2 enabled",
        ) from exc
    if ready:
        raise ChatStorageRollbackBlockedError(
            "Normalized Chat cutover is irreversible; keep "
            "QWENPAW_CHAT_STORAGE_V2 enabled and disable only "
            "QWENPAW_CHAT_V2_UI",
        )


__all__ = [
    "CHAT_STORAGE_V2_ENV",
    "CHAT_V2_UI_ENV",
    "ChatStorageRollbackBlockedError",
    "assert_chat_storage_route_safe",
    "chat_storage_v2_enabled",
    "chat_v2_ui_enabled",
]
