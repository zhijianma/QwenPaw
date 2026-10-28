# -*- coding: utf-8 -*-
"""Partial block durability and terminal transaction tests."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from qwenpaw.app.chats.storage import (
    AcceptanceRequest,
    ChatStorageCoordinator,
    ChatStoragePaths,
    ConversationCreateRequest,
    PartialBlockMaterializer,
    RunFencingError,
    accept_input,
    append_completed_message,
    append_partial_block,
    begin_partial_text_block,
    finalize_run,
    read_transcript,
)
from qwenpaw.app.chats.storage.common import connect_database
from qwenpaw.schemas import Message, Role, RunStatus, TextContent


CONVERSATION_ID = "11111111-1111-4111-8111-111111111111"


def _accepted(tmp_path: Path):
    paths = ChatStoragePaths(tmp_path)
    ChatStorageCoordinator(paths).create_conversation(
        ConversationCreateRequest(
            conversation_id=CONVERSATION_ID,
            agent_id="agent",
            owner_scope="console:user",
            display_name="Streaming",
            backend="qwenpaw",
            now_us=1,
        ),
    )
    result = accept_input(
        paths.conversation(CONVERSATION_ID),
        AcceptanceRequest(
            conversation_id=CONVERSATION_ID,
            client_request_id="request",
            message=Message(
                role=Role.USER,
                content=[TextContent(text="prompt").completed()],
                status=RunStatus.Completed,
            ),
            process_instance_id="process",
            now_us=2,
        ),
        recall_sequences=(10,),
    )
    with connect_database(
        paths.conversation(CONVERSATION_ID),
        readonly=True,
    ) as connection:
        token = int(
            connection.execute(
                "SELECT fencing_token FROM chat_runs WHERE run_id = ?",
                (result.work_id,),
            ).fetchone()[0],
        )
    handle = begin_partial_text_block(
        paths.conversation(CONVERSATION_ID),
        conversation_id=CONVERSATION_ID,
        run_id=result.work_id,
        fencing_token=token,
        recall_seq=11,
        now_us=3,
    )
    return paths, handle


@pytest.mark.asyncio
async def test_partial_materializer_flushes_on_size_and_time_thresholds(
    tmp_path: Path,
) -> None:
    paths, handle = _accepted(tmp_path)
    path = paths.conversation(CONVERSATION_ID)
    materializer = PartialBlockMaterializer(
        path,
        handle,
        flush_interval=0.01,
        flush_bytes=5,
    )

    await materializer.append("hello", now_us=4)
    with connect_database(path, readonly=True) as connection:
        assert (
            connection.execute(
                "SELECT total_bytes FROM chat_blocks WHERE block_id = ?",
                (handle.block_id,),
            ).fetchone()[0]
            == 5
        )

    await materializer.append("!", now_us=5)
    await asyncio.sleep(0.03)
    with connect_database(path, readonly=True) as connection:
        block = connection.execute(
            "SELECT total_bytes, entity_version FROM chat_blocks "
            "WHERE block_id = ?",
            (handle.block_id,),
        ).fetchone()
        assert tuple(block) == (6, 2)

    generation = finalize_run(
        path,
        handle,
        state="completed",
        runtime_context=b'{"state":"saved"}',
        usage={"total_tokens": 7},
        now_us=6,
    )
    assert generation > 0
    restored = read_transcript(path)
    assert [message.content[0].text for message in restored] == [
        "prompt",
        "hello!",
    ]
    with connect_database(path, readonly=True) as connection:
        run = connection.execute(
            "SELECT state, durable_event_seq, durable_bytes FROM chat_runs",
        ).fetchone()
        assert tuple(run) == ("completed", 2, 6)
        turn = connection.execute(
            "SELECT state, usage_json, context_revision_id FROM chat_turns",
        ).fetchone()
        assert turn["state"] == "completed"
        assert turn["usage_json"] == '{"total_tokens":7}'
        assert turn["context_revision_id"] is not None


@pytest.mark.asyncio
async def test_partial_snapshot_keeps_inflight_and_buffer_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, handle = _accepted(tmp_path)
    path = paths.conversation(CONVERSATION_ID)
    materializer = PartialBlockMaterializer(
        path,
        handle,
        flush_interval=60,
        flush_bytes=1024,
    )
    entered = threading.Event()
    release = threading.Event()

    from qwenpaw.app.chats.storage import materializer as module

    original_append = module.append_partial_block

    def delayed_append(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=1)
        return original_append(*args, **kwargs)

    monkeypatch.setattr(module, "append_partial_block", delayed_append)

    first = await materializer.append("first", now_us=4)
    assert first.expected_offset == 0
    assert first.entity_version == 1
    flush = asyncio.create_task(materializer.flush(now_us=5))
    assert await asyncio.to_thread(entered.wait, 1)

    second = await materializer.append("second", now_us=6)
    inflight = await materializer.snapshot()
    assert second.expected_offset == 5
    assert second.entity_version == 2
    assert [segment.start_byte for segment in inflight.pending] == [0, 5]
    assert [segment.entity_version for segment in inflight.pending] == [1, 2]
    assert [segment.payload for segment in inflight.pending] == [
        b"first",
        b"second",
    ]

    release.set()
    assert await flush == 1
    after = await materializer.snapshot()
    assert after.durable_bytes == 5
    assert after.total_bytes == 11
    assert len(after.pending) == 1
    assert after.pending[0].start_byte == 5
    assert after.pending[0].entity_version == 2
    assert after.pending[0].payload == b"second"
    await materializer.close(now_us=7)


def test_partial_materializer_rejects_stale_fencing_token(
    tmp_path: Path,
) -> None:
    paths, handle = _accepted(tmp_path)

    with pytest.raises(RunFencingError, match="Stale fencing"):
        begin_partial_text_block(
            paths.conversation(CONVERSATION_ID),
            conversation_id=CONVERSATION_ID,
            run_id=handle.run_id,
            fencing_token=handle.fencing_token + 1,
            recall_seq=12,
            now_us=4,
        )


def test_completed_large_block_is_segmented(tmp_path: Path) -> None:
    paths, handle = _accepted(tmp_path)
    append_completed_message(
        paths.conversation(CONVERSATION_ID),
        handle,
        Message(
            id="large",
            role=Role.ASSISTANT,
            content=[TextContent(text="x" * 150_000).completed()],
            status=RunStatus.Completed,
        ),
        recall_sequences=[12],
        now_us=4,
    )

    with connect_database(
        paths.conversation(CONVERSATION_ID),
        readonly=True,
    ) as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS count, MAX(LENGTH(payload)) AS maximum "
            "FROM chat_block_segments WHERE block_id != ?",
            (handle.block_id,),
        ).fetchone()
    assert row["count"] >= 3
    assert row["maximum"] <= 64 * 1024


def test_large_partial_flush_is_segmented(tmp_path: Path) -> None:
    paths, handle = _accepted(tmp_path)
    payload = ("界" * 50_000).encode("utf-8")

    append_partial_block(
        paths.conversation(CONVERSATION_ID),
        handle,
        payload,
        now_us=4,
    )

    with connect_database(
        paths.conversation(CONVERSATION_ID),
        readonly=True,
    ) as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS count, MAX(LENGTH(payload)) AS maximum, "
            "SUM(LENGTH(payload)) AS total FROM chat_block_segments "
            "WHERE block_id = ?",
            (handle.block_id,),
        ).fetchone()
    assert row["count"] >= 3
    assert row["maximum"] <= 64 * 1024
    assert row["total"] == len(payload)
