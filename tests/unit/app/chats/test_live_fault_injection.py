# -*- coding: utf-8 -*-
"""Process-crash boundaries for normalized Chat live persistence."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
from pathlib import Path

import pytest

from qwenpaw.app.chats.application import ChatApplicationService
from qwenpaw.app.chats.storage import (
    AcceptanceRequest,
    ChatStorageCoordinator,
    ChatStoragePaths,
    ConversationCreateRequest,
    PartialBlockMaterializer,
    accept_input,
    begin_partial_text_block,
    finalize_run,
    load_runtime_run_handle,
    read_transcript,
)
from qwenpaw.app.chats.storage.common import connect_database
from qwenpaw.schemas import Message, Role, RunStatus, TextContent


CONVERSATION_ID = "11111111-1111-4111-8111-111111111111"
CRASH_EXIT_CODE = 97


def _crash_at_boundary(
    workspace: str,
    phase: str,
) -> None:
    paths = ChatStoragePaths(Path(workspace))
    service = ChatApplicationService(
        Path(workspace),
        process_instance_id="crashing-process",
    )
    service._record_intent(  # pylint: disable=protected-access
        CONVERSATION_ID,
        "request",
        2,
    )
    if phase == "before_accept_commit":
        os._exit(CRASH_EXIT_CODE)

    accepted = accept_input(
        paths.conversation(CONVERSATION_ID),
        AcceptanceRequest(
            conversation_id=CONVERSATION_ID,
            client_request_id="request",
            message=Message(
                role=Role.USER,
                content=[TextContent(text="accepted").completed()],
                status=RunStatus.Completed,
            ),
            process_instance_id="crashing-process",
            now_us=3,
        ),
        recall_sequences=(1,),
    )
    if phase == "after_accept_commit":
        os._exit(CRASH_EXIT_CODE)

    handle = load_runtime_run_handle(
        paths.conversation(CONVERSATION_ID),
        conversation_id=CONVERSATION_ID,
        run_id=accepted.work_id,
    )
    partial_handle = begin_partial_text_block(
        paths.conversation(CONVERSATION_ID),
        conversation_id=CONVERSATION_ID,
        run_id=accepted.work_id,
        fencing_token=handle.fencing_token,
        recall_seq=2,
        source_message_id="assistant",
        now_us=4,
    )

    async def append_partial() -> None:
        flush_bytes = 1024 if phase == "before_partial_threshold" else 1
        materializer = PartialBlockMaterializer(
            paths.conversation(CONVERSATION_ID),
            partial_handle,
            flush_interval=60,
            flush_bytes=flush_bytes,
        )
        await materializer.append("durable", now_us=5)

    asyncio.run(append_partial())
    if phase in {"before_partial_threshold", "after_partial_threshold"}:
        os._exit(CRASH_EXIT_CODE)
    if phase == "before_terminal_commit":
        os._exit(CRASH_EXIT_CODE)

    finalize_run(
        paths.conversation(CONVERSATION_ID),
        handle,
        state="completed",
        runtime_context=b'{"state":"saved"}',
        usage={"total_tokens": 1},
        now_us=6,
    )
    os._exit(CRASH_EXIT_CODE)


def _prepare_workspace(tmp_path: Path) -> None:
    ChatStorageCoordinator(
        ChatStoragePaths(tmp_path),
    ).create_conversation(
        ConversationCreateRequest(
            conversation_id=CONVERSATION_ID,
            agent_id="agent",
            owner_scope="console:user",
            display_name="Fault injection",
            backend="qwenpaw",
            now_us=1,
        ),
    )


def _run_crashing_process(
    tmp_path: Path,
    phase: str,
) -> None:
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_at_boundary,
        args=(str(tmp_path), phase),
    )
    process.start()
    process.join(timeout=15)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail(f"Fault process did not exit at {phase}")
    assert process.exitcode == CRASH_EXIT_CODE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "expected_recovered", "expected_run", "expected_bytes"),
    (
        ("before_accept_commit", 0, None, None),
        ("after_accept_commit", 1, "interrupted", None),
        ("before_partial_threshold", 1, "interrupted", 0),
        ("after_partial_threshold", 1, "interrupted", 7),
        ("before_terminal_commit", 1, "interrupted", 7),
        ("after_terminal_commit", 0, "completed", 7),
    ),
)
async def test_restart_reconciles_real_process_crash_boundaries(
    tmp_path: Path,
    phase: str,
    expected_recovered: int,
    expected_run: str | None,
    expected_bytes: int | None,
) -> None:
    paths = ChatStoragePaths(tmp_path)
    _prepare_workspace(tmp_path)
    _run_crashing_process(tmp_path, phase)

    recovered = await ChatApplicationService(
        tmp_path,
        process_instance_id="replacement-process",
    ).recover_after_restart(now_us=10)

    assert recovered == expected_recovered
    with connect_database(paths.catalog, readonly=True) as connection:
        intent_state = connection.execute(
            "SELECT state FROM chat_runtime_intents",
        ).fetchone()[0]
    assert intent_state == "terminal"
    with connect_database(
        paths.conversation(CONVERSATION_ID),
        readonly=True,
    ) as connection:
        run = connection.execute(
            "SELECT state FROM chat_runs",
        ).fetchone()
        block = connection.execute(
            "SELECT total_bytes, state FROM chat_blocks "
            "WHERE kind = 'text' AND message_id IN ("
            "SELECT message_id FROM chat_messages WHERE role = 'assistant')",
        ).fetchone()
    assert (str(run["state"]) if run is not None else None) == expected_run
    assert (int(block["total_bytes"]) if block is not None else None) == (
        expected_bytes
    )
    if phase != "before_accept_commit":
        transcript = read_transcript(paths.conversation(CONVERSATION_ID))
        assert transcript[0].content[0].text == "accepted"
    if phase == "after_partial_threshold":
        assert block["state"] == "interrupted"
    if phase == "after_terminal_commit":
        assert block["state"] == "completed"
        assert transcript[-1].content[0].text == "durable"
