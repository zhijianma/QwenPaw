# -*- coding: utf-8 -*-
"""Tests for the workspace-owned interaction infrastructure."""

import asyncio
import sqlite3
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from qwenpaw.interactions import (
    InteractionConflictError,
    InteractionIdempotencyConflictError,
    InteractionRevisionConflictError,
    InteractionService,
)
from qwenpaw.kernel import (
    ActorRef,
    ActorType,
    ContinuationAvailability,
    ContinuationDispatchStatus,
    ContinuationMode,
    ConversationContinuationPort,
    InteractionKind,
    InteractionHistoryPort,
    InteractionMode,
    InteractionOption,
    InteractionPolicyError,
    InteractionRequest,
    InteractionResponse,
    InteractionStatus,
    InteractionPort,
    UserInputReason,
    WaitConditionStatus,
    WaitConditionProjectionPort,
)


def _request(
    invocation_id: UUID,
    *,
    kind: InteractionKind = InteractionKind.APPROVAL,
    mode: InteractionMode = InteractionMode.BLOCKING,
) -> InteractionRequest:
    return InteractionRequest(
        kind=kind,
        mode=mode,
        agent_id="default",
        conversation_id="chat-spec-1",
        invocation_id=invocation_id,
        user_input_reason=(
            UserInputReason.MISSING_REQUIRED_FACT
            if kind is InteractionKind.USER_INPUT
            else None
        ),
        title="Runtime needs a decision",
        prompt="Choose how to continue.",
        options=(
            InteractionOption(option_id="approve", label="Approve"),
            InteractionOption(option_id="deny", label="Deny"),
        ),
    )


def _response(
    request: InteractionRequest,
    *,
    key: str = "response-1",
    option: str = "approve",
    revision: int = 1,
) -> InteractionResponse:
    return InteractionResponse(
        interaction_id=request.interaction_id,
        idempotency_key=key,
        expected_revision=revision,
        actor=ActorRef(type=ActorType.USER, id="local-user"),
        selected_option_ids=(option,),
    )


@pytest.mark.asyncio
async def test_service_rejects_unclassified_user_input_before_storage(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "interactions.sqlite3"
    service = InteractionService(database_path)
    request = InteractionRequest(
        kind=InteractionKind.USER_INPUT,
        mode=InteractionMode.BLOCKING,
        agent_id="default",
        conversation_id="chat-spec-1",
        invocation_id=uuid4(),
        title="Generic confirmation",
        prompt="Continue?",
    )

    with pytest.raises(InteractionPolicyError):
        await service.open(request)

    assert not database_path.exists()


@pytest.mark.asyncio
async def test_schema_v1_migrates_to_continuation_outbox(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "interactions.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA user_version = 1")

    service = InteractionService(database_path)
    await service.start()

    with sqlite3.connect(database_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        table = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = ?",
            ("interaction_continuations",),
        ).fetchone()

    assert version == 2
    assert table == ("interaction_continuations",)


@pytest.mark.asyncio
async def test_legacy_unclassified_user_input_remains_readable(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "interactions.sqlite3"
    service = InteractionService(database_path)
    await service.start()
    legacy = InteractionRequest(
        kind=InteractionKind.USER_INPUT,
        mode=InteractionMode.BLOCKING,
        agent_id="default",
        conversation_id="chat-spec-1",
        invocation_id=uuid4(),
        title="Legacy input",
        prompt="Legacy question",
    )
    request_json = legacy.model_dump_json(
        by_alias=True,
        exclude={"user_input_reason"},
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO runtime_interactions "
            "(interaction_id, agent_id, conversation_id, invocation_id, "
            "mode, status, revision, request_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(legacy.interaction_id),
                legacy.agent_id,
                legacy.conversation_id,
                str(legacy.invocation_id),
                legacy.mode.value,
                legacy.status.value,
                legacy.revision,
                request_json,
                legacy.created_at.isoformat(),
                legacy.created_at.isoformat(),
            ),
        )

    recovered = await service.get_request(legacy.interaction_id)

    assert recovered is not None
    assert recovered.user_input_reason is None


@pytest.mark.asyncio
async def test_parallel_interactions_are_owned_by_chat_spec(
    tmp_path: Path,
) -> None:
    service = InteractionService(tmp_path / "interactions.sqlite3")
    assert isinstance(service, InteractionPort)
    assert isinstance(service, InteractionHistoryPort)
    assert isinstance(service, WaitConditionProjectionPort)
    assert isinstance(service, ConversationContinuationPort)
    invocation_id = uuid4()
    first = _request(invocation_id)
    second = _request(
        invocation_id,
        kind=InteractionKind.USER_INPUT,
    )

    await asyncio.gather(service.open(first), service.open(second))
    open_items = await service.list_open(
        agent_id="default",
        conversation_id="chat-spec-1",
    )

    assert {item.interaction_id for item in open_items} == {
        first.interaction_id,
        second.interaction_id,
    }
    assert all(item.conversation_id == "chat-spec-1" for item in open_items)
    assert all("session_id" not in item.model_dump() for item in open_items)


@pytest.mark.asyncio
async def test_resolve_wakes_waiter_and_replays_same_response(
    tmp_path: Path,
) -> None:
    service = InteractionService(tmp_path / "interactions.sqlite3")
    request = _request(uuid4())
    await service.open(request)
    waiter = asyncio.create_task(service.wait(request.interaction_id))
    await asyncio.sleep(0)

    response = _response(request)
    resolution = await service.resolve(response)
    replay = await service.resolve(response)

    assert await waiter == resolution
    assert replay == resolution
    assert resolution.status is InteractionStatus.RESOLVED

    [record] = await service.list_for_conversation(
        agent_id="default",
        conversation_id="chat-spec-1",
    )
    assert record.request == request
    assert record.resolution == resolution


@pytest.mark.asyncio
async def test_conversation_response_creates_idempotent_outbox(
    tmp_path: Path,
) -> None:
    service = InteractionService(tmp_path / "interactions.sqlite3")
    request = _request(
        uuid4(),
        kind=InteractionKind.USER_INPUT,
    ).model_copy(
        update={
            "continuation_mode": ContinuationMode.CONVERSATION_TURN,
        },
    )
    await service.open(request)
    response = _response(request, option="approve")

    resolution = await service.resolve(response)
    replay = await service.resolve(response)
    ready = await service.list_ready_continuations(agent_id="default")

    assert replay == resolution
    assert len(ready) == 1
    assert ready[0].interaction_id == request.interaction_id
    assert ready[0].response_revision == resolution.revision
    assert ready[0].status is ContinuationDispatchStatus.READY

    submission_id = uuid4()
    dispatched = await service.mark_continuation_dispatched(
        request.interaction_id,
        submission_id,
    )
    replayed = await service.mark_continuation_dispatched(
        request.interaction_id,
        submission_id,
    )

    assert replayed == dispatched
    assert dispatched.submission_id == submission_id
    assert dispatched.status is ContinuationDispatchStatus.DISPATCHED
    assert not await service.list_ready_continuations(agent_id="default")

    with pytest.raises(
        InteractionConflictError,
        match="another submission",
    ):
        await service.mark_continuation_dispatched(
            request.interaction_id,
            uuid4(),
        )


@pytest.mark.asyncio
async def test_response_enforces_revision_options_and_idempotency(
    tmp_path: Path,
) -> None:
    service = InteractionService(tmp_path / "interactions.sqlite3")
    request = _request(uuid4())
    await service.open(request)

    with pytest.raises(InteractionRevisionConflictError):
        await service.resolve(_response(request, revision=2))
    with pytest.raises(InteractionConflictError, match="unknown"):
        await service.resolve(_response(request, option="unknown"))

    await service.resolve(_response(request))
    with pytest.raises(InteractionIdempotencyConflictError):
        await service.resolve(_response(request, option="deny"))


@pytest.mark.asyncio
async def test_cancel_invocation_atomically_releases_parallel_waiters(
    tmp_path: Path,
) -> None:
    service = InteractionService(tmp_path / "interactions.sqlite3")
    invocation_id = uuid4()
    requests = (_request(invocation_id), _request(invocation_id))
    for request in requests:
        await service.open(request)
    waiters = [
        asyncio.create_task(service.wait(request.interaction_id))
        for request in requests
    ]
    await asyncio.sleep(0)

    cancelled = await service.cancel_invocation(
        invocation_id,
        detail="invocation interrupted",
    )
    waited = await asyncio.gather(*waiters)

    assert len(cancelled) == 2
    assert waited == list(cancelled)
    assert all(
        item.status is InteractionStatus.CANCELLED for item in cancelled
    )


@pytest.mark.asyncio
async def test_runtime_finalization_retains_non_blocking_suggestion(
    tmp_path: Path,
) -> None:
    service = InteractionService(tmp_path / "interactions.sqlite3")
    invocation_id = uuid4()
    blocking = _request(invocation_id)
    suggestion = _request(
        invocation_id,
        kind=InteractionKind.SUGGESTION,
        mode=InteractionMode.NON_BLOCKING,
    )
    deferred = _request(
        invocation_id,
        kind=InteractionKind.USER_INPUT,
    ).model_copy(
        update={
            "continuation_mode": ContinuationMode.CONVERSATION_TURN,
        },
    )
    await service.open(blocking)
    await service.open(suggestion)
    await service.open(deferred)

    cancelled = await service.cancel_invocation(
        invocation_id,
        detail="invocation finished as succeeded",
        include_non_blocking=False,
        preserve_conversation_continuations=True,
    )
    opened = await service.list_open(
        agent_id="default",
        conversation_id="chat-spec-1",
    )

    assert [item.interaction_id for item in cancelled] == [
        blocking.interaction_id,
    ]
    assert opened == (suggestion, deferred)


@pytest.mark.asyncio
async def test_wait_timeout_is_persisted_and_survives_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "interactions.sqlite3"
    service = InteractionService(path)
    request = _request(uuid4())
    await service.open(request)

    resolution = await service.wait(
        request.interaction_id,
        timeout_seconds=0.01,
    )
    restarted = InteractionService(path)
    recovered = await restarted.get_resolution(request.interaction_id)

    assert resolution.status is InteractionStatus.EXPIRED
    assert recovered == resolution
    conditions = await restarted.list_wait_conditions(
        agent_id="default",
        conversation_id="chat-spec-1",
        include_terminal=True,
    )
    assert len(conditions) == 1
    assert conditions[0].status is WaitConditionStatus.EXPIRED
    assert conditions[0].continuation.invocation_id == request.invocation_id
    assert (
        conditions[0].continuation.availability
        is ContinuationAvailability.DETACHED
    )


@pytest.mark.asyncio
async def test_wait_projection_reports_live_hook_attachment(
    tmp_path: Path,
) -> None:
    service = InteractionService(tmp_path / "interactions.sqlite3")
    request = _request(uuid4())
    await service.open(request)

    async def on_terminal(_resolution) -> None:
        return None

    await service.add_terminal_hook(request.interaction_id, on_terminal)

    conditions = await service.list_wait_conditions(
        agent_id="default",
        conversation_id="chat-spec-1",
    )
    await service.cancel_invocation(
        request.invocation_id,
        detail="test cleanup",
    )

    assert len(conditions) == 1
    assert (
        conditions[0].continuation.availability
        is ContinuationAvailability.ATTACHED
    )


@pytest.mark.asyncio
async def test_suggestion_is_persisted_but_never_awaited(
    tmp_path: Path,
) -> None:
    service = InteractionService(tmp_path / "interactions.sqlite3")
    request = _request(
        uuid4(),
        kind=InteractionKind.SUGGESTION,
        mode=InteractionMode.NON_BLOCKING,
    )
    await service.open(request)

    with pytest.raises(InteractionConflictError, match="cannot be awaited"):
        await service.wait(request.interaction_id)


@pytest.mark.asyncio
async def test_approval_rejects_ambiguous_multi_option_response(
    tmp_path: Path,
) -> None:
    service = InteractionService(tmp_path / "interactions.sqlite3")
    request = _request(uuid4())
    await service.open(request)

    with pytest.raises(
        InteractionConflictError,
        match="requires exactly one option",
    ):
        await service.resolve(
            InteractionResponse(
                interaction_id=request.interaction_id,
                idempotency_key="ambiguous",
                expected_revision=1,
                actor=ActorRef(type=ActorType.USER, id="local-user"),
                selected_option_ids=("approve", "deny"),
            ),
        )


@pytest.mark.asyncio
async def test_wait_projection_excludes_suggestions_and_cancels(
    tmp_path: Path,
) -> None:
    path = tmp_path / "interactions.sqlite3"
    service = InteractionService(path)
    invocation_id = uuid4()
    blocking = _request(invocation_id)
    suggestion = _request(
        invocation_id,
        kind=InteractionKind.SUGGESTION,
        mode=InteractionMode.NON_BLOCKING,
    )
    await service.open(blocking)
    await service.open(suggestion)

    opened = await service.list_wait_conditions(
        agent_id="default",
        conversation_id="chat-spec-1",
    )
    await service.cancel_invocation(
        invocation_id,
        detail="test cancellation",
        include_non_blocking=False,
    )
    restarted = InteractionService(path)
    terminal = await restarted.list_wait_conditions(
        agent_id="default",
        conversation_id="chat-spec-1",
        include_terminal=True,
    )

    assert [item.condition_id for item in opened] == [
        blocking.interaction_id,
    ]
    assert [item.status for item in terminal] == [
        WaitConditionStatus.CANCELLED,
    ]
    assert "prompt" not in terminal[0].model_dump(mode="json")


@pytest.mark.asyncio
async def test_interaction_history_is_owner_scoped_and_bounded(
    tmp_path: Path,
) -> None:
    service = InteractionService(tmp_path / "interactions.sqlite3")
    await service.open(_request(uuid4()))

    assert (
        len(
            await service.list_for_conversation(
                agent_id="default",
                conversation_id="chat-spec-1",
                limit=1,
            ),
        )
        == 1
    )
    assert (
        await service.list_for_conversation(
            agent_id="another-agent",
            conversation_id="chat-spec-1",
        )
        == ()
    )
    with pytest.raises(ValueError, match="limit must be between"):
        await service.list_for_conversation(
            agent_id="default",
            conversation_id="chat-spec-1",
            limit=0,
        )
