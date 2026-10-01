# -*- coding: utf-8 -*-
"""Tests for the workspace-owned interaction infrastructure."""

import asyncio
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
    InteractionKind,
    InteractionHistoryPort,
    InteractionMode,
    InteractionOption,
    InteractionRequest,
    InteractionResponse,
    InteractionStatus,
    InteractionPort,
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
async def test_parallel_interactions_are_owned_by_chat_spec(
    tmp_path: Path,
) -> None:
    service = InteractionService(tmp_path / "interactions.sqlite3")
    assert isinstance(service, InteractionPort)
    assert isinstance(service, InteractionHistoryPort)
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
    await service.open(blocking)
    await service.open(suggestion)

    cancelled = await service.cancel_invocation(
        invocation_id,
        detail="invocation finished as succeeded",
        include_non_blocking=False,
    )
    opened = await service.list_open(
        agent_id="default",
        conversation_id="chat-spec-1",
    )

    assert [item.interaction_id for item in cancelled] == [
        blocking.interaction_id,
    ]
    assert opened == (suggestion,)


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
