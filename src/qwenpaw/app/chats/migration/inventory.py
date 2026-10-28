# -*- coding: utf-8 -*-
"""Cheap legacy Chat inventory that never parses session transcript JSON."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

from ..models import ChatSpec, ChatsFile
from ..session import session_relative_paths
from ..storage import (
    CatalogRepository,
    CatalogStateConflictError,
    ChatStoragePaths,
)
from ..storage.common import connect_database, immediate_transaction


MIGRATION_VERSION = 1


@dataclass(frozen=True)
class LegacyInventoryReport:
    """Counts and diagnostics from one metadata-only workspace scan."""

    chats: int
    hot: int
    cold: int
    missing_session: int
    orphan_files: tuple[str, ...]
    ambiguous_files: tuple[str, ...]
    ambiguous_aliases: tuple[str, ...]
    invalid_chats: tuple[str, ...]


def _iter_session_metadata(sessions_dir: Path) -> dict[str, tuple[int, int]]:
    """Return relative path -> (size, mtime_ns) without opening a file."""
    if not sessions_dir.is_dir():
        return {}
    result: dict[str, tuple[int, int]] = {}
    for path in sorted(sessions_dir.rglob("*.json")):
        relative = path.relative_to(sessions_dir)
        if any(part.startswith(".") for part in relative.parts):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        result[relative.as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return result


def _load_registry(chats_path: Path) -> ChatsFile:
    """Load only the small Chat registry, never a session transcript."""
    data = json.loads(chats_path.read_text(encoding="utf-8"))
    return ChatsFile.model_validate(data)


def _canonical_uuid(value: str) -> str | None:
    try:
        canonical = str(UUID(value))
    except (ValueError, AttributeError, TypeError):
        return None
    return canonical if value == canonical else None


def _owner_scope(chat: ChatSpec) -> str:
    return f"{chat.channel}:{chat.user_id}"


def _priority(chat: ChatSpec) -> int:
    updated = chat.updated_at
    timestamp_us = int(updated.timestamp() * 1_000_000)
    active_bonus = 10**18 if chat.archived_at is None else 0
    return active_bonus + max(timestamp_us, 0)


def _fingerprint(
    sources: list[str],
    metadata: dict[str, tuple[int, int]],
) -> str:
    return "|".join(
        f"{source}:{metadata[source][0]}:{metadata[source][1]}"
        for source in sorted(sources)
    )


def _timestamp_us(value: datetime) -> int:
    return int(value.timestamp() * 1_000_000)


def _is_deletion_tombstone(
    connection: sqlite3.Connection,
    conversation_id: str,
) -> bool:
    row = connection.execute(
        "SELECT lifecycle FROM chat_catalog_entries "
        "WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()
    return row is not None and str(row["lifecycle"]) in {
        "deleting",
        "deleted",
    }


def _upsert_inventory_chat(
    connection: sqlite3.Connection,
    *,
    chat: ChatSpec,
    canonical: str,
    agent_id: str,
    owner_scope: str,
    sources: list[str],
    conflicting: list[str],
    alias_conflicting: bool,
    fingerprint: str,
    now_us: int,
) -> None:
    """Write one inventory item inside the caller's batch transaction."""
    physical_relpath = f"conversations/{canonical}.db"
    connection.execute(
        "INSERT INTO chat_catalog_entries("
        "conversation_id, agent_id, owner_scope, display_name, spec_json, "
        "physical_relpath, lifecycle, pinned, archived_at_us, group_id, "
        "created_at_us, updated_at_us) "
        "VALUES (?, ?, ?, ?, ?, ?, 'creating', ?, ?, ?, ?, ?) "
        "ON CONFLICT(conversation_id) DO UPDATE SET "
        "spec_json = excluded.spec_json",
        (
            canonical,
            agent_id,
            owner_scope,
            chat.name,
            chat.model_dump_json(),
            physical_relpath,
            int(chat.pinned),
            (
                _timestamp_us(chat.archived_at)
                if chat.archived_at is not None
                else None
            ),
            chat.group_id,
            _timestamp_us(chat.created_at),
            _timestamp_us(chat.updated_at),
        ),
    )
    entry = connection.execute(
        "SELECT agent_id, owner_scope, physical_relpath, lifecycle "
        "FROM chat_catalog_entries WHERE conversation_id = ?",
        (canonical,),
    ).fetchone()
    expected_identity = {
        "agent_id": agent_id,
        "owner_scope": owner_scope,
        "physical_relpath": physical_relpath,
    }
    mismatched = [
        field
        for field, expected_value in expected_identity.items()
        if entry is None or str(entry[field]) != expected_value
    ]
    if entry is None or str(entry["lifecycle"]) not in {
        "creating",
        "quarantined",
        "ready",
    }:
        mismatched.append("lifecycle")
    if mismatched:
        raise CatalogStateConflictError(
            f"Legacy Chat conflicts with catalog identity: {canonical} "
            f"({','.join(mismatched)})",
        )

    state = (
        "failed_terminal"
        if conflicting or alias_conflicting
        else "legacy_only"
    )
    progress = {
        "session_id": chat.session_id,
        "user_id": chat.user_id,
        "channel": chat.channel,
        "sources": sources,
        "ambiguous_sources": conflicting,
        "ambiguous_alias": alias_conflicting,
        "history_db": "history.db",
    }
    existing = connection.execute(
        "SELECT state, source_fingerprint FROM chat_migrations "
        "WHERE source_locator = ?",
        (f"chat:{canonical}",),
    ).fetchone()
    published = str(entry["lifecycle"]) == "ready"
    if published:
        # Cutover is one-way. Legacy JSON remains only as a rollback artifact;
        # later mtime/size drift must neither replace its migration proof nor
        # demote an already-published V2 Chat. Native V2 Chats have no
        # migration row and inventory must not manufacture one for them.
        if existing is not None:
            connection.execute(
                "UPDATE chat_migrations SET state = 'ready_v2', "
                "priority = ?, error_digest = NULL, updated_at_us = ? "
                "WHERE source_locator = ?",
                (_priority(chat), now_us, f"chat:{canonical}"),
            )
    else:
        preserve_state = bool(
            existing
            and str(existing["source_fingerprint"] or "") == fingerprint
            and str(existing["state"])
            in {
                "snapshotting",
                "normalizing",
                "validating",
            },
        )
        connection.execute(
            "INSERT INTO chat_migrations("
            "source_locator, conversation_id, state, priority, "
            "source_fingerprint, migration_version, progress_json, "
            "error_digest, created_at_us, updated_at_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(source_locator) DO UPDATE SET "
            "conversation_id = excluded.conversation_id, "
            "priority = excluded.priority, "
            "source_fingerprint = excluded.source_fingerprint, "
            "migration_version = excluded.migration_version, "
            "progress_json = excluded.progress_json, "
            "state = CASE WHEN ? THEN chat_migrations.state "
            "ELSE excluded.state END, "
            "error_digest = CASE WHEN ? THEN chat_migrations.error_digest "
            "ELSE excluded.error_digest END, "
            "updated_at_us = excluded.updated_at_us",
            (
                f"chat:{canonical}",
                canonical,
                state,
                _priority(chat),
                fingerprint,
                MIGRATION_VERSION,
                json.dumps(progress, sort_keys=True),
                (
                    "ambiguous_session_path"
                    if conflicting
                    else "ambiguous_alias"
                    if alias_conflicting
                    else None
                ),
                now_us,
                now_us,
                preserve_state,
                preserve_state,
            ),
        )
    if not alias_conflicting:
        connection.execute(
            "INSERT INTO chat_alias_index("
            "agent_id, alias_namespace, external_key, owner_scope, "
            "conversation_id, created_at_us, updated_at_us) "
            "VALUES (?, 'legacy_session', ?, ?, ?, ?, ?) "
            "ON CONFLICT(agent_id, alias_namespace, external_key, "
            "owner_scope) DO UPDATE SET "
            "updated_at_us = excluded.updated_at_us "
            "WHERE chat_alias_index.conversation_id = "
            "excluded.conversation_id",
            (
                agent_id,
                chat.session_id,
                owner_scope,
                canonical,
                now_us,
                now_us,
            ),
        )


def inventory_legacy_workspace(
    workspace: Path,
    *,
    agent_id: str,
    now_us: int,
) -> LegacyInventoryReport:
    """Persist migration jobs using registry data and filesystem metadata."""
    chats_path = workspace / "chats.json"
    registry = _load_registry(chats_path)
    sessions_dir = workspace / "sessions"
    file_metadata = _iter_session_metadata(sessions_dir)
    path_owners: dict[str, list[str]] = defaultdict(list)
    alias_owners: dict[tuple[str, str], list[str]] = defaultdict(list)
    chat_sources: dict[str, list[str]] = {}
    invalid_chats: list[str] = []

    for chat in registry.chats:
        canonical = _canonical_uuid(chat.id)
        if canonical is None:
            invalid_chats.append(chat.id)
            continue
        candidates = session_relative_paths(
            chat.session_id,
            chat.user_id,
            chat.channel,
        )
        sources = sorted(
            candidate for candidate in candidates if candidate in file_metadata
        )
        chat_sources[chat.id] = sources
        alias_owners[(chat.session_id, _owner_scope(chat))].append(chat.id)
        for source in sources:
            path_owners[source].append(chat.id)

    ambiguous = {
        path: tuple(sorted(owners))
        for path, owners in path_owners.items()
        if len(set(owners)) > 1
    }
    ambiguous_aliases = {
        alias: tuple(sorted(owners))
        for alias, owners in alias_owners.items()
        if len(set(owners)) > 1
    }
    paths = ChatStoragePaths(workspace)
    paths.ensure_layout()
    CatalogRepository(paths.catalog)
    hot = 0
    cold = 0
    missing = 0
    catalog_ambiguous_aliases: set[tuple[str, str]] = set()

    with connect_database(paths.catalog) as connection:
        with immediate_transaction(connection):
            for ordinal, group in enumerate(registry.groups):
                connection.execute(
                    "INSERT INTO chat_groups("
                    "group_id, agent_id, name, ordinal, kind, pinned, "
                    "created_at_us, updated_at_us) VALUES (?, ?, ?, ?, ?, "
                    "?, ?, ?) ON CONFLICT(group_id) DO UPDATE SET "
                    "name = excluded.name, ordinal = excluded.ordinal, "
                    "kind = excluded.kind, pinned = excluded.pinned, "
                    "updated_at_us = excluded.updated_at_us",
                    (
                        group.id,
                        agent_id,
                        group.name,
                        ordinal,
                        group.kind.value,
                        int(group.pinned),
                        now_us,
                        now_us,
                    ),
                )
            for chat in registry.chats:
                canonical = _canonical_uuid(chat.id)
                if canonical is None or _is_deletion_tombstone(
                    connection,
                    canonical,
                ):
                    continue
                sources = chat_sources.get(chat.id, [])
                conflicting = sorted(
                    source for source in sources if source in ambiguous
                )
                alias_key = (chat.session_id, _owner_scope(chat))
                existing_alias = connection.execute(
                    "SELECT conversation_id FROM chat_alias_index "
                    "WHERE agent_id = ? AND alias_namespace = "
                    "'legacy_session' AND external_key = ? "
                    "AND owner_scope = ?",
                    (agent_id, chat.session_id, _owner_scope(chat)),
                ).fetchone()
                catalog_alias_conflict = bool(
                    existing_alias is not None
                    and str(existing_alias["conversation_id"]) != canonical,
                )
                if catalog_alias_conflict:
                    catalog_ambiguous_aliases.add(alias_key)
                _upsert_inventory_chat(
                    connection,
                    chat=chat,
                    canonical=canonical,
                    agent_id=agent_id,
                    owner_scope=_owner_scope(chat),
                    sources=sources,
                    conflicting=conflicting,
                    alias_conflicting=(
                        alias_key in ambiguous_aliases
                        or catalog_alias_conflict
                    ),
                    fingerprint=_fingerprint(sources, file_metadata),
                    now_us=now_us,
                )
                if chat.archived_at is None:
                    hot += 1
                else:
                    cold += 1
                if not sources:
                    missing += 1

    claimed_files = set(path_owners)
    orphan_files = tuple(sorted(set(file_metadata) - claimed_files))
    return LegacyInventoryReport(
        chats=len(registry.chats),
        hot=hot,
        cold=cold,
        missing_session=missing,
        orphan_files=orphan_files,
        ambiguous_files=tuple(sorted(ambiguous)),
        ambiguous_aliases=tuple(
            sorted(
                f"{owner_scope}:{session_id}"
                for session_id, owner_scope in (
                    set(ambiguous_aliases) | catalog_ambiguous_aliases
                )
            ),
        ),
        invalid_chats=tuple(sorted(invalid_chats)),
    )
