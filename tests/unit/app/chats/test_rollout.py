# -*- coding: utf-8 -*-
"""Rollout health counters and stop-condition coverage."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from qwenpaw.app.chats.rollout import read_chat_rollout_status
from qwenpaw.app.chats.storage import (
    ChatStorageCoordinator,
    ChatStoragePaths,
    ConversationCreateRequest,
    initialize_recall_database,
)
from qwenpaw.app.chats.storage.common import (
    connect_database,
    immediate_transaction,
)


def _ready_chat(tmp_path: Path) -> tuple[ChatStoragePaths, str]:
    paths = ChatStoragePaths(tmp_path)
    conversation_id = str(uuid4())
    ChatStorageCoordinator(paths).create_conversation(
        ConversationCreateRequest(
            conversation_id=conversation_id,
            agent_id="agent",
            owner_scope="console:user",
            display_name="Rollout",
            backend="qwenpaw",
            now_us=1,
        ),
    )
    return paths, conversation_id


def test_rollout_status_reports_projection_lag_as_warning(
    tmp_path: Path,
) -> None:
    paths, _conversation_id = _ready_chat(tmp_path)

    status = read_chat_rollout_status(paths)

    assert status.healthy is True
    assert status.lifecycle == {"ready": 1}
    assert not status.recall_projection_states
    assert status.recall_projection_lag == 1
    assert status.warnings == ("recall_projection_lag",)


def test_rollout_status_blocks_quarantine_and_terminal_migration(
    tmp_path: Path,
) -> None:
    paths, conversation_id = _ready_chat(tmp_path)
    with connect_database(paths.catalog) as connection:
        with immediate_transaction(connection):
            connection.execute(
                "UPDATE chat_catalog_entries SET lifecycle = 'quarantined' "
                "WHERE conversation_id = ?",
                (conversation_id,),
            )
            connection.execute(
                "INSERT INTO chat_migrations("
                "source_locator, conversation_id, state, "
                "migration_version, created_at_us, updated_at_us) "
                "VALUES ('legacy:test', ?, 'failed_terminal', 1, 1, 1)",
                (conversation_id,),
            )

    status = read_chat_rollout_status(paths)

    assert status.healthy is False
    assert status.stop_reasons == (
        "quarantined_chat",
        "terminal_migration_failure",
    )


def test_rollout_status_blocks_failed_recall_projection(
    tmp_path: Path,
) -> None:
    paths, conversation_id = _ready_chat(tmp_path)
    initialize_recall_database(paths.recall)
    with connect_database(paths.recall) as connection:
        with immediate_transaction(connection):
            connection.execute(
                "INSERT INTO chat_recall_projection_state("
                "conversation_id, source_generation, state, updated_at_us) "
                "VALUES (?, 1, 'failed', 1)",
                (conversation_id,),
            )

    status = read_chat_rollout_status(paths)

    assert status.healthy is False
    assert status.recall_projection_states == {"failed": 1}
    assert status.stop_reasons == ("recall_projection_failed",)
