# -*- coding: utf-8 -*-
"""Regression tests for context identity at completed turn boundaries."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from qwenpaw.app.chats.transcript import TranscriptStore
from qwenpaw.app.chats.transcript_catalog import TranscriptCatalog
from qwenpaw.app.chats.transcript_recorder import TranscriptRecorder
from qwenpaw.app.chats.session import DatabaseSession
from qwenpaw.hooks.session.session_hook import SessionSaveHook
from qwenpaw.hooks.session.signals import SESSION_SAVE_SUCCEEDED_KEY
from qwenpaw.schemas import AgentRequest, AgentResponse, RunStatus
from qwenpaw.schemas import Message, TextContent

IDENTITY = {
    "session_id": "session",
    "user_id": "user",
    "channel": "console",
}


def _saved_context(path, turn_id):
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT b.payload FROM turn_context_revision t "
            "JOIN context_revisions r ON r.session_id = t.session_id "
            "AND r.revision_id = t.revision_id "
            "JOIN context_blobs b ON b.content_hash = r.content_hash "
            "WHERE t.turn_id = ?",
            (turn_id,),
        ).fetchone()
    return json.loads(row[0]) if row else None


def test_turn_context_survives_clear_and_later_saves(tmp_path):
    path = tmp_path / "session.db"
    store = TranscriptStore(path)
    try:
        store.start_turn(**IDENTITY, turn_id="first")
        store.write_runtime_state(
            **IDENTITY,
            turn_id="first",
            state={"agent": {"summary": "first context"}},
        )
        assert _saved_context(path, "first") is None
        store.finish_turn(
            session_id="session",
            turn_id="first",
            status="completed",
        )
        store.write_runtime_state(
            **IDENTITY,
            state={"agent": {}},
            reset_context=True,
        )
        store.start_turn(**IDENTITY, turn_id="second")
        store.write_runtime_state(
            **IDENTITY,
            turn_id="second",
            state={"agent": {"new": True}},
        )
        store.finish_turn(
            session_id="session",
            turn_id="second",
            status="completed",
        )
        assert _saved_context(path, "first") == {
            "agent": {"summary": "first context"},
        }
        assert _saved_context(path, "second") == {"agent": {"new": True}}
    finally:
        store.close()


def test_missing_turn_snapshot_does_not_reuse_current_context(tmp_path):
    path = tmp_path / "session.db"
    store = TranscriptStore(path)
    try:
        store.write_runtime_state(**IDENTITY, state={"agent": {"old": True}})
        store.start_turn(**IDENTITY, turn_id="unsaved")
        store.finish_turn(
            session_id="session",
            turn_id="unsaved",
            status="completed",
        )
        assert _saved_context(path, "unsaved") is None
        with pytest.raises(ValueError, match="running turn"):
            store.write_runtime_state(
                **IDENTITY,
                turn_id="unsaved",
                state={"agent": {}},
            )
        snapshot = store.read_runtime_state(**IDENTITY)
        assert snapshot.state == {"agent": {"old": True}}
    finally:
        store.close()


def test_unsupported_development_schema_is_not_modified(tmp_path):
    path = tmp_path / "session.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE old_layout (value TEXT)")
        connection.execute("INSERT INTO old_layout VALUES ('preserve')")
    with pytest.raises(ValueError, match="unsupported conversation"):
        TranscriptStore(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM old_layout").fetchone() == (
            "preserve",
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'",
            ).fetchone()[0]
            == 1
        )


@pytest.mark.asyncio
async def test_real_session_hook_records_context_before_terminal(tmp_path):
    catalog = TranscriptCatalog(tmp_path)
    session = DatabaseSession(
        catalog=catalog,
        legacy_save_dir=str(tmp_path / "sessions"),
    )
    request = AgentRequest(**IDENTITY, input=[])
    recorder = TranscriptRecorder(store=catalog, request=request)
    ctx = SimpleNamespace(
        request=request,
        workspace=SimpleNamespace(session=session),
        agent=SimpleNamespace(state_dict=lambda: {"summary": "saved"}),
        session_id="session",
        mode_state={},
        extras={},
    )
    try:
        await recorder.start()
        await SessionSaveHook().run(ctx)
        assert ctx.extras[SESSION_SAVE_SUCCEEDED_KEY]
        await recorder.observe(AgentResponse(status=RunStatus.Completed))
        path = next((tmp_path / "transcripts").rglob("*.db"))
        state = _saved_context(
            path,
            request.request_context["_qwenpaw_transcript_turn_id"],
        )
        assert state["agent"]["summary"] == "saved"
    finally:
        catalog.close()


def test_copy_uses_historical_context_and_excludes_future_turns(tmp_path):
    catalog = TranscriptCatalog(tmp_path)
    try:
        for number in (1, 2):
            turn_id = f"turn-{number}"
            catalog.start_turn(**IDENTITY, turn_id=turn_id)
            catalog.upsert_message(
                session_id="session",
                turn_id=turn_id,
                ordinal=0,
                message=Message(
                    id=f"user-{number}",
                    role="user",
                    content=[TextContent(text=f"question {number}")],
                ).completed(),
            )
            catalog.write_runtime_state(
                **IDENTITY,
                turn_id=turn_id,
                state={"agent": {"turn": number}},
            )
            catalog.finish_turn(
                session_id="session",
                turn_id=turn_id,
                status="completed",
            )
        assert catalog.clone_session(
            source_session_id="session",
            source_user_id="user",
            source_channel="console",
            target_session_id="copy",
            target_user_id="user",
            target_channel="console",
            source_turn_id="turn-1",
        )
        target = dict(IDENTITY, session_id="copy")
        assert catalog.read_runtime_state(**target).state == {
            "agent": {"turn": 1},
        }
        page = catalog.get_page(**target)
        assert [message.id for message in page.messages] == ["user-1"]
        assert catalog.read_turn_context(**target, turn_id="turn-1").state == {
            "agent": {"turn": 1},
        }
        catalog.delete_session("session")
        assert catalog.read_runtime_state(**target).state == {
            "agent": {"turn": 1},
        }
    finally:
        catalog.close()
