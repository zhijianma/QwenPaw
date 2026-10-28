# -*- coding: utf-8 -*-
"""Recoverable legacy Chat migration orchestration tests."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from qwenpaw.app.chats.migration import (
    LegacyMigrationCoordinator,
    inventory_legacy_workspace,
)
from qwenpaw.app.chats.application import ChatApplicationService
from qwenpaw.app.chats.models import ChatSpec, ChatsFile
from qwenpaw.app.chats.session import session_relative_paths
from qwenpaw.app.chats.storage import ChatStoragePaths
from qwenpaw.app.chats.storage.common import connect_database


CONVERSATION_ID = "11111111-1111-4111-8111-111111111111"


def _workspace(tmp_path: Path) -> Path:
    instant = datetime(2026, 1, 1, tzinfo=timezone.utc)
    chat = ChatSpec(
        id=CONVERSATION_ID,
        name="Migrated Chat",
        session_id="console:migrate",
        user_id="user",
        channel="console",
        created_at=instant,
        updated_at=instant,
    )
    payload = ChatsFile(chats=[chat]).model_dump(mode="json")
    (tmp_path / "chats.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    candidates = session_relative_paths(
        chat.session_id,
        chat.user_id,
        chat.channel,
    )
    relative = next(path for path in candidates if path.startswith("console/"))
    session = tmp_path / "sessions" / relative
    session.parent.mkdir(parents=True)
    session.write_text(
        json.dumps(
            {
                "agent": {
                    "state": {
                        "context": [
                            {
                                "id": "user-1",
                                "name": "user",
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": "hello"},
                                ],
                                "created_at": "2026-01-01T00:00:00",
                            },
                            {
                                "id": "assistant-1",
                                "name": "assistant",
                                "role": "assistant",
                                "content": [
                                    {"type": "text", "text": "world"},
                                ],
                                "created_at": "2026-01-01T00:00:01",
                            },
                        ],
                    },
                },
            },
        ),
        encoding="utf-8",
    )
    inventory_legacy_workspace(tmp_path, agent_id="agent", now_us=10)
    return tmp_path


def _catalog_state(workspace: Path) -> tuple[str, str, int]:
    with connect_database(
        ChatStoragePaths(workspace).catalog,
        readonly=True,
    ) as connection:
        row = connection.execute(
            "SELECT e.lifecycle, m.state, m.attempt "
            "FROM chat_catalog_entries AS e "
            "JOIN chat_migrations AS m "
            "ON m.conversation_id = e.conversation_id",
        ).fetchone()
    return str(row["lifecycle"]), str(row["state"]), int(row["attempt"])


@pytest.mark.asyncio
async def test_concurrent_callers_join_one_migration(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    coordinator = LegacyMigrationCoordinator(workspace)
    phases: list[str] = []

    first, second = await asyncio.gather(
        coordinator.migrate(CONVERSATION_ID, phase_hook=phases.append),
        coordinator.migrate(CONVERSATION_ID, phase_hook=phases.append),
    )

    assert first == second
    assert first.turns == 1
    assert first.messages == 2
    assert phases == [
        "snapshot",
        "normalize",
        "validate",
        "rename",
        "catalog_publish",
    ]
    assert _catalog_state(workspace) == ("ready", "ready_v2", 1)
    paths = ChatStoragePaths(workspace)
    with connect_database(paths.conversation(CONVERSATION_ID)) as connection:
        alias = connection.execute(
            "SELECT alias_namespace, external_key FROM chat_aliases",
        ).fetchone()
        assert tuple(alias) == ("legacy_session", "console:migrate")
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM chat_runtime_blobs",
            ).fetchone()[0]
            == 1
        )
    with connect_database(paths.recall, readonly=True) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM chat_recall_documents",
            ).fetchone()[0]
            == 2
        )
    runtime_context = await ChatApplicationService(
        workspace,
    ).load_runtime_context(CONVERSATION_ID)
    assert runtime_context is not None
    assert runtime_context["state"]["context"][0]["id"] == "user-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault_phase",
    ["snapshot", "normalize", "validate", "rename", "catalog_publish"],
)
async def test_migration_resumes_after_each_publish_boundary(
    tmp_path: Path,
    fault_phase: str,
) -> None:
    workspace = _workspace(tmp_path)
    coordinator = LegacyMigrationCoordinator(workspace)

    def fail_once(phase: str) -> None:
        if phase == fault_phase:
            raise RuntimeError(f"fault:{phase}")

    with pytest.raises(RuntimeError, match=f"fault:{fault_phase}"):
        await coordinator.migrate(
            CONVERSATION_ID,
            phase_hook=fail_once,
        )

    lifecycle, state, _attempt = _catalog_state(workspace)
    assert lifecycle == "creating"
    assert state == "failed_retryable"

    result = await coordinator.migrate(CONVERSATION_ID)

    assert result.messages == 2
    final_lifecycle, final_state, attempts = _catalog_state(workspace)
    assert (final_lifecycle, final_state) == ("ready", "ready_v2")
    assert attempts == 2


@pytest.mark.asyncio
async def test_projection_failure_does_not_roll_back_published_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)

    def fail_projection(*_args, **_kwargs) -> None:
        raise OSError("recall unavailable")

    monkeypatch.setattr(
        "qwenpaw.app.chats.migration.orchestrator.project_conversation",
        fail_projection,
    )

    result = await LegacyMigrationCoordinator(workspace).migrate(
        CONVERSATION_ID,
    )

    assert result.messages == 2
    assert _catalog_state(workspace) == ("ready", "ready_v2", 1)
    with connect_database(
        ChatStoragePaths(workspace).catalog,
        readonly=True,
    ) as connection:
        repair = connection.execute(
            "SELECT expected_generation, state FROM chat_catalog_repairs",
        ).fetchone()
        assert tuple(repair) == (1, "pending")
