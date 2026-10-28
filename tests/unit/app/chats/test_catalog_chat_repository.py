# -*- coding: utf-8 -*-
"""ChatManager catalog repository cutover tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from qwenpaw.app.chats.application import ChatApplicationService
from qwenpaw.app.chats.manager import ChatManager
from qwenpaw.app.chats.models import ChatSpec, ChatUpdate, ChatsFile
from qwenpaw.app.chats.repo.catalog_repo import CatalogChatRepository
from qwenpaw.app.chats.storage import (
    CatalogStateConflictError,
    ChatStoragePaths,
)
from qwenpaw.app.chats.storage.common import connect_database


CONVERSATION_ID = "11111111-1111-4111-8111-111111111111"


async def test_catalog_repository_leaves_legacy_registry_unchanged(
    tmp_path,
) -> None:
    instant = datetime(2026, 1, 1, tzinfo=timezone.utc)
    legacy = ChatSpec(
        id=CONVERSATION_ID,
        name="Legacy name",
        session_id="console:legacy",
        user_id="user",
        channel="console",
        created_at=instant,
        updated_at=instant,
    )
    chats_path = tmp_path / "chats.json"
    chats_path.write_text(
        json.dumps(ChatsFile(chats=[legacy]).model_dump(mode="json")),
        encoding="utf-8",
    )
    original = chats_path.read_bytes()
    await ChatApplicationService(tmp_path).initialize(
        agent_id="agent",
        now_us=2,
    )
    manager = ChatManager(
        repo=CatalogChatRepository(
            tmp_path,
            agent_id="agent",
            backend="qwenpaw",
        ),
    )

    renamed = await manager.patch_chat(
        CONVERSATION_ID,
        ChatUpdate(name="Catalog name"),
    )

    assert renamed is not None
    assert renamed.name == "Catalog name"
    assert chats_path.read_bytes() == original
    assert (await manager.get_chat(CONVERSATION_ID)).name == "Catalog name"


async def test_catalog_repository_creates_new_chat_without_json_write(
    tmp_path,
) -> None:
    chats_path = tmp_path / "chats.json"
    chats_path.write_text(
        json.dumps(ChatsFile().model_dump(mode="json")),
        encoding="utf-8",
    )
    original = chats_path.read_bytes()
    await ChatApplicationService(tmp_path).initialize(
        agent_id="agent",
        now_us=1,
    )
    manager = ChatManager(
        repo=CatalogChatRepository(
            tmp_path,
            agent_id="agent",
            backend="qwenpaw",
        ),
    )

    chat = await manager.get_or_create_chat(
        "console:new",
        "user",
        "console",
    )

    paths = ChatStoragePaths(tmp_path)
    assert paths.conversation(chat.id).is_file()
    assert chats_path.read_bytes() == original
    with connect_database(paths.catalog, readonly=True) as connection:
        row = connection.execute(
            "SELECT lifecycle, spec_json FROM chat_catalog_entries "
            "WHERE conversation_id = ?",
            (chat.id,),
        ).fetchone()
        assert row["lifecycle"] == "ready"
        assert json.loads(row["spec_json"])["session_id"] == "console:new"


async def test_catalog_alias_claim_never_reassigns_an_existing_owner(
    tmp_path,
) -> None:
    instant = datetime(2026, 1, 1, tzinfo=timezone.utc)
    chats = ChatsFile(
        chats=[
            ChatSpec(
                id=CONVERSATION_ID,
                name="First",
                session_id="console:shared",
                user_id="user",
                channel="console",
                created_at=instant,
                updated_at=instant,
            ),
            ChatSpec(
                id="22222222-2222-4222-8222-222222222222",
                name="Second",
                session_id="console:shared",
                user_id="user",
                channel="console",
                created_at=instant,
                updated_at=instant,
            ),
        ],
    )
    repository = CatalogChatRepository(
        tmp_path,
        agent_id="agent",
        backend="qwenpaw",
    )

    with pytest.raises(CatalogStateConflictError, match="already owned"):
        await repository.save(chats)

    paths = ChatStoragePaths(tmp_path)
    with connect_database(paths.catalog, readonly=True) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM chat_alias_index",
            ).fetchone()[0]
            == 0
        )
    for chat in chats.chats:
        with connect_database(
            paths.conversation(chat.id),
            readonly=True,
        ) as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM chat_aliases",
                ).fetchone()[0]
                == 0
            )


async def test_alias_projection_failure_enqueues_durable_repair(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instant = datetime(2026, 1, 1, tzinfo=timezone.utc)
    chat = ChatSpec(
        id=CONVERSATION_ID,
        name="Repair",
        session_id="console:repair",
        user_id="user",
        channel="console",
        created_at=instant,
        updated_at=instant,
    )

    def fail_projection(*_args, **_kwargs) -> None:
        raise OSError("recall unavailable")

    monkeypatch.setattr(
        "qwenpaw.app.chats.repo.catalog_repo.project_conversation",
        fail_projection,
    )
    repository = CatalogChatRepository(
        tmp_path,
        agent_id="agent",
        backend="qwenpaw",
    )

    await repository.save(ChatsFile(chats=[chat]))

    with connect_database(
        ChatStoragePaths(tmp_path).catalog,
        readonly=True,
    ) as connection:
        repair = connection.execute(
            "SELECT state FROM chat_catalog_repairs",
        ).fetchone()
        assert repair["state"] == "pending"
