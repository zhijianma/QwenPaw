# -*- coding: utf-8 -*-
"""Catalog and recall projection tests."""

from __future__ import annotations

from pathlib import Path

from qwenpaw.agents.context.scroll.memoryspace import MemorySpace
from qwenpaw.app.chats.integrity import build_transcript_manifest
from qwenpaw.app.chats.migration import (
    NormalizedTranscript,
    create_legacy_snapshot,
)
from qwenpaw.app.chats.storage import (
    CatalogRepository,
    ChatStoragePaths,
    enqueue_projection_repair,
    materialize_normalized_transcript,
    project_conversation,
    repair_pending_projections,
    rebuild_all_recall_projections,
)
from qwenpaw.app.chats.storage.common import connect_database
from qwenpaw.schemas import Message, Role, RunStatus, TextContent


CONVERSATION_ID = "11111111-1111-4111-8111-111111111111"


def _prepare_conversation(tmp_path: Path) -> ChatStoragePaths:
    paths = ChatStoragePaths(tmp_path)
    paths.ensure_layout()
    catalog = CatalogRepository(paths.catalog)
    catalog.reserve_conversation(
        conversation_id=CONVERSATION_ID,
        agent_id="agent",
        owner_scope="console:user",
        display_name="Projection",
        physical_relpath=f"conversations/{CONVERSATION_ID}.db",
        now_us=1,
    )
    messages = [
        Message(
            role=Role.USER,
            content=[TextContent(text="alpha memory").completed()],
            status=RunStatus.Completed,
        ),
        Message(
            role=Role.ASSISTANT,
            content=[TextContent(text="beta answer").completed()],
            status=RunStatus.Completed,
        ),
    ]
    normalized = NormalizedTranscript(
        messages=tuple(messages),
        manifest=build_transcript_manifest(messages),
        history_parts=0,
        session_parts=2,
        deduplicated_parts=0,
    )
    snapshot = create_legacy_snapshot(
        tmp_path,
        conversation_id=CONVERSATION_ID,
        session_id="console:projection",
        source_paths=[],
    )
    materialize_normalized_transcript(
        paths.conversation(CONVERSATION_ID),
        conversation_id=CONVERSATION_ID,
        agent_id="agent",
        owner_scope="console:user",
        backend="qwenpaw",
        normalized=normalized,
        snapshot=snapshot,
        now_us=1,
        allocated_recall_sequences=(41, 42),
    )
    catalog.mark_ready(CONVERSATION_ID, now_us=2)
    return paths


def test_projection_is_searchable_idempotent_and_marks_outbox(
    tmp_path: Path,
) -> None:
    paths = _prepare_conversation(tmp_path)

    first = project_conversation(paths, CONVERSATION_ID, now_us=3)
    second = project_conversation(paths, CONVERSATION_ID, now_us=4)

    assert first == second
    assert first.recall_documents == 2
    with connect_database(paths.catalog, readonly=True) as connection:
        catalog = connection.execute(
            "SELECT source_generation FROM chat_catalog_entries",
        ).fetchone()
        alias = connection.execute(
            "SELECT source_generation FROM chat_alias_index",
        ).fetchone()
        assert tuple(catalog) == (1,)
        assert tuple(alias) == (1,)
    with connect_database(paths.recall, readonly=True) as connection:
        rows = connection.execute(
            "SELECT recall_seq, content FROM chat_recall_documents "
            "ORDER BY recall_seq",
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            (41, "alpha memory"),
            (42, "beta answer"),
        ]
        hits = connection.execute(
            "SELECT rowid FROM chat_recall_fts "
            "WHERE chat_recall_fts MATCH 'alpha'",
        ).fetchall()
        assert len(hits) == 1
    memory = MemorySpace(
        history_db_path=paths.recall,
        session_id=None,
        agent_id="agent",
    )
    try:
        assert memory.expand(41, 42)[0]["content"] == "alpha memory"
        assert memory.search("beta", include_turn=False)[0]["seq"] == 42
        assert memory.sessions()[0]["session_id"] == "console:projection"
    finally:
        memory.close()
    with connect_database(
        paths.conversation(CONVERSATION_ID),
        readonly=True,
    ) as connection:
        states = connection.execute(
            "SELECT DISTINCT state FROM chat_projection_outbox",
        ).fetchall()
        assert [row["state"] for row in states] == ["delivered"]


def test_rebuild_all_recall_uses_ready_catalog_entries(tmp_path: Path) -> None:
    paths = _prepare_conversation(tmp_path)

    results = rebuild_all_recall_projections(paths, now_us=3)

    assert len(results) == 1
    assert results[0].conversation_id == CONVERSATION_ID


def test_pending_projection_repair_is_durable_and_idempotent(
    tmp_path: Path,
) -> None:
    paths = _prepare_conversation(tmp_path)
    enqueue_projection_repair(
        paths,
        CONVERSATION_ID,
        expected_generation=1,
        now_us=3,
    )
    enqueue_projection_repair(
        paths,
        CONVERSATION_ID,
        expected_generation=1,
        now_us=4,
    )

    results = repair_pending_projections(paths, now_us=5)

    assert len(results) == 1
    assert results[0].source_generation == 1
    with connect_database(paths.catalog, readonly=True) as connection:
        repair = connection.execute(
            "SELECT state, attempt FROM chat_catalog_repairs",
        ).fetchone()
        assert tuple(repair) == ("completed", 1)
    with connect_database(
        paths.conversation(CONVERSATION_ID),
        readonly=True,
    ) as connection:
        states = connection.execute(
            "SELECT DISTINCT state FROM chat_projection_outbox",
        ).fetchall()
        assert [row["state"] for row in states] == ["delivered"]


def test_normal_projection_completes_superseded_repair_job(
    tmp_path: Path,
) -> None:
    paths = _prepare_conversation(tmp_path)
    enqueue_projection_repair(
        paths,
        CONVERSATION_ID,
        expected_generation=1,
        now_us=3,
    )

    project_conversation(paths, CONVERSATION_ID, now_us=4)

    with connect_database(paths.catalog, readonly=True) as connection:
        repair = connection.execute(
            "SELECT state, attempt FROM chat_catalog_repairs",
        ).fetchone()
    assert tuple(repair) == ("completed", 0)
