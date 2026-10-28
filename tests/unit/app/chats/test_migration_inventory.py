# -*- coding: utf-8 -*-
"""Metadata-only legacy Chat inventory tests."""

from __future__ import annotations

import importlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from qwenpaw.app.chats.migration import inventory_legacy_workspace
from qwenpaw.app.chats.models import ChatSpec, ChatsFile
from qwenpaw.app.chats.session import session_relative_paths
from qwenpaw.app.chats.storage import CatalogRepository, ChatStoragePaths
from qwenpaw.app.chats.storage.common import connect_database


CHAT_A = "11111111-1111-4111-8111-111111111111"
CHAT_B = "22222222-2222-4222-8222-222222222222"


def _chat(
    chat_id: str,
    *,
    session_id: str,
    archived: bool = False,
) -> ChatSpec:
    instant = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return ChatSpec(
        id=chat_id,
        name=f"Chat {chat_id[0]}",
        session_id=session_id,
        user_id="user",
        channel="console",
        created_at=instant,
        updated_at=instant,
        archived_at=instant if archived else None,
    )


def _write_registry(workspace: Path, chats: list[ChatSpec]) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    payload = ChatsFile(chats=chats).model_dump(mode="json")
    (workspace / "chats.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def _session_path(workspace: Path, chat: ChatSpec) -> Path:
    candidates = session_relative_paths(
        chat.session_id,
        chat.user_id,
        chat.channel,
    )
    relative = next(path for path in candidates if path.startswith("console/"))
    path = workspace / "sessions" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_inventory_does_not_parse_session_transcript_json(
    tmp_path: Path,
) -> None:
    chat = _chat(CHAT_A, session_id="console:one")
    _write_registry(tmp_path, [chat])
    session = _session_path(tmp_path, chat)
    session.write_text(
        "{ deliberately invalid and potentially huge",
        encoding="utf-8",
    )

    report = inventory_legacy_workspace(
        tmp_path,
        agent_id="agent",
        now_us=10,
    )

    assert report.chats == 1
    assert report.hot == 1
    assert report.missing_session == 0
    paths = ChatStoragePaths(tmp_path)
    with connect_database(paths.catalog, readonly=True) as connection:
        row = connection.execute(
            "SELECT state, progress_json FROM chat_migrations",
        ).fetchone()
        assert row["state"] == "legacy_only"
        progress = json.loads(row["progress_json"])
        assert progress["sources"] == [
            session.relative_to(tmp_path / "sessions").as_posix(),
        ]


def test_inventory_prioritizes_active_chats_and_reports_orphans(
    tmp_path: Path,
) -> None:
    active = _chat(CHAT_A, session_id="console:active")
    archived = _chat(CHAT_B, session_id="console:archived", archived=True)
    _write_registry(tmp_path, [active, archived])
    _session_path(tmp_path, active).write_text("{}", encoding="utf-8")
    orphan = tmp_path / "sessions" / "orphan.json"
    orphan.write_text("{}", encoding="utf-8")

    report = inventory_legacy_workspace(
        tmp_path,
        agent_id="agent",
        now_us=10,
    )

    assert report.hot == 1
    assert report.cold == 1
    assert report.missing_session == 1
    assert report.orphan_files == ("orphan.json",)
    with connect_database(
        ChatStoragePaths(tmp_path).catalog,
        readonly=True,
    ) as connection:
        rows = connection.execute(
            "SELECT conversation_id, priority FROM chat_migrations "
            "ORDER BY priority DESC",
        ).fetchall()
        assert [row["conversation_id"] for row in rows] == [CHAT_A, CHAT_B]


def test_inventory_refuses_ambiguous_session_path(tmp_path: Path) -> None:
    first = _chat(CHAT_A, session_id="console:shared")
    second = _chat(CHAT_B, session_id="console:shared")
    _write_registry(tmp_path, [first, second])
    session = _session_path(tmp_path, first)
    session.write_text("{}", encoding="utf-8")

    report = inventory_legacy_workspace(
        tmp_path,
        agent_id="agent",
        now_us=10,
    )

    relative = session.relative_to(tmp_path / "sessions").as_posix()
    assert report.ambiguous_files == (relative,)
    with connect_database(
        ChatStoragePaths(tmp_path).catalog,
        readonly=True,
    ) as connection:
        rows = connection.execute(
            "SELECT state, error_digest FROM chat_migrations",
        ).fetchall()
        assert len(rows) == 2
        assert {row["state"] for row in rows} == {"failed_terminal"}
        assert {row["error_digest"] for row in rows} == {
            "ambiguous_session_path",
        }


def test_inventory_never_demotes_published_v2_after_legacy_drift(
    tmp_path: Path,
) -> None:
    chat = _chat(CHAT_A, session_id="console:one")
    _write_registry(tmp_path, [chat])
    session = _session_path(tmp_path, chat)
    session.write_text("{}", encoding="utf-8")
    inventory_legacy_workspace(tmp_path, agent_id="agent", now_us=10)
    catalog_path = ChatStoragePaths(tmp_path).catalog
    with connect_database(catalog_path) as connection:
        connection.execute(
            "UPDATE chat_migrations SET state = 'ready_v2'",
        )
        connection.execute(
            "UPDATE chat_catalog_entries SET lifecycle = 'ready'",
        )
        connection.commit()
    inventory_legacy_workspace(tmp_path, agent_id="agent", now_us=20)
    with connect_database(catalog_path, readonly=True) as connection:
        row = connection.execute(
            "SELECT state, source_fingerprint, updated_at_us "
            "FROM chat_migrations",
        ).fetchone()
        original_fingerprint = str(row["source_fingerprint"])
        assert (row["state"], row["updated_at_us"]) == ("ready_v2", 20)

    session.write_text("changed", encoding="utf-8")
    inventory_legacy_workspace(tmp_path, agent_id="agent", now_us=30)
    with connect_database(catalog_path, readonly=True) as connection:
        row = connection.execute(
            "SELECT state, source_fingerprint, updated_at_us "
            "FROM chat_migrations",
        ).fetchone()
        assert (row["state"], row["updated_at_us"]) == ("ready_v2", 30)
        assert row["source_fingerprint"] == original_fingerprint

    with connect_database(catalog_path) as connection:
        connection.execute(
            "UPDATE chat_migrations SET state = 'legacy_only'",
        )
        connection.commit()
    inventory_legacy_workspace(tmp_path, agent_id="agent", now_us=40)
    with connect_database(catalog_path, readonly=True) as connection:
        row = connection.execute(
            "SELECT state, source_fingerprint, updated_at_us "
            "FROM chat_migrations",
        ).fetchone()
        assert (row["state"], row["updated_at_us"]) == ("ready_v2", 40)
        assert row["source_fingerprint"] == original_fingerprint


def test_inventory_preserves_quarantine_without_blocking_startup(
    tmp_path: Path,
) -> None:
    chat = _chat(CHAT_A, session_id="console:quarantined")
    _write_registry(tmp_path, [chat])
    _session_path(tmp_path, chat).write_text("{}", encoding="utf-8")
    inventory_legacy_workspace(tmp_path, agent_id="agent", now_us=10)
    catalog_path = ChatStoragePaths(tmp_path).catalog
    with connect_database(catalog_path) as connection:
        connection.execute(
            "UPDATE chat_catalog_entries SET lifecycle = 'quarantined' "
            "WHERE conversation_id = ?",
            (CHAT_A,),
        )
        connection.commit()

    report = inventory_legacy_workspace(
        tmp_path,
        agent_id="agent",
        now_us=20,
    )

    assert report.chats == 1
    with connect_database(catalog_path, readonly=True) as connection:
        lifecycle = connection.execute(
            "SELECT lifecycle FROM chat_catalog_entries "
            "WHERE conversation_id = ?",
            (CHAT_A,),
        ).fetchone()[0]
    assert lifecycle == "quarantined"

def test_inventory_does_not_add_migration_row_to_native_v2(
    tmp_path: Path,
) -> None:
    chat = _chat(CHAT_A, session_id="console:native")
    _write_registry(tmp_path, [chat])
    _session_path(tmp_path, chat).write_text("{}", encoding="utf-8")
    inventory_legacy_workspace(tmp_path, agent_id="agent", now_us=10)
    catalog_path = ChatStoragePaths(tmp_path).catalog
    with connect_database(catalog_path) as connection:
        connection.execute("DELETE FROM chat_migrations")
        connection.execute(
            "UPDATE chat_catalog_entries SET lifecycle = 'ready'",
        )
        connection.commit()

    inventory_legacy_workspace(tmp_path, agent_id="agent", now_us=20)

    with connect_database(catalog_path, readonly=True) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM chat_migrations",
        ).fetchone()[0]
    assert count == 0


def test_inventory_never_resurrects_deletion_tombstone(
    tmp_path: Path,
) -> None:
    chat = _chat(CHAT_A, session_id="console:deleted")
    _write_registry(tmp_path, [chat])
    _session_path(tmp_path, chat).write_text("{}", encoding="utf-8")
    inventory_legacy_workspace(tmp_path, agent_id="agent", now_us=10)
    catalog = ChatStoragePaths(tmp_path).catalog
    with connect_database(catalog) as connection:
        connection.execute(
            "UPDATE chat_catalog_entries SET lifecycle = 'deleted' "
            "WHERE conversation_id = ?",
            (CHAT_A,),
        )
        connection.execute(
            "DELETE FROM chat_alias_index WHERE conversation_id = ?",
            (CHAT_A,),
        )
        connection.execute(
            "DELETE FROM chat_migrations WHERE conversation_id = ?",
            (CHAT_A,),
        )
        connection.commit()

    inventory_legacy_workspace(tmp_path, agent_id="agent", now_us=20)

    with connect_database(catalog, readonly=True) as connection:
        lifecycle = connection.execute(
            "SELECT lifecycle FROM chat_catalog_entries "
            "WHERE conversation_id = ?",
            (CHAT_A,),
        ).fetchone()[0]
        aliases = connection.execute(
            "SELECT COUNT(*) FROM chat_alias_index "
            "WHERE conversation_id = ?",
            (CHAT_A,),
        ).fetchone()[0]
        migrations = connection.execute(
            "SELECT COUNT(*) FROM chat_migrations "
            "WHERE conversation_id = ?",
            (CHAT_A,),
        ).fetchone()[0]
    assert lifecycle == "deleted"
    assert aliases == 0
    assert migrations == 0


def test_inventory_refuses_duplicate_alias_without_session_files(
    tmp_path: Path,
) -> None:
    first = _chat(CHAT_A, session_id="console:shared")
    second = _chat(CHAT_B, session_id="console:shared")
    _write_registry(tmp_path, [first, second])

    report = inventory_legacy_workspace(
        tmp_path,
        agent_id="agent",
        now_us=10,
    )

    assert not report.ambiguous_files
    assert report.ambiguous_aliases == ("console:user:console:shared",)
    with connect_database(
        ChatStoragePaths(tmp_path).catalog,
        readonly=True,
    ) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM chat_alias_index",
            ).fetchone()[0]
            == 0
        )
        rows = connection.execute(
            "SELECT state, error_digest FROM chat_migrations",
        ).fetchall()
        assert {tuple(row) for row in rows} == {
            ("failed_terminal", "ambiguous_alias"),
        }


def test_inventory_never_steals_an_existing_catalog_alias(
    tmp_path: Path,
) -> None:
    stale = _chat(CHAT_A, session_id="console:shared")
    _write_registry(tmp_path, [stale])
    paths = ChatStoragePaths(tmp_path)
    paths.ensure_layout()
    catalog = CatalogRepository(paths.catalog)
    catalog.reserve_conversation(
        conversation_id=CHAT_B,
        agent_id="agent",
        owner_scope="console:user",
        display_name="Current",
        physical_relpath=f"conversations/{CHAT_B}.db",
        now_us=1,
    )
    with connect_database(paths.catalog) as connection:
        connection.execute(
            "INSERT INTO chat_alias_index("
            "agent_id, alias_namespace, external_key, owner_scope, "
            "conversation_id, created_at_us, updated_at_us) "
            "VALUES ('agent', 'legacy_session', 'console:shared', "
            "'console:user', ?, 1, 1)",
            (CHAT_B,),
        )

    report = inventory_legacy_workspace(
        tmp_path,
        agent_id="agent",
        now_us=10,
    )

    assert report.ambiguous_aliases == ("console:user:console:shared",)
    with connect_database(paths.catalog, readonly=True) as connection:
        owner = connection.execute(
            "SELECT conversation_id FROM chat_alias_index",
        ).fetchone()[0]
        migration = connection.execute(
            "SELECT state, error_digest FROM chat_migrations "
            "WHERE conversation_id = ?",
            (CHAT_A,),
        ).fetchone()
    assert owner == CHAT_B
    assert tuple(migration) == ("failed_terminal", "ambiguous_alias")


def test_inventory_batches_catalog_writes_in_one_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chats = [
        _chat(
            f"{index:08x}-1111-4111-8111-111111111111",
            session_id=f"console:{index}",
        )
        for index in range(1, 51)
    ]
    _write_registry(tmp_path, chats)
    inventory_module = importlib.import_module(
        "qwenpaw.app.chats.migration.inventory",
    )
    original = inventory_module.connect_database
    opened = 0

    def counted_connect(
        path: Path,
        *,
        readonly: bool = False,
    ) -> sqlite3.Connection:
        nonlocal opened
        opened += 1
        return original(path, readonly=readonly)

    monkeypatch.setattr(inventory_module, "connect_database", counted_connect)

    report = inventory_legacy_workspace(
        tmp_path,
        agent_id="agent",
        now_us=10,
    )

    assert report.chats == 50
    assert report.missing_session == 50
    assert opened == 1
