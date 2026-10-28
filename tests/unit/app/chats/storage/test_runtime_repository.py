# -*- coding: utf-8 -*-
"""Durable Chat input acceptance and restart recovery tests."""

from __future__ import annotations

from pathlib import Path

from qwenpaw.app.chats.storage import (
    AcceptanceRequest,
    ChatStorageCoordinator,
    ChatStoragePaths,
    ConversationCreateRequest,
    accept_input,
    cancel_accepted_input,
    claim_queued_input,
    finalize_run,
    interrupt_nonterminal_work,
    load_runtime_run_handle,
)
from qwenpaw.app.chats.storage.common import connect_database
from qwenpaw.schemas import Message, Role, RunStatus, TextContent


CONVERSATION_ID = "11111111-1111-4111-8111-111111111111"


def _created(tmp_path: Path) -> ChatStoragePaths:
    paths = ChatStoragePaths(tmp_path)
    ChatStorageCoordinator(paths).create_conversation(
        ConversationCreateRequest(
            conversation_id=CONVERSATION_ID,
            agent_id="agent",
            owner_scope="console:user",
            display_name="Runtime",
            backend="qwenpaw",
            now_us=1,
        ),
    )
    return paths


def _request(
    request_id: str,
    now_us: int,
) -> AcceptanceRequest:
    return AcceptanceRequest(
        conversation_id=CONVERSATION_ID,
        client_request_id=request_id,
        message=Message(
            role=Role.USER,
            content=[TextContent(text=f"input:{request_id}").completed()],
            status=RunStatus.Completed,
        ),
        process_instance_id="process",
        now_us=now_us,
    )


def test_acceptance_is_idempotent_and_queues_behind_active_run(
    tmp_path: Path,
) -> None:
    paths = _created(tmp_path)
    path = paths.conversation(CONVERSATION_ID)
    first_request = _request("request-1", 2)

    first = accept_input(path, first_request, recall_sequences=(10,))
    replay = accept_input(path, first_request, recall_sequences=(11,))
    queued = accept_input(
        path,
        _request("request-2", 3),
        recall_sequences=(12,),
    )

    assert first.kind == "run"
    assert first.replayed is False
    assert replay.work_id == first.work_id
    assert replay.replayed is True
    assert queued.kind == "queue"
    assert queued.turn_id is not None
    with connect_database(path, readonly=True) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM chat_turns").fetchone()[0]
            == 2
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM chat_runs").fetchone()[0]
            == 1
        )
        queue = connection.execute(
            "SELECT client_request_id, turn_id, state "
            "FROM chat_queue_items",
        ).fetchone()
        assert tuple(queue) == ("request-2", queued.turn_id, "pending")
        blocks = connection.execute(
            "SELECT recall_seq FROM chat_blocks ORDER BY recall_seq",
        ).fetchall()
        assert [row["recall_seq"] for row in blocks] == [10, 12]


def test_restart_interrupts_runs_turns_and_queue_without_resuming(
    tmp_path: Path,
) -> None:
    paths = _created(tmp_path)
    path = paths.conversation(CONVERSATION_ID)
    accept_input(
        path,
        _request("request-1", 2),
        recall_sequences=(10,),
    )
    accept_input(
        path,
        _request("request-2", 3),
        recall_sequences=(11,),
    )

    recovered = interrupt_nonterminal_work(
        path,
        conversation_id=CONVERSATION_ID,
        now_us=4,
    )
    second = interrupt_nonterminal_work(
        path,
        conversation_id=CONVERSATION_ID,
        now_us=5,
    )

    assert recovered.runs == 1
    assert recovered.turns == 2
    assert recovered.queue_items == 1
    assert second.runs == 0
    with connect_database(path, readonly=True) as connection:
        assert (
            connection.execute("SELECT state FROM chat_runs").fetchone()[0]
            == "interrupted"
        )
        assert (
            connection.execute("SELECT state FROM chat_turns").fetchone()[0]
            == "interrupted"
        )
        assert (
            connection.execute(
                "SELECT state FROM chat_queue_items",
            ).fetchone()[0]
            == "interrupted"
        )


def test_oldest_queue_item_is_claimed_after_active_run_terminal(
    tmp_path: Path,
) -> None:
    paths = _created(tmp_path)
    path = paths.conversation(CONVERSATION_ID)
    first = accept_input(
        path,
        _request("request-1", 2),
        recall_sequences=(10,),
    )
    queued = accept_input(
        path,
        _request("request-2", 3),
        recall_sequences=(11,),
    )
    assert (
        claim_queued_input(
            path,
            conversation_id=CONVERSATION_ID,
            queue_item_id=queued.work_id,
            process_instance_id="process-2",
            now_us=4,
        )
        is None
    )
    handle = load_runtime_run_handle(
        path,
        conversation_id=CONVERSATION_ID,
        run_id=first.work_id,
    )
    finalize_run(
        path,
        handle,
        state="completed",
        runtime_context=None,
        usage=None,
        error_digest=None,
        now_us=5,
    )

    claimed = claim_queued_input(
        path,
        conversation_id=CONVERSATION_ID,
        queue_item_id=queued.work_id,
        process_instance_id="process-2",
        now_us=6,
    )
    replay = claim_queued_input(
        path,
        conversation_id=CONVERSATION_ID,
        queue_item_id=queued.work_id,
        process_instance_id="process-2",
        now_us=7,
    )

    assert claimed is not None
    assert claimed.kind == "run"
    assert claimed.turn_id == queued.turn_id
    assert replay is not None
    assert replay.work_id == claimed.work_id
    assert replay.replayed is True
    with connect_database(path, readonly=True) as connection:
        assert (
            connection.execute(
                "SELECT state FROM chat_queue_items",
            ).fetchone()[0]
            == "completed"
        )


def test_canceling_oldest_queue_item_unblocks_the_next_input(
    tmp_path: Path,
) -> None:
    paths = _created(tmp_path)
    path = paths.conversation(CONVERSATION_ID)
    first = accept_input(
        path,
        _request("request-1", 2),
        recall_sequences=(10,),
    )
    canceled = accept_input(
        path,
        _request("request-2", 3),
        recall_sequences=(11,),
    )
    following = accept_input(
        path,
        _request("request-3", 4),
        recall_sequences=(12,),
    )

    generation = cancel_accepted_input(
        path,
        conversation_id=CONVERSATION_ID,
        accepted=canceled,
        process_instance_id="process",
        now_us=5,
    )
    handle = load_runtime_run_handle(
        path,
        conversation_id=CONVERSATION_ID,
        run_id=first.work_id,
    )
    finalize_run(
        path,
        handle,
        state="completed",
        runtime_context=None,
        usage=None,
        error_digest=None,
        now_us=6,
    )
    claimed = claim_queued_input(
        path,
        conversation_id=CONVERSATION_ID,
        queue_item_id=following.work_id,
        process_instance_id="process",
        now_us=7,
    )

    assert generation is not None
    assert claimed is not None
    assert claimed.turn_id == following.turn_id
    with connect_database(path, readonly=True) as connection:
        states = {
            str(row["client_request_id"]): str(row["state"])
            for row in connection.execute(
                "SELECT client_request_id, state FROM chat_queue_items",
            )
        }
        turn_state = connection.execute(
            "SELECT state FROM chat_turns WHERE turn_id = ?",
            (canceled.turn_id,),
        ).fetchone()[0]
    assert states == {
        "request-2": "canceled",
        "request-3": "completed",
    }
    assert turn_state == "canceled"


def test_canceling_concurrently_claimed_queue_cancels_accepted_run(
    tmp_path: Path,
) -> None:
    paths = _created(tmp_path)
    path = paths.conversation(CONVERSATION_ID)
    first = accept_input(
        path,
        _request("request-1", 2),
        recall_sequences=(10,),
    )
    queued = accept_input(
        path,
        _request("request-2", 3),
        recall_sequences=(11,),
    )
    handle = load_runtime_run_handle(
        path,
        conversation_id=CONVERSATION_ID,
        run_id=first.work_id,
    )
    finalize_run(
        path,
        handle,
        state="completed",
        runtime_context=None,
        usage=None,
        error_digest=None,
        now_us=4,
    )
    claimed = claim_queued_input(
        path,
        conversation_id=CONVERSATION_ID,
        queue_item_id=queued.work_id,
        process_instance_id="process",
        now_us=5,
    )

    generation = cancel_accepted_input(
        path,
        conversation_id=CONVERSATION_ID,
        accepted=queued,
        process_instance_id="process",
        now_us=6,
    )

    assert claimed is not None
    assert generation is not None
    with connect_database(path, readonly=True) as connection:
        run_state = connection.execute(
            "SELECT state FROM chat_runs WHERE run_id = ?",
            (claimed.work_id,),
        ).fetchone()[0]
        turn_state = connection.execute(
            "SELECT state FROM chat_turns WHERE turn_id = ?",
            (queued.turn_id,),
        ).fetchone()[0]
    assert run_state == "canceled"
    assert turn_state == "canceled"


def test_canceling_direct_acceptance_closes_run_and_turn(
    tmp_path: Path,
) -> None:
    paths = _created(tmp_path)
    path = paths.conversation(CONVERSATION_ID)
    accepted = accept_input(
        path,
        _request("request-1", 2),
        recall_sequences=(10,),
    )

    generation = cancel_accepted_input(
        path,
        conversation_id=CONVERSATION_ID,
        accepted=accepted,
        process_instance_id="process",
        now_us=3,
    )

    assert generation is not None
    with connect_database(path, readonly=True) as connection:
        run_state = connection.execute(
            "SELECT state FROM chat_runs WHERE run_id = ?",
            (accepted.work_id,),
        ).fetchone()[0]
        turn_state = connection.execute(
            "SELECT state FROM chat_turns WHERE turn_id = ?",
            (accepted.turn_id,),
        ).fetchone()[0]
    assert run_state == "canceled"
    assert turn_state == "canceled"
