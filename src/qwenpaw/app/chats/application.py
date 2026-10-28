# -*- coding: utf-8 -*-
"""Unified durable acceptance boundary for every Chat entry point."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Coroutine
from uuid import uuid4

from ...schemas import Message
from .migration import LegacyMigrationCoordinator
from .migration.inventory import (
    LegacyInventoryReport,
    inventory_legacy_workspace,
)
from .pagination_cursor import (
    ChatCursorCodec,
    load_or_create_cursor_key,
)
from .storage import (
    AcceptanceRequest,
    AcceptanceResult,
    CatalogEntryNotFoundError,
    ChatStorageSchemaError,
    ChatStoragePaths,
    ChatCopyError,
    ChatCopyResult,
    ChatRecallSearchPage,
    ChatStorageCoordinator,
    accept_input,
    begin_conversation_deletion,
    cancel_accepted_input,
    claim_queued_input,
    ConversationCopyRequest,
    enqueue_projection_repair,
    initialize_catalog_database,
    initialize_conversation_database,
    interrupt_nonterminal_work,
    inspect_copy,
    project_conversation,
    purge_conversation_storage,
    read_transcript,
    repair_pending_projections,
    reserve_recall_sequences,
    search_chat_recall,
)
from .storage.common import connect_database, immediate_transaction
from .storage.pagination_repository import (
    ChatPage,
    read_block_fragment,
    read_block_page,
    read_history_page_view,
    read_message_page,
    read_turn_page,
)


logger = logging.getLogger(__name__)
_PROCESS_MIGRATION_LIMIT = asyncio.Semaphore(2)


@dataclass(frozen=True)
class ChatAlias:
    """Workspace-scoped external routing identity."""

    agent_id: str
    namespace: str
    external_key: str
    owner_scope: str


class AcceptanceReplayError(RuntimeError):
    """A duplicate request must attach to live replay instead of re-running."""


# Application boundary intentionally exposes pagination, live, and Chat-copy
# capabilities through one owner-scoped facade.
# pylint: disable-next=too-many-public-methods
class ChatApplicationService:
    """Coordinate migration, idempotent acceptance, and source recovery."""

    def __init__(
        self,
        workspace: Path,
        *,
        process_instance_id: str | None = None,
    ) -> None:
        self._workspace = workspace
        self._paths = ChatStoragePaths(workspace)
        self._paths.ensure_layout()
        initialize_catalog_database(self._paths.catalog)
        self._cursor_codec = ChatCursorCodec(
            load_or_create_cursor_key(self._paths.root),
        )
        self._migration = LegacyMigrationCoordinator(workspace)
        self._process_instance_id = process_instance_id or uuid4().hex
        self._background_migration: asyncio.Task[None] | None = None
        self._validated_conversations: set[str] = set()
        self._page_requests: dict[
            tuple[object, ...],
            asyncio.Task[ChatPage],
        ] = {}
        self._page_requests_lock = asyncio.Lock()
        self._active_materializers: weakref.WeakSet[Any] = weakref.WeakSet()

    @property
    def paths(self) -> ChatStoragePaths:
        """Expose immutable storage paths to runtime adapters."""
        return self._paths

    @property
    def process_instance_id(self) -> str:
        """Return the owner identity used by leases and run fencing."""
        return self._process_instance_id

    def register_materializer(self, materializer: Any) -> None:
        """Expose active partial buffers to the checkpoint barrier."""
        self._active_materializers.add(materializer)

    async def flush_partial_blocks(self, conversation_id: str) -> None:
        """Force active partials for one Chat across the durable boundary."""
        targets = [
            materializer
            for materializer in tuple(self._active_materializers)
            if materializer.conversation_id == conversation_id
        ]
        if targets:
            await asyncio.gather(
                *(target.flush_partial_blocks() for target in targets),
            )

    async def initialize(
        self,
        *,
        agent_id: str,
        now_us: int | None = None,
    ) -> LegacyInventoryReport | None:
        """Inventory legacy metadata and interrupt abandoned runtime work."""
        instant = now_us if now_us is not None else time.time_ns() // 1_000
        report = None
        if (self._workspace / "chats.json").is_file():
            report = await asyncio.to_thread(
                inventory_legacy_workspace,
                self._workspace,
                agent_id=agent_id,
                now_us=instant,
            )
        await self.recover_after_restart(now_us=instant)
        return report

    def _active_migration_candidates(self, *, now_us: int) -> list[str]:
        with connect_database(self._paths.catalog) as connection:
            with immediate_transaction(connection):
                rows = connection.execute(
                    "SELECT m.conversation_id FROM chat_migrations AS m "
                    "JOIN chat_catalog_entries AS e ON e.conversation_id = "
                    "m.conversation_id WHERE e.archived_at_us IS NULL "
                    "AND e.lifecycle = 'creating' AND m.state IN "
                    "('legacy_only', 'queued', 'failed_retryable') "
                    "ORDER BY e.pinned DESC, m.priority DESC, "
                    "m.updated_at_us DESC",
                ).fetchall()
                candidates = [
                    str(row["conversation_id"])
                    for row in rows
                    if row["conversation_id"] is not None
                ]
                if candidates:
                    placeholders = ", ".join("?" for _ in candidates)
                    connection.execute(
                        "UPDATE chat_migrations SET state = 'queued', "
                        "updated_at_us = ? WHERE conversation_id IN ("
                        f"{placeholders})",
                        (now_us, *candidates),
                    )
        return candidates

    async def migrate_active_legacy(self) -> None:
        """Migrate active legacy Chats in persisted priority order."""
        await asyncio.to_thread(
            repair_pending_projections,
            self._paths,
            now_us=time.time_ns() // 1_000,
        )
        candidates = await asyncio.to_thread(
            self._active_migration_candidates,
            now_us=time.time_ns() // 1_000,
        )
        for conversation_id in candidates:
            try:
                async with _PROCESS_MIGRATION_LIMIT:
                    await self._migration.migrate(conversation_id)
            except Exception:
                logger.warning(
                    "Background Chat migration failed: %s",
                    conversation_id,
                    exc_info=True,
                )

    def start_background_migration(self) -> None:
        """Start one non-blocking active migration worker per workspace."""
        if (
            self._background_migration is None
            or self._background_migration.done()
        ):
            self._background_migration = asyncio.create_task(
                self.migrate_active_legacy(),
                name=f"chat-migration:{self._workspace.name}",
            )

    async def close(self) -> None:
        """Cancel and join the workspace migration worker."""
        task = self._background_migration
        self._background_migration = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def load_runtime_context(
        self,
        conversation_id: str,
    ) -> dict | None:
        """Load the current runtime context blob for one Chat."""
        await self.ensure_ready(conversation_id)
        path = self._paths.conversation(conversation_id)

        def _load() -> dict | None:
            with connect_database(path, readonly=True) as connection:
                row = connection.execute(
                    "SELECT rb.codec, rb.payload FROM chat_meta AS m "
                    "JOIN chat_context_revisions AS cr ON "
                    "cr.context_revision_id = "
                    "m.current_context_revision_id "
                    "JOIN chat_runtime_blobs AS rb ON rb.blob_hash = "
                    "cr.runtime_blob_hash WHERE m.singleton_id = 1",
                ).fetchone()
            if row is None or str(row["codec"]) != "agentscope-json":
                return None
            value = json.loads(bytes(row["payload"]).decode("utf-8"))
            return value if isinstance(value, dict) else None

        return await asyncio.to_thread(_load)

    async def load_transcript(
        self,
        conversation_id: str,
        *,
        backend: str = "qwenpaw",
    ) -> list[Message]:
        """Load the complete transcript for one Chat."""
        await self.ensure_ready(conversation_id, backend=backend)
        return await asyncio.to_thread(
            read_transcript,
            self._paths.conversation(conversation_id),
        )

    async def _project_change(self, conversation_id: str) -> None:
        """Project a committed Chat mutation without undoing its source."""
        instant = time.time_ns() // 1_000
        try:
            await asyncio.to_thread(
                project_conversation,
                self._paths,
                conversation_id,
                now_us=instant,
            )
        except Exception:
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
            await asyncio.to_thread(
                enqueue_projection_repair,
                self._paths,
                conversation_id,
                expected_generation=generation,
                now_us=instant,
            )
            logger.warning(
                "Chat projection deferred: %s",
                conversation_id,
                exc_info=True,
            )

    async def copy_chat(
        self,
        conversation_id: str,
        *,
        target_conversation_id: str,
        target_session_id: str,
        source_turn_id: str,
        display_name: str,
        group_id: str | None,
        backend: str = "qwenpaw",
    ) -> ChatCopyResult:
        """Copy one completed lineage into an independent Chat."""
        await self.ensure_ready(conversation_id, backend=backend)
        if backend != "qwenpaw":
            raise ChatCopyError(
                "copy_unsupported",
                "This backend does not support Chat copies",
            )
        source_path = self._paths.conversation(conversation_id)
        (
            block_count,
            agent_id,
            owner_scope,
            _source_backend,
        ) = await asyncio.to_thread(
            inspect_copy,
            source_path,
            source_turn_id=source_turn_id,
        )
        with connect_database(self._paths.catalog, readonly=True) as catalog:
            source_row = catalog.execute(
                "SELECT root_conversation_id FROM chat_catalog_entries "
                "WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        root_conversation_id = (
            str(source_row["root_conversation_id"])
            if source_row is not None
            and source_row["root_conversation_id"] is not None
            else conversation_id
        )
        instant = time.time_ns() // 1_000
        lease = await asyncio.to_thread(
            reserve_recall_sequences,
            self._paths.catalog,
            workspace_id=str(self._workspace.resolve()),
            count=block_count,
            lease_owner=f"copy:{target_conversation_id}",
            now_us=instant,
        )
        recall_sequences = list(lease.values()) if lease else []
        result = await asyncio.to_thread(
            ChatStorageCoordinator(self._paths).copy_conversation,
            ConversationCopyRequest(
                conversation_id=target_conversation_id,
                source_conversation_id=conversation_id,
                source_turn_id=source_turn_id,
                root_conversation_id=root_conversation_id,
                session_id=target_session_id,
                agent_id=agent_id,
                owner_scope=owner_scope,
                display_name=display_name,
                group_id=group_id,
                now_us=instant,
            ),
            recall_sequences=recall_sequences,
        )
        await self._project_change(target_conversation_id)
        self._validated_conversations.add(target_conversation_id)
        return result

    async def discard_unpublished_chat(self, conversation_id: str) -> None:
        """Remove a copied Chat that failed before its spec was published."""
        instant = time.time_ns() // 1_000
        target = await asyncio.to_thread(
            begin_conversation_deletion,
            self._paths,
            conversation_id,
            spec_json=None,
            now_us=instant,
        )
        if target is not None:
            await asyncio.to_thread(
                purge_conversation_storage,
                self._paths,
                conversation_id,
                now_us=instant,
            )
        self._validated_conversations.discard(conversation_id)

    async def history_view(
        self,
        conversation_id: str,
        *,
        backend: str = "qwenpaw",
    ) -> dict:
        """Return lightweight metadata without loading transcript content."""
        await self.ensure_ready(conversation_id, backend=backend)

        def _read() -> dict:
            with connect_database(
                self._paths.catalog,
                readonly=True,
            ) as catalog:
                catalog_row = catalog.execute(
                    "SELECT display_name, lifecycle, active_run_id, "
                    "run_state, catalog_generation FROM "
                    "chat_catalog_entries WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()
                migration_row = catalog.execute(
                    "SELECT state FROM chat_migrations "
                    "WHERE conversation_id = ? ORDER BY updated_at_us DESC "
                    "LIMIT 1",
                    (conversation_id,),
                ).fetchone()
            if catalog_row is None:
                raise CatalogEntryNotFoundError(conversation_id)
            with connect_database(
                self._paths.conversation(conversation_id),
                readonly=True,
            ) as connection:
                meta_row = connection.execute(
                    "SELECT head_turn_id, current_context_revision_id "
                    "FROM chat_meta WHERE singleton_id = 1",
                ).fetchone()
            if meta_row is None:
                raise RuntimeError(
                    f"Ready Chat has no metadata: {conversation_id}",
                )
            page_view = read_history_page_view(
                self._paths.conversation(conversation_id),
                conversation_id=conversation_id,
            )
            return {
                "conversation_id": conversation_id,
                "display_name": str(catalog_row["display_name"]),
                "lifecycle": str(catalog_row["lifecycle"]),
                "migration_state": (
                    str(migration_row["state"])
                    if migration_row is not None
                    else "ready_v2"
                ),
                "catalog_generation": int(
                    catalog_row["catalog_generation"],
                ),
                "head_turn_id": page_view.head_turn_id,
                "view_version": page_view.view_version,
                "context_revision_id": str(
                    meta_row["current_context_revision_id"],
                ),
                "active_run": (
                    {
                        "run_id": str(catalog_row["active_run_id"]),
                        "state": str(catalog_row["run_state"]),
                    }
                    if catalog_row["active_run_id"] is not None
                    else None
                ),
                "capabilities": {
                    "turn_pagination": True,
                    "message_pagination": True,
                    "block_pagination": True,
                    "block_fragments": True,
                    "live_resync": True,
                    "chat_copy": backend == "qwenpaw",
                    "edit_last_user_message": False,
                },
            }

        return await asyncio.to_thread(_read)

    async def _remove_page_request(
        self,
        key: tuple[object, ...],
        task: asyncio.Task[ChatPage],
    ) -> None:
        async with self._page_requests_lock:
            if self._page_requests.get(key) is task:
                self._page_requests.pop(key, None)

    async def _coalesced_page(
        self,
        key: tuple[object, ...],
        loader: Callable[[], Coroutine[Any, Any, ChatPage]],
    ) -> ChatPage:
        """Share identical in-flight reads without sharing cancellation."""
        async with self._page_requests_lock:
            task = self._page_requests.get(key)
            if task is None:
                task = asyncio.create_task(loader())
                self._page_requests[key] = task
                loop = asyncio.get_running_loop()
                task.add_done_callback(
                    lambda done: loop.create_task(
                        self._remove_page_request(key, done),
                    ),
                )
        return await asyncio.shield(task)

    async def turn_page(
        self,
        conversation_id: str,
        *,
        subject: str,
        backend: str = "qwenpaw",
        head_turn_id: str | None = None,
        view_version: str | None = None,
        cursor: str | None = None,
        target_turn_id: str | None = None,
        limit: int = 30,
    ) -> ChatPage:
        """Read one stable page of turns for an authorized subject."""
        await self.ensure_ready(conversation_id, backend=backend)
        key = (
            "turn",
            conversation_id,
            subject,
            head_turn_id,
            view_version,
            cursor,
            target_turn_id,
            limit,
        )
        return await self._coalesced_page(
            key,
            lambda: asyncio.to_thread(
                read_turn_page,
                self._paths.conversation(conversation_id),
                conversation_id=conversation_id,
                subject=subject,
                codec=self._cursor_codec,
                head_turn_id=head_turn_id,
                view_version=view_version,
                cursor=cursor,
                target_turn_id=target_turn_id,
                limit=limit,
            ),
        )

    async def search_history(
        self,
        query: str,
        *,
        before_seq: int | None = None,
        limit: int = 50,
    ) -> ChatRecallSearchPage:
        """Search the bounded workspace recall projection."""
        return await asyncio.to_thread(
            search_chat_recall,
            self._paths.recall,
            query=query,
            before_seq=before_seq,
            limit=limit,
        )

    async def message_page(
        self,
        conversation_id: str,
        *,
        turn_id: str,
        subject: str,
        backend: str = "qwenpaw",
        head_turn_id: str | None = None,
        view_version: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> ChatPage:
        """Read one message page after Chat-lineage verification."""
        await self.ensure_ready(conversation_id, backend=backend)
        key = (
            "message",
            conversation_id,
            turn_id,
            subject,
            head_turn_id,
            view_version,
            cursor,
            limit,
        )
        return await self._coalesced_page(
            key,
            lambda: asyncio.to_thread(
                read_message_page,
                self._paths.conversation(conversation_id),
                conversation_id=conversation_id,
                turn_id=turn_id,
                subject=subject,
                codec=self._cursor_codec,
                head_turn_id=head_turn_id,
                view_version=view_version,
                cursor=cursor,
                limit=limit,
            ),
        )

    async def block_page(
        self,
        conversation_id: str,
        *,
        message_id: str,
        subject: str,
        backend: str = "qwenpaw",
        head_turn_id: str | None = None,
        view_version: str | None = None,
        cursor: str | None = None,
        limit: int = 200,
    ) -> ChatPage:
        """Read one block page with byte-bounded inline content."""
        await self.ensure_ready(conversation_id, backend=backend)
        key = (
            "block",
            conversation_id,
            message_id,
            subject,
            head_turn_id,
            view_version,
            cursor,
            limit,
        )
        return await self._coalesced_page(
            key,
            lambda: asyncio.to_thread(
                read_block_page,
                self._paths.conversation(conversation_id),
                conversation_id=conversation_id,
                message_id=message_id,
                subject=subject,
                codec=self._cursor_codec,
                head_turn_id=head_turn_id,
                view_version=view_version,
                cursor=cursor,
                limit=limit,
            ),
        )

    async def block_fragment(
        self,
        conversation_id: str,
        *,
        block_id: str,
        subject: str,
        backend: str = "qwenpaw",
        head_turn_id: str | None = None,
        view_version: str | None = None,
        cursor: str | None = None,
    ) -> ChatPage:
        """Read one version-fenced block fragment."""
        await self.ensure_ready(conversation_id, backend=backend)
        key = (
            "fragment",
            conversation_id,
            block_id,
            subject,
            head_turn_id,
            view_version,
            cursor,
        )
        return await self._coalesced_page(
            key,
            lambda: asyncio.to_thread(
                read_block_fragment,
                self._paths.conversation(conversation_id),
                conversation_id=conversation_id,
                block_id=block_id,
                subject=subject,
                codec=self._cursor_codec,
                head_turn_id=head_turn_id,
                view_version=view_version,
                cursor=cursor,
            ),
        )

    async def mark_intent_terminal(
        self,
        *,
        conversation_id: str,
        client_request_id: str,
        now_us: int | None = None,
    ) -> None:
        """Close the catalog-side acceptance intent after terminal commit."""
        instant = now_us if now_us is not None else time.time_ns() // 1_000

        def _mark() -> None:
            with connect_database(self._paths.catalog) as connection:
                with immediate_transaction(connection):
                    connection.execute(
                        "UPDATE chat_runtime_intents SET state = 'terminal', "
                        "updated_at_us = ? WHERE conversation_id = ? "
                        "AND client_request_id = ?",
                        (
                            instant,
                            conversation_id,
                            client_request_id,
                        ),
                    )

        await asyncio.to_thread(_mark)

    def resolve_alias(self, alias: ChatAlias) -> str | None:
        """Resolve an external identity without opening a Chat source DB."""
        with connect_database(
            self._paths.catalog,
            readonly=True,
        ) as connection:
            row = connection.execute(
                "SELECT conversation_id FROM chat_alias_index "
                "WHERE agent_id = ? AND alias_namespace = ? "
                "AND external_key = ? AND owner_scope = ?",
                (
                    alias.agent_id,
                    alias.namespace,
                    alias.external_key,
                    alias.owner_scope,
                ),
            ).fetchone()
        if row is None:
            return None
        return str(row["conversation_id"])

    async def ensure_ready(
        self,
        conversation_id: str,
        *,
        backend: str = "qwenpaw",
    ) -> str:
        """Pass the per-Chat migration barrier and validate its database."""
        with connect_database(
            self._paths.catalog,
            readonly=True,
        ) as connection:
            row = connection.execute(
                "SELECT lifecycle, agent_id, owner_scope, created_at_us "
                "FROM chat_catalog_entries "
                "WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        if row is None:
            raise CatalogEntryNotFoundError(conversation_id)
        lifecycle = str(row["lifecycle"])
        if lifecycle not in {"ready", "quarantined"}:
            result = await self._migration.migrate(
                conversation_id,
                backend=backend,
            )
            self._validated_conversations.add(conversation_id)
            return result.conversation_id
        if conversation_id not in self._validated_conversations:
            try:
                await asyncio.to_thread(
                    initialize_conversation_database,
                    self._paths.conversation(conversation_id),
                    conversation_id=conversation_id,
                    agent_id=str(row["agent_id"]),
                    owner_scope=str(row["owner_scope"]),
                    backend=backend,
                    created_at_us=int(row["created_at_us"]),
                )
            except ChatStorageSchemaError:
                await asyncio.to_thread(
                    self._quarantine_conversation,
                    conversation_id,
                    time.time_ns() // 1_000,
                )
                raise
            if lifecycle == "quarantined":
                await asyncio.to_thread(
                    self._restore_quarantined_conversation,
                    conversation_id,
                    time.time_ns() // 1_000,
                )
            self._validated_conversations.add(conversation_id)
        return conversation_id

    def _restore_quarantined_conversation(
        self,
        conversation_id: str,
        now_us: int,
    ) -> None:
        """Return a successfully validated quarantined Chat to ready."""
        with connect_database(self._paths.catalog) as connection:
            with immediate_transaction(connection):
                connection.execute(
                    "UPDATE chat_catalog_entries SET lifecycle = 'ready', "
                    "updated_at_us = ? WHERE conversation_id = ? "
                    "AND lifecycle = 'quarantined'",
                    (now_us, conversation_id),
                )

    def _quarantine_conversation(
        self,
        conversation_id: str,
        now_us: int,
    ) -> None:
        with connect_database(self._paths.catalog) as connection:
            with immediate_transaction(connection):
                connection.execute(
                    "UPDATE chat_catalog_entries SET "
                    "lifecycle = 'quarantined', active_run_id = NULL, "
                    "run_state = NULL, updated_at_us = ? "
                    "WHERE conversation_id = ?",
                    (now_us, conversation_id),
                )

    async def accept_input(
        self,
        *,
        conversation_id: str,
        client_request_id: str,
        message: Message,
        backend: str = "qwenpaw",
        now_us: int | None = None,
    ) -> AcceptanceResult:
        """Durably accept user input before runtime execution can start."""
        await self.ensure_ready(conversation_id, backend=backend)
        instant = now_us if now_us is not None else time.time_ns() // 1_000
        await asyncio.to_thread(
            self._record_intent,
            conversation_id,
            client_request_id,
            instant,
        )
        lease = await asyncio.to_thread(
            reserve_recall_sequences,
            self._paths.catalog,
            workspace_id=str(self._workspace.resolve()),
            count=len(message.content),
            lease_owner=self._process_instance_id,
            now_us=instant,
        )
        request = AcceptanceRequest(
            conversation_id=conversation_id,
            client_request_id=client_request_id,
            message=message,
            process_instance_id=self._process_instance_id,
            now_us=instant,
        )

        async def accept_and_publish() -> AcceptanceResult:
            try:
                result = await asyncio.to_thread(
                    accept_input,
                    self._paths.conversation(conversation_id),
                    request,
                    recall_sequences=lease.values() if lease else (),
                )
            except BaseException:
                await asyncio.to_thread(
                    self._finish_intent,
                    conversation_id,
                    client_request_id,
                    None,
                    "terminal",
                    instant,
                )
                raise
            if result.replayed:
                return result
            await asyncio.to_thread(
                self._finish_intent,
                conversation_id,
                client_request_id,
                result,
                "recorded",
                instant,
            )
            await self._project_after_acceptance(
                conversation_id,
                result,
                instant,
            )
            return result

        acceptance_task = asyncio.create_task(accept_and_publish())
        try:
            return await asyncio.shield(acceptance_task)
        except asyncio.CancelledError:
            result = await acceptance_task
            if not result.replayed:
                await asyncio.shield(
                    self.cancel_accepted_before_runtime(
                        conversation_id=conversation_id,
                        accepted=result,
                    ),
                )
            raise

    async def _project_after_acceptance(
        self,
        conversation_id: str,
        accepted: AcceptanceResult,
        now_us: int,
    ) -> None:
        """Publish an acceptance without weakening source durability."""
        try:
            await asyncio.to_thread(
                project_conversation,
                self._paths,
                conversation_id,
                now_us=now_us,
            )
        except Exception:
            try:
                await asyncio.to_thread(
                    enqueue_projection_repair,
                    self._paths,
                    conversation_id,
                    expected_generation=accepted.source_generation,
                    now_us=now_us,
                )
            except Exception:
                logger.exception(
                    "Chat projection repair could not be queued after "
                    "durable acceptance",
                )
            logger.warning(
                "Chat projection deferred after durable acceptance",
                exc_info=True,
            )

    async def wait_for_queued_run(
        self,
        *,
        conversation_id: str,
        accepted: AcceptanceResult,
    ) -> AcceptanceResult:
        """Wait until a durable queued input is atomically runnable."""
        if accepted.kind == "run" or accepted.replayed:
            return accepted
        try:
            while True:
                instant = time.time_ns() // 1_000
                claimed = await asyncio.to_thread(
                    claim_queued_input,
                    self._paths.conversation(conversation_id),
                    conversation_id=conversation_id,
                    queue_item_id=accepted.work_id,
                    process_instance_id=self._process_instance_id,
                    now_us=instant,
                )
                if claimed is not None:
                    await asyncio.to_thread(
                        self._finish_intent,
                        conversation_id,
                        claimed.client_request_id,
                        claimed,
                        "recorded",
                        instant,
                    )
                    await self._project_after_queue_claim(
                        conversation_id,
                        claimed,
                        instant,
                    )
                    return claimed
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            await asyncio.shield(
                self.cancel_accepted_before_runtime(
                    conversation_id=conversation_id,
                    accepted=accepted,
                ),
            )
            raise

    async def _project_after_queue_claim(
        self,
        conversation_id: str,
        claimed: AcceptanceResult,
        now_us: int,
    ) -> None:
        """Publish a queue claim without weakening source durability."""
        try:
            await asyncio.to_thread(
                project_conversation,
                self._paths,
                conversation_id,
                now_us=now_us,
            )
        except Exception:
            try:
                await asyncio.to_thread(
                    enqueue_projection_repair,
                    self._paths,
                    conversation_id,
                    expected_generation=claimed.source_generation,
                    now_us=now_us,
                )
            except Exception:
                logger.exception(
                    "Chat projection repair could not be queued "
                    "after queue claim",
                )
            logger.warning(
                "Chat projection deferred after queue claim",
                exc_info=True,
            )

    async def cancel_accepted_before_runtime(
        self,
        *,
        conversation_id: str,
        accepted: AcceptanceResult,
    ) -> None:
        """Close durable accepted work whose runtime will not be started."""
        instant = time.time_ns() // 1_000
        generation = await asyncio.to_thread(
            cancel_accepted_input,
            self._paths.conversation(conversation_id),
            conversation_id=conversation_id,
            accepted=accepted,
            process_instance_id=self._process_instance_id,
            now_us=instant,
        )
        await self.mark_intent_terminal(
            conversation_id=conversation_id,
            client_request_id=accepted.client_request_id,
            now_us=instant,
        )
        if generation is None:
            return
        try:
            await asyncio.to_thread(
                project_conversation,
                self._paths,
                conversation_id,
                now_us=instant,
            )
        except Exception:
            try:
                await asyncio.to_thread(
                    enqueue_projection_repair,
                    self._paths,
                    conversation_id,
                    expected_generation=generation,
                    now_us=instant,
                )
            except Exception:
                logger.exception(
                    "Chat projection repair could not be queued after "
                    "queue cancellation",
                )
            logger.warning(
                "Chat projection deferred after queue cancellation",
                exc_info=True,
            )

    def _record_intent(
        self,
        conversation_id: str,
        client_request_id: str,
        now_us: int,
    ) -> None:
        with connect_database(self._paths.catalog) as connection:
            with immediate_transaction(connection):
                connection.execute(
                    "INSERT INTO chat_runtime_intents("
                    "conversation_id, client_request_id, kind, "
                    "state, created_at_us, updated_at_us) "
                    "VALUES (?, ?, 'run', 'starting', ?, ?) "
                    "ON CONFLICT(conversation_id, client_request_id) "
                    "DO NOTHING",
                    (
                        conversation_id,
                        client_request_id,
                        now_us,
                        now_us,
                    ),
                )

    def _finish_intent(
        self,
        conversation_id: str,
        client_request_id: str,
        result: AcceptanceResult | None,
        state: str,
        now_us: int,
    ) -> None:
        with connect_database(self._paths.catalog) as connection:
            with immediate_transaction(connection):
                connection.execute(
                    "UPDATE chat_runtime_intents SET kind = ?, "
                    "run_or_queue_id = ?, state = ?, updated_at_us = ? "
                    "WHERE conversation_id = ? AND client_request_id = ?",
                    (
                        result.kind if result else "run",
                        result.work_id if result else None,
                        state,
                        now_us,
                        conversation_id,
                        client_request_id,
                    ),
                )

    async def recover_after_restart(self, *, now_us: int | None = None) -> int:
        """Interrupt durable nonterminal work without resuming execution."""
        instant = now_us if now_us is not None else time.time_ns() // 1_000
        with connect_database(
            self._paths.catalog,
            readonly=True,
        ) as connection:
            conversation_ids = [
                str(row["conversation_id"])
                for row in connection.execute(
                    "SELECT conversation_id FROM chat_catalog_entries "
                    "WHERE lifecycle = 'ready' AND (run_state IN "
                    "('accepted', 'running', 'stopping') OR "
                    "conversation_id IN (SELECT conversation_id "
                    "FROM chat_runtime_intents WHERE state != 'terminal'))",
                )
            ]
        recovered = 0
        for conversation_id in conversation_ids:
            result = await asyncio.to_thread(
                interrupt_nonterminal_work,
                self._paths.conversation(conversation_id),
                conversation_id=conversation_id,
                now_us=instant,
            )
            if any(
                (
                    result.runs,
                    result.turns,
                    result.messages,
                    result.blocks,
                    result.tools,
                    result.queue_items,
                ),
            ):
                recovered += 1
            with connect_database(self._paths.catalog) as connection:
                with immediate_transaction(connection):
                    connection.execute(
                        "UPDATE chat_runtime_intents SET state = 'terminal', "
                        "updated_at_us = ? WHERE conversation_id = ? "
                        "AND state != 'terminal'",
                        (instant, conversation_id),
                    )
            try:
                await asyncio.to_thread(
                    project_conversation,
                    self._paths,
                    conversation_id,
                    now_us=instant,
                )
            except Exception:
                try:
                    await asyncio.to_thread(
                        enqueue_projection_repair,
                        self._paths,
                        conversation_id,
                        expected_generation=result.source_generation,
                        now_us=instant,
                    )
                except Exception:
                    logger.exception(
                        "Chat projection repair could not be queued after "
                        "restart recovery",
                    )
                logger.warning(
                    "Chat projection deferred after restart recovery",
                    exc_info=True,
                )
        return recovered
