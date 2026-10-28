# -*- coding: utf-8 -*-
"""Recoverable coordination across catalog and per-conversation databases."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .catalog_repository import CatalogEntry, CatalogRepository
from .common import ChatStorageSchemaError, connect_database
from .conversation import (
    initialize_conversation_database,
    validate_conversation_database,
)
from .conversation_repository import initialize_timeline
from .deletion import begin_conversation_deletion, purge_conversation_storage
from .copy_repository import ChatCopyResult, copy_conversation_at_turn
from .paths import ChatStoragePaths


@dataclass(frozen=True)
class ConversationCreateRequest:
    """Immutable inputs for one recoverable Chat creation attempt."""

    conversation_id: str
    agent_id: str
    owner_scope: str
    display_name: str
    backend: str
    now_us: int


@dataclass(frozen=True)
class ConversationCopyRequest:
    """Immutable inputs for publishing one independent copied Chat."""

    conversation_id: str
    source_conversation_id: str
    source_turn_id: str
    root_conversation_id: str
    session_id: str
    agent_id: str
    owner_scope: str
    display_name: str
    group_id: str | None
    now_us: int


class ChatStorageCoordinator:
    """Coordinate cross-file state machines without pretending atomicity."""

    def __init__(
        self,
        paths: ChatStoragePaths,
        *,
        catalog: CatalogRepository | None = None,
    ) -> None:
        self._paths = paths
        self._paths.ensure_layout()
        self._catalog = catalog or CatalogRepository(paths.catalog)

    def create_conversation(
        self,
        request: ConversationCreateRequest,
    ) -> CatalogEntry:
        """Create one Chat, safely resumable around the atomic file publish."""
        target = self._paths.conversation(request.conversation_id)
        relative_path = target.relative_to(self._paths.root).as_posix()
        self._catalog.reserve_conversation(
            conversation_id=request.conversation_id,
            agent_id=request.agent_id,
            owner_scope=request.owner_scope,
            display_name=request.display_name,
            physical_relpath=relative_path,
            now_us=request.now_us,
        )
        if target.is_file():
            validate_conversation_database(
                target,
                expected_conversation_id=request.conversation_id,
                expected_agent_id=request.agent_id,
                expected_owner_scope=request.owner_scope,
                expected_backend=request.backend,
            )
            initialize_timeline(
                target,
                conversation_id=request.conversation_id,
                now_us=request.now_us,
            )
            self._catalog.mark_ready(
                conversation_id=request.conversation_id,
                now_us=request.now_us,
            )
            return self._catalog.set_runtime_projection(
                request.conversation_id,
                source_generation=1,
                now_us=request.now_us,
            )

        temporary = target.with_name(f".{target.name}.creating")
        initialize_conversation_database(
            temporary,
            conversation_id=request.conversation_id,
            agent_id=request.agent_id,
            owner_scope=request.owner_scope,
            backend=request.backend,
            created_at_us=request.now_us,
        )
        initialize_timeline(
            temporary,
            conversation_id=request.conversation_id,
            now_us=request.now_us,
        )
        validate_conversation_database(
            temporary,
            expected_conversation_id=request.conversation_id,
            expected_agent_id=request.agent_id,
            expected_owner_scope=request.owner_scope,
            expected_backend=request.backend,
        )
        self._prepare_database_for_move(temporary)
        self._fsync_file(temporary)
        os.replace(temporary, target)
        self._fsync_directory(target.parent)
        validate_conversation_database(
            target,
            expected_conversation_id=request.conversation_id,
            expected_agent_id=request.agent_id,
            expected_owner_scope=request.owner_scope,
            expected_backend=request.backend,
        )
        self._catalog.mark_ready(
            request.conversation_id,
            now_us=request.now_us,
        )
        return self._catalog.set_runtime_projection(
            request.conversation_id,
            source_generation=1,
            now_us=request.now_us,
        )

    def copy_conversation(
        self,
        request: ConversationCopyRequest,
        *,
        recall_sequences: list[int],
    ) -> ChatCopyResult:
        """Publish a new physical Chat copied through one completed turn."""
        target = self._paths.conversation(request.conversation_id)
        relative_path = target.relative_to(self._paths.root).as_posix()
        self._catalog.reserve_conversation(
            conversation_id=request.conversation_id,
            agent_id=request.agent_id,
            owner_scope=request.owner_scope,
            display_name=request.display_name,
            physical_relpath=relative_path,
            now_us=request.now_us,
            source_conversation_id=request.source_conversation_id,
            source_turn_id=request.source_turn_id,
            root_conversation_id=request.root_conversation_id,
            copied_at_us=request.now_us,
        )
        if target.exists():
            raise ChatStorageSchemaError(
                f"Copy target already exists: {request.conversation_id}",
            )
        temporary = target.with_name(f".{target.name}.copying")
        temporary.unlink(missing_ok=True)
        published = False
        try:
            result = copy_conversation_at_turn(
                self._paths.conversation(request.source_conversation_id),
                temporary,
                conversation_id=request.conversation_id,
                source_conversation_id=request.source_conversation_id,
                source_turn_id=request.source_turn_id,
                session_id=request.session_id,
                recall_sequences=recall_sequences,
                now_us=request.now_us,
            )
            validate_conversation_database(
                temporary,
                expected_conversation_id=request.conversation_id,
                expected_agent_id=request.agent_id,
                expected_owner_scope=request.owner_scope,
            )
            self._prepare_database_for_move(temporary)
            self._fsync_file(temporary)
            os.replace(temporary, target)
            self._fsync_directory(target.parent)
            self._catalog.mark_ready(
                request.conversation_id,
                now_us=request.now_us,
            )
            published = True
            self._catalog.set_runtime_projection(
                request.conversation_id,
                source_generation=1,
                now_us=request.now_us,
            )
            if request.group_id is not None:
                self._catalog.set_group(
                    request.conversation_id,
                    request.group_id,
                    now_us=request.now_us,
                )
            return result
        except Exception:
            temporary.unlink(missing_ok=True)
            if published:
                begin_conversation_deletion(
                    self._paths,
                    request.conversation_id,
                    spec_json=None,
                    now_us=request.now_us,
                )
                purge_conversation_storage(
                    self._paths,
                    request.conversation_id,
                    now_us=request.now_us,
                )
            else:
                target.unlink(missing_ok=True)
                self._catalog.release_creating(request.conversation_id)
            raise

    @staticmethod
    def _prepare_database_for_move(path: Path) -> None:
        with connect_database(path) as connection:
            checkpoint = connection.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)",
            ).fetchone()
            if checkpoint is None or int(checkpoint[0]) != 0:
                raise ChatStorageSchemaError(
                    f"Could not checkpoint temporary Chat database: {path}",
                )
            mode = str(
                connection.execute("PRAGMA journal_mode = DELETE").fetchone()[
                    0
                ],
            ).lower()
            if mode != "delete":
                raise ChatStorageSchemaError(
                    f"Could not detach temporary Chat WAL: {path}",
                )
        sidecars = (
            path.with_name(f"{path.name}-wal"),
            path.with_name(f"{path.name}-shm"),
        )
        if any(sidecar.exists() for sidecar in sidecars):
            raise ChatStorageSchemaError(
                f"Temporary Chat database retained SQLite sidecars: {path}",
            )

    @staticmethod
    def _fsync_file(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
