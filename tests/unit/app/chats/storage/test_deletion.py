# -*- coding: utf-8 -*-
"""Crash-resume tests for normalized Chat deletion."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from qwenpaw.app.chats.storage import (
    DELETE_PROJECTIONS,
    DELETE_TOMBSTONE,
    ChatStorageCoordinator,
    ChatStoragePaths,
    ConversationCreateRequest,
    begin_conversation_deletion,
    list_pending_deletions,
    project_conversation,
    purge_conversation_storage,
)
from qwenpaw.app.chats.storage.common import (
    connect_database,
    immediate_transaction,
)


def _conversation(tmp_path: Path) -> tuple[str, ChatStoragePaths]:
    paths = ChatStoragePaths(tmp_path / "workspace")
    conversation_id = str(uuid4())
    ChatStorageCoordinator(paths).create_conversation(
        ConversationCreateRequest(
            conversation_id=conversation_id,
            agent_id="agent",
            owner_scope="console:user",
            display_name="Chat",
            backend="qwenpaw",
            now_us=1,
        ),
    )
    with connect_database(paths.conversation(conversation_id)) as connection:
        with immediate_transaction(connection):
            connection.execute(
                "INSERT INTO chat_aliases("
                "alias_namespace, external_key, owner_scope, "
                "created_at_us, updated_at_us) VALUES "
                "('legacy_session', 'session', 'console:user', 2, 2)",
            )
            connection.execute(
                "UPDATE chat_meta SET source_generation = 2, "
                "write_seq = 2, updated_at_us = 2 "
                "WHERE singleton_id = 1",
            )
    project_conversation(paths, conversation_id, now_us=2)
    with connect_database(paths.catalog) as connection:
        with immediate_transaction(connection):
            connection.execute(
                "INSERT INTO chat_runtime_intents("
                "conversation_id, client_request_id, kind, "
                "state, created_at_us, updated_at_us) "
                "VALUES (?, 'request', 'run', 'recorded', 2, 2)",
                (conversation_id,),
            )
    return conversation_id, paths


def test_deletion_resumes_after_projection_cleanup_failure(
    tmp_path: Path,
) -> None:
    conversation_id, paths = _conversation(tmp_path)
    attachment = paths.workspace / "attachments" / "keep.bin"
    attachment.parent.mkdir()
    attachment.write_bytes(b"keep")
    target = begin_conversation_deletion(
        paths,
        conversation_id,
        spec_json='{"session_id":"session"}',
        now_us=3,
    )
    assert target is not None
    with connect_database(paths.catalog, readonly=True) as connection:
        lifecycle = connection.execute(
            "SELECT lifecycle FROM chat_catalog_entries "
            "WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()[0]
    assert lifecycle == "deleting"

    def fail_after_projection(step: int) -> None:
        if step == DELETE_PROJECTIONS:
            raise RuntimeError("injected crash")

    with pytest.raises(RuntimeError, match="injected crash"):
        purge_conversation_storage(
            paths,
            conversation_id,
            now_us=4,
            after_step=fail_after_projection,
        )

    pending = list_pending_deletions(paths)
    assert len(pending) == 1
    assert pending[0].completed_steps & DELETE_PROJECTIONS
    assert paths.conversation(conversation_id).is_file()

    purge_conversation_storage(paths, conversation_id, now_us=5)

    assert not paths.conversation(conversation_id).exists()
    assert attachment.read_bytes() == b"keep"
    assert not list_pending_deletions(paths)
    with connect_database(paths.catalog, readonly=True) as connection:
        entry = connection.execute(
            "SELECT lifecycle, spec_json "
            "FROM chat_catalog_entries WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        aliases = connection.execute(
            "SELECT COUNT(*) FROM chat_alias_index "
            "WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()[0]
        intents = connection.execute(
            "SELECT COUNT(*) FROM chat_runtime_intents "
            "WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()[0]
        job = connection.execute(
            "SELECT state, completed_steps FROM chat_deletion_jobs "
            "WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
    assert tuple(entry) == ("deleted", "{}")
    assert aliases == 0
    assert intents == 0
    assert job["state"] == "completed"
    assert int(job["completed_steps"]) & DELETE_TOMBSTONE
    with connect_database(paths.recall, readonly=True) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM chat_recall_projection_state "
                "WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()[0]
            == 0
        )


def test_begin_deletion_is_idempotent(tmp_path: Path) -> None:
    conversation_id, paths = _conversation(tmp_path)
    first = begin_conversation_deletion(
        paths,
        conversation_id,
        spec_json='{"session_id":"session"}',
        now_us=3,
    )
    second = begin_conversation_deletion(
        paths,
        conversation_id,
        spec_json=None,
        now_us=4,
    )

    assert first is not None
    assert second is not None
    assert second.spec_json == first.spec_json
    assert second.state == "pending"
