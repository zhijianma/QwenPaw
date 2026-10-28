# -*- coding: utf-8 -*-
"""Transactional repository for workspace-scoped Chat catalog ownership."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .catalog import initialize_catalog_database
from .common import connect_database, immediate_transaction


class CatalogEntryNotFoundError(LookupError):
    """The requested catalog entry does not exist or is no longer mutable."""


class CatalogStateConflictError(RuntimeError):
    """A catalog lifecycle transition conflicts with durable state."""


@dataclass(frozen=True)
class CatalogEntry:
    """Catalog-owned fields plus rebuildable runtime projections."""

    conversation_id: str
    agent_id: str
    owner_scope: str
    display_name: str
    physical_relpath: str
    lifecycle: str
    pinned: bool
    archived_at_us: int | None
    group_id: str | None
    active_run_id: str | None
    run_state: str | None
    source_conversation_id: str | None
    source_turn_id: str | None
    root_conversation_id: str | None
    copied_at_us: int | None
    source_generation: int
    catalog_generation: int
    created_at_us: int
    updated_at_us: int


class CatalogRepository:
    """Own Chat listing metadata and creation lifecycle transitions."""

    def __init__(self, path: Path) -> None:
        self._path = path
        initialize_catalog_database(path)

    def reserve_conversation(
        self,
        *,
        conversation_id: str,
        agent_id: str,
        owner_scope: str,
        display_name: str,
        physical_relpath: str,
        now_us: int,
        source_conversation_id: str | None = None,
        source_turn_id: str | None = None,
        root_conversation_id: str | None = None,
        copied_at_us: int | None = None,
    ) -> CatalogEntry:
        """Reserve a physical identity before the conversation DB is built."""
        with connect_database(self._path) as connection:
            with immediate_transaction(connection):
                existing = self._select_entry(connection, conversation_id)
                if existing is not None:
                    if (
                        existing.lifecycle in ("creating", "ready")
                        and existing.agent_id == agent_id
                        and existing.owner_scope == owner_scope
                        and existing.physical_relpath == physical_relpath
                    ):
                        return existing
                    raise CatalogStateConflictError(
                        f"Conversation reservation already exists: "
                        f"{conversation_id}",
                    )
                connection.execute(
                    "INSERT INTO chat_catalog_entries("
                    "conversation_id, agent_id, owner_scope, display_name, "
                    "physical_relpath, lifecycle, source_conversation_id, "
                    "source_turn_id, root_conversation_id, copied_at_us, "
                    "created_at_us, updated_at_us) VALUES "
                    "(?, ?, ?, ?, ?, 'creating', ?, ?, ?, ?, ?, ?)",
                    (
                        conversation_id,
                        agent_id,
                        owner_scope,
                        display_name,
                        physical_relpath,
                        source_conversation_id,
                        source_turn_id,
                        root_conversation_id,
                        copied_at_us,
                        now_us,
                        now_us,
                    ),
                )
            entry = self._select_entry(connection, conversation_id)
            if entry is None:
                raise CatalogStateConflictError(
                    f"Conversation reservation disappeared: {conversation_id}",
                )
            return entry

    def mark_ready(
        self,
        conversation_id: str,
        *,
        now_us: int,
    ) -> CatalogEntry:
        """Publish one fully validated per-conversation database."""
        with connect_database(self._path) as connection:
            with immediate_transaction(connection):
                entry = self._require_entry(connection, conversation_id)
                if entry.lifecycle == "ready":
                    return entry
                if entry.lifecycle != "creating":
                    raise CatalogStateConflictError(
                        f"Cannot mark {conversation_id} ready from "
                        f"{entry.lifecycle}",
                    )
                connection.execute(
                    "UPDATE chat_catalog_entries SET lifecycle = 'ready', "
                    "updated_at_us = ? WHERE conversation_id = ?",
                    (now_us, conversation_id),
                )
            return self._require_entry(connection, conversation_id)

    def release_creating(self, conversation_id: str) -> None:
        """Remove an unpublished reservation after target cleanup."""
        with connect_database(self._path) as connection:
            with immediate_transaction(connection):
                connection.execute(
                    "DELETE FROM chat_catalog_entries WHERE "
                    "conversation_id = ? AND lifecycle = 'creating'",
                    (conversation_id,),
                )

    def set_runtime_projection(
        self,
        conversation_id: str,
        *,
        source_generation: int,
        now_us: int,
    ) -> CatalogEntry:
        """Repair rebuildable source-owned runtime fields."""
        with connect_database(self._path) as connection:
            with immediate_transaction(connection):
                entry = self._require_entry(connection, conversation_id)
                if entry.lifecycle != "ready":
                    raise CatalogStateConflictError(
                        f"Conversation is not ready: {conversation_id}",
                    )
                connection.execute(
                    "UPDATE chat_catalog_entries SET "
                    "source_generation = MAX(source_generation, ?), "
                    "updated_at_us = ? WHERE conversation_id = ?",
                    (
                        source_generation,
                        now_us,
                        conversation_id,
                    ),
                )
            return self._require_entry(connection, conversation_id)

    def rename(
        self,
        conversation_id: str,
        display_name: str,
        *,
        now_us: int,
    ) -> CatalogEntry:
        """Update the catalog-owned Chat display name."""
        if not display_name.strip():
            raise ValueError("display_name must not be blank")
        return self._update_ready_entry(
            conversation_id,
            "display_name = ?",
            (display_name,),
            now_us=now_us,
        )

    def set_pinned(
        self,
        conversation_id: str,
        pinned: bool,
        *,
        now_us: int,
    ) -> CatalogEntry:
        """Update the catalog-owned pin state."""
        return self._update_ready_entry(
            conversation_id,
            "pinned = ?",
            (int(pinned),),
            now_us=now_us,
        )

    def set_archived_at(
        self,
        conversation_id: str,
        archived_at_us: int | None,
        *,
        now_us: int,
    ) -> CatalogEntry:
        """Archive or unarchive one Chat without touching its transcript."""
        return self._update_ready_entry(
            conversation_id,
            "archived_at_us = ?",
            (archived_at_us,),
            now_us=now_us,
        )

    def set_group(
        self,
        conversation_id: str,
        group_id: str | None,
        *,
        now_us: int,
    ) -> CatalogEntry:
        """Move one Chat between catalog-owned groups."""
        return self._update_ready_entry(
            conversation_id,
            "group_id = ?",
            (group_id,),
            now_us=now_us,
        )

    def create_group(
        self,
        *,
        group_id: str,
        agent_id: str,
        name: str,
        ordinal: int,
        kind: str,
        now_us: int,
    ) -> None:
        """Create workspace catalog grouping metadata atomically."""
        with connect_database(self._path) as connection:
            with immediate_transaction(connection):
                connection.execute(
                    "INSERT INTO chat_groups("
                    "group_id, agent_id, name, ordinal, kind, created_at_us, "
                    "updated_at_us) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (group_id, agent_id, name, ordinal, kind, now_us, now_us),
                )

    def get(self, conversation_id: str) -> CatalogEntry | None:
        """Read one catalog row without opening transcript storage."""
        with connect_database(self._path, readonly=True) as connection:
            return self._select_entry(connection, conversation_id)

    def _update_ready_entry(
        self,
        conversation_id: str,
        assignment: str,
        values: tuple[object, ...],
        *,
        now_us: int,
    ) -> CatalogEntry:
        with connect_database(self._path) as connection:
            with immediate_transaction(connection):
                entry = self._require_entry(connection, conversation_id)
                if entry.lifecycle != "ready":
                    raise CatalogStateConflictError(
                        f"Conversation is not ready: {conversation_id}",
                    )
                connection.execute(
                    f"UPDATE chat_catalog_entries SET {assignment}, "
                    "catalog_generation = catalog_generation + 1, "
                    "updated_at_us = ? WHERE conversation_id = ?",
                    (*values, now_us, conversation_id),
                )
            return self._require_entry(connection, conversation_id)

    @staticmethod
    def _require_entry(
        connection: sqlite3.Connection,
        conversation_id: str,
    ) -> CatalogEntry:
        entry = CatalogRepository._select_entry(connection, conversation_id)
        if entry is None:
            raise CatalogEntryNotFoundError(conversation_id)
        return entry

    @staticmethod
    def _select_entry(
        connection: sqlite3.Connection,
        conversation_id: str,
    ) -> CatalogEntry | None:
        row = connection.execute(
            "SELECT conversation_id, agent_id, owner_scope, display_name, "
            "physical_relpath, lifecycle, pinned, archived_at_us, group_id, "
            "active_run_id, run_state, source_conversation_id, "
            "source_turn_id, root_conversation_id, copied_at_us, "
            "source_generation, catalog_generation, created_at_us, "
            "updated_at_us "
            "FROM chat_catalog_entries WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            return None
        return CatalogEntry(
            conversation_id=str(row["conversation_id"]),
            agent_id=str(row["agent_id"]),
            owner_scope=str(row["owner_scope"]),
            display_name=str(row["display_name"]),
            physical_relpath=str(row["physical_relpath"]),
            lifecycle=str(row["lifecycle"]),
            pinned=bool(row["pinned"]),
            archived_at_us=row["archived_at_us"],
            group_id=row["group_id"],
            active_run_id=row["active_run_id"],
            run_state=row["run_state"],
            source_conversation_id=row["source_conversation_id"],
            source_turn_id=row["source_turn_id"],
            root_conversation_id=row["root_conversation_id"],
            copied_at_us=row["copied_at_us"],
            source_generation=int(row["source_generation"]),
            catalog_generation=int(row["catalog_generation"]),
            created_at_us=int(row["created_at_us"]),
            updated_at_us=int(row["updated_at_us"]),
        )
