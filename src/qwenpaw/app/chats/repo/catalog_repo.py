# -*- coding: utf-8 -*-
"""ChatManager repository backed by the normalized workspace catalog."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..models import ChatGroup, ChatSpec, ChatsFile
from ..storage import (
    CatalogStateConflictError,
    ChatStorageCoordinator,
    ChatStoragePaths,
    ConversationCreateRequest,
    enqueue_projection_repair,
    project_conversation,
)
from ..storage.common import connect_database, immediate_transaction
from .base import BaseChatRepository


logger = logging.getLogger(__name__)


def _to_us(value: datetime) -> int:
    return int(value.timestamp() * 1_000_000)


def _from_us(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1_000_000, tz=timezone.utc)


class CatalogChatRepository(BaseChatRepository):
    """Preserve ChatManager behavior without writing legacy chats.json."""

    def __init__(
        self,
        workspace: Path,
        *,
        agent_id: str,
        backend: str,
    ) -> None:
        self._paths = ChatStoragePaths(workspace)
        self._paths.ensure_layout()
        self._agent_id = agent_id
        self._backend = backend
        self._coordinator = ChatStorageCoordinator(self._paths)
        self._write_lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        """Return the catalog file for existing ChatManager diagnostics."""
        return self._paths.catalog

    def _load_sync(self) -> ChatsFile:
        with connect_database(
            self._paths.catalog,
            readonly=True,
        ) as connection:
            group_rows = connection.execute(
                "SELECT group_id, name, ordinal, kind, pinned "
                "FROM chat_groups WHERE agent_id = ? ORDER BY ordinal",
                (self._agent_id,),
            ).fetchall()
            chat_rows = connection.execute(
                "SELECT spec_json, display_name, pinned, archived_at_us, "
                "group_id, run_state, created_at_us, updated_at_us "
                "FROM chat_catalog_entries WHERE agent_id = ? "
                "AND lifecycle IN ('creating', 'ready', 'quarantined') "
                "ORDER BY updated_at_us DESC",
                (self._agent_id,),
            ).fetchall()
        groups = [
            ChatGroup(
                id=str(row["group_id"]),
                name=str(row["name"]),
                order=int(row["ordinal"]),
                kind=str(row["kind"]),
                pinned=bool(row["pinned"]),
            )
            for row in group_rows
        ]
        chats: list[ChatSpec] = []
        for row in chat_rows:
            raw = str(row["spec_json"] or "{}")
            if raw == "{}":
                continue
            chat = ChatSpec.model_validate_json(raw)
            updates: dict[str, Any] = {
                "name": str(row["display_name"]),
                "pinned": bool(row["pinned"]),
                "group_id": row["group_id"],
                "status": (
                    "running"
                    if row["run_state"] in {"accepted", "running", "stopping"}
                    else "idle"
                ),
                "created_at": _from_us(int(row["created_at_us"])),
                "updated_at": _from_us(int(row["updated_at_us"])),
            }
            if row["archived_at_us"] is not None:
                updates["archived_at"] = _from_us(
                    int(row["archived_at_us"]),
                )
            else:
                updates["archived_at"] = None
            chats.append(chat.model_copy(update=updates))
        return ChatsFile(chats=chats, groups=groups)

    async def load(self) -> ChatsFile:
        """Load catalog metadata off the event loop."""
        return await asyncio.to_thread(self._load_sync)

    def _ensure_conversation(self, chat: ChatSpec, now_us: int) -> None:
        with connect_database(
            self._paths.catalog,
            readonly=True,
        ) as connection:
            exists = connection.execute(
                "SELECT 1 FROM chat_catalog_entries WHERE conversation_id = ?",
                (chat.id,),
            ).fetchone()
        if exists is not None:
            return
        self._coordinator.create_conversation(
            ConversationCreateRequest(
                conversation_id=chat.id,
                agent_id=self._agent_id,
                owner_scope=f"{chat.channel}:{chat.user_id}",
                display_name=chat.name,
                backend=self._backend,
                now_us=now_us,
            ),
        )

    def _ensure_source_alias(self, chat: ChatSpec, now_us: int) -> bool:
        path = self._paths.conversation(chat.id)
        # Legacy Chats remain catalog-only until their lazy migration barrier.
        # The migration writes the same alias into the source database.
        if not path.is_file():
            return False
        owner_scope = f"{chat.channel}:{chat.user_id}"
        with connect_database(path) as connection:
            with immediate_transaction(connection):
                meta = connection.execute(
                    "SELECT source_generation, write_seq "
                    "FROM chat_meta WHERE singleton_id = 1",
                ).fetchone()
                if meta is None:
                    raise RuntimeError(
                        f"Chat source has no metadata: {chat.id}",
                    )
                existing = connection.execute(
                    "SELECT 1 FROM chat_aliases WHERE "
                    "alias_namespace = 'legacy_session' "
                    "AND external_key = ? AND owner_scope = ?",
                    (chat.session_id, owner_scope),
                ).fetchone()
                if existing is not None:
                    return False
                generation = int(meta["source_generation"]) + 1
                write_seq = int(meta["write_seq"]) + 1
                connection.execute(
                    "INSERT INTO chat_aliases("
                    "alias_namespace, external_key, owner_scope, "
                    "created_at_us, updated_at_us) VALUES "
                    "('legacy_session', ?, ?, ?, ?) "
                    "ON CONFLICT(alias_namespace, external_key, owner_scope) "
                    "DO UPDATE SET updated_at_us = excluded.updated_at_us",
                    (
                        chat.session_id,
                        owner_scope,
                        now_us,
                        now_us,
                    ),
                )
                connection.execute(
                    "UPDATE chat_meta SET source_generation = ?, "
                    "write_seq = ?, updated_at_us = ? "
                    "WHERE singleton_id = 1",
                    (generation, write_seq, now_us),
                )
                connection.execute(
                    "INSERT INTO chat_projection_outbox("
                    "projection, operation, source_generation, "
                    "payload_json, created_at_us, updated_at_us) "
                    "VALUES ('catalog', 'alias_upserted', ?, ?, ?, ?)",
                    (
                        generation,
                        json.dumps(
                            {"conversation_id": chat.id},
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        now_us,
                        now_us,
                    ),
                )
        return True

    def _save_sync(self, chats_file: ChatsFile) -> None:
        now_us = int(datetime.now(timezone.utc).timestamp() * 1_000_000)
        for chat in chats_file.chats:
            self._ensure_conversation(chat, now_us)
        with connect_database(self._paths.catalog) as connection:
            with immediate_transaction(connection):
                existing_group_ids = {
                    str(row["group_id"])
                    for row in connection.execute(
                        "SELECT group_id FROM chat_groups WHERE agent_id = ?",
                        (self._agent_id,),
                    )
                }
                connection.execute(
                    "UPDATE chat_groups SET ordinal = ordinal + 1000000 "
                    "WHERE agent_id = ?",
                    (self._agent_id,),
                )
                saved_group_ids: set[str] = set()
                for ordinal, group in enumerate(chats_file.groups):
                    saved_group_ids.add(group.id)
                    connection.execute(
                        "INSERT INTO chat_groups("
                        "group_id, agent_id, name, ordinal, kind, pinned, "
                        "created_at_us, updated_at_us) VALUES (?, ?, ?, ?, "
                        "?, ?, ?, ?) ON CONFLICT(group_id) DO UPDATE SET "
                        "name = excluded.name, ordinal = excluded.ordinal, "
                        "kind = excluded.kind, pinned = excluded.pinned, "
                        "updated_at_us = excluded.updated_at_us",
                        (
                            group.id,
                            self._agent_id,
                            group.name,
                            ordinal,
                            group.kind.value,
                            int(group.pinned),
                            now_us,
                            now_us,
                        ),
                    )
                removed_groups = existing_group_ids - saved_group_ids
                for group_id in removed_groups:
                    connection.execute(
                        "DELETE FROM chat_groups WHERE group_id = ? "
                        "AND agent_id = ?",
                        (group_id, self._agent_id),
                    )

                saved_ids: set[str] = set()
                for chat in chats_file.chats:
                    saved_ids.add(chat.id)
                    connection.execute(
                        "UPDATE chat_catalog_entries SET owner_scope = ?, "
                        "display_name = ?, spec_json = ?, pinned = ?, "
                        "archived_at_us = ?, group_id = ?, updated_at_us = ? "
                        "WHERE conversation_id = ? AND agent_id = ?",
                        (
                            f"{chat.channel}:{chat.user_id}",
                            chat.name,
                            chat.model_dump_json(),
                            int(chat.pinned),
                            (
                                _to_us(chat.archived_at)
                                if chat.archived_at is not None
                                else None
                            ),
                            chat.group_id,
                            _to_us(chat.updated_at),
                            chat.id,
                            self._agent_id,
                        ),
                    )
                    conflict = connection.execute(
                        "SELECT conversation_id FROM chat_alias_index "
                        "WHERE agent_id = ? AND alias_namespace = "
                        "'legacy_session' AND external_key = ? "
                        "AND owner_scope = ?",
                        (
                            self._agent_id,
                            chat.session_id,
                            f"{chat.channel}:{chat.user_id}",
                        ),
                    ).fetchone()
                    if (
                        conflict is not None
                        and str(conflict["conversation_id"]) != chat.id
                    ):
                        raise CatalogStateConflictError(
                            "Chat alias is already owned by another "
                            f"conversation: {chat.session_id}",
                        )
                    connection.execute(
                        "INSERT INTO chat_alias_index("
                        "agent_id, alias_namespace, external_key, "
                        "owner_scope, conversation_id, "
                        "created_at_us, updated_at_us) VALUES (?, "
                        "'legacy_session', ?, ?, ?, ?, ?) "
                        "ON CONFLICT(agent_id, alias_namespace, external_key, "
                        "owner_scope) DO UPDATE SET updated_at_us = "
                        "excluded.updated_at_us",
                        (
                            self._agent_id,
                            chat.session_id,
                            f"{chat.channel}:{chat.user_id}",
                            chat.id,
                            now_us,
                            now_us,
                        ),
                    )
                rows = connection.execute(
                    "SELECT conversation_id FROM chat_catalog_entries "
                    "WHERE agent_id = ? AND lifecycle != 'deleted'",
                    (self._agent_id,),
                ).fetchall()
                for row in rows:
                    conversation_id = str(row["conversation_id"])
                    if conversation_id not in saved_ids:
                        connection.execute(
                            "UPDATE chat_catalog_entries SET "
                            "lifecycle = 'deleted', updated_at_us = ? "
                            "WHERE conversation_id = ?",
                            (now_us, conversation_id),
                        )
        changed_aliases = [
            chat.id
            for chat in chats_file.chats
            if self._ensure_source_alias(chat, now_us)
        ]
        for conversation_id in changed_aliases:
            try:
                project_conversation(
                    self._paths,
                    conversation_id,
                    now_us=now_us,
                )
            except Exception:
                try:
                    with connect_database(
                        self._paths.conversation(conversation_id),
                        readonly=True,
                    ) as connection:
                        generation = int(
                            connection.execute(
                                "SELECT source_generation FROM chat_meta "
                                "WHERE singleton_id = 1",
                            ).fetchone()[0],
                        )
                    enqueue_projection_repair(
                        self._paths,
                        conversation_id,
                        expected_generation=generation,
                        now_us=now_us,
                    )
                except Exception:
                    logger.exception(
                        "Chat alias projection repair could not be queued: "
                        "%s",
                        conversation_id,
                    )
                logger.warning(
                    "Chat alias projection deferred: %s",
                    conversation_id,
                    exc_info=True,
                )

    async def save(self, chats_file: ChatsFile) -> None:
        """Persist all metadata in catalog without mutating chats.json."""
        async with self._write_lock:
            await asyncio.to_thread(self._save_sync, chats_file)


__all__ = ["CatalogChatRepository"]
