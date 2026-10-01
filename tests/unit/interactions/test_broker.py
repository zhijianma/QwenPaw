# -*- coding: utf-8 -*-
"""Tests for the invocation-bound runtime interaction producer API."""

import asyncio
from pathlib import Path
from uuid import UUID

import pytest

from qwenpaw.interactions import (
    InteractionService,
    RuntimeInteractionBroker,
    runtime_interaction_broker_from_context,
)
from qwenpaw.kernel import (
    ActorRef,
    ActorType,
    InteractionKind,
    InteractionOption,
    InteractionResponse,
    InteractionStatus,
    ContinuationMode,
)

INVOCATION_ID = UUID("00000000-0000-0000-0000-000000000401")


def _broker(
    tmp_path: Path,
) -> tuple[RuntimeInteractionBroker, InteractionService]:
    service = InteractionService(tmp_path / "interactions.sqlite3")
    return (
        RuntimeInteractionBroker(
            service=service,
            agent_id="default",
            conversation_id="chat-spec-1",
            invocation_id=INVOCATION_ID,
        ),
        service,
    )


@pytest.mark.asyncio
async def test_ask_user_persists_before_wait_and_resolves(tmp_path) -> None:
    broker, service = _broker(tmp_path)
    ask = asyncio.create_task(
        broker.ask_user(
            title="Choose output",
            prompt="Which format should be generated?",
            options=(
                InteractionOption(option_id="md", label="Markdown"),
                InteractionOption(option_id="html", label="HTML"),
            ),
        ),
    )
    for _ in range(100):
        opened = await service.list_open(
            agent_id="default",
            conversation_id="chat-spec-1",
        )
        if opened:
            break
        await asyncio.sleep(0)

    assert len(opened) == 1
    request = opened[0]
    assert request.kind is InteractionKind.USER_INPUT
    assert request.invocation_id == INVOCATION_ID
    assert "session_id" not in request.model_dump()

    await service.resolve(
        InteractionResponse(
            interaction_id=request.interaction_id,
            idempotency_key="answer-format",
            expected_revision=request.revision,
            actor=ActorRef(type=ActorType.USER, id="local-user"),
            selected_option_ids=("html",),
        ),
    )
    resolution = await ask

    assert resolution.status is InteractionStatus.RESOLVED
    assert resolution.response is not None
    assert resolution.response.selected_option_ids == ("html",)


@pytest.mark.asyncio
async def test_ask_user_is_released_by_invocation_cancellation(
    tmp_path,
) -> None:
    broker, service = _broker(tmp_path)
    ask = asyncio.create_task(
        broker.ask_user(title="Need input", prompt="Continue?"),
    )
    for _ in range(100):
        opened = await service.list_open(
            agent_id="default",
            conversation_id="chat-spec-1",
        )
        if opened:
            break
        await asyncio.sleep(0)

    await service.cancel_invocation(
        INVOCATION_ID,
        detail="invocation interrupted",
    )

    assert (await ask).status is InteractionStatus.CANCELLED


@pytest.mark.asyncio
async def test_ask_user_timeout_is_durable(tmp_path) -> None:
    broker, service = _broker(tmp_path)

    resolution = await broker.ask_user(
        title="Need input",
        prompt="Continue?",
        timeout_seconds=0.01,
    )
    recovered = await service.get_resolution(resolution.interaction_id)

    assert resolution.status is InteractionStatus.EXPIRED
    assert recovered == resolution


@pytest.mark.asyncio
async def test_deferred_input_preserves_correlation_and_returns(
    tmp_path,
) -> None:
    service = InteractionService(tmp_path / "interactions.sqlite3")
    correlation_id = UUID("00000000-0000-0000-0000-000000000499")
    broker = RuntimeInteractionBroker(
        service=service,
        agent_id="default",
        conversation_id="chat-spec-1",
        invocation_id=INVOCATION_ID,
        correlation_id=correlation_id,
    )

    request = await broker.defer_user_input(
        title="Need durable input",
        prompt="Which path?",
    )
    second = await broker.defer_user_input(
        title="Need another input",
        prompt="Which format?",
    )

    assert request.correlation_id == correlation_id
    assert request.continuation_mode is ContinuationMode.CONVERSATION_TURN
    assert broker.has_deferred_user_input is True
    assert broker.deferred_interaction_ids == [
        request.interaction_id,
        second.interaction_id,
    ]


@pytest.mark.asyncio
async def test_suggestion_persists_without_blocking(tmp_path) -> None:
    broker, service = _broker(tmp_path)

    suggestion = await broker.suggest(
        title="Possible next step",
        prompt="Consider adding a regression test.",
        metadata={"source": "quality-provider"},
    )
    opened = await service.list_open(
        agent_id="default",
        conversation_id="chat-spec-1",
    )

    assert suggestion.kind is InteractionKind.SUGGESTION
    assert suggestion.metadata == {"source": "quality-provider"}
    assert opened == (suggestion,)


def test_context_binding_requires_server_owned_identities(tmp_path) -> None:
    service = InteractionService(tmp_path / "interactions.sqlite3")
    context = {
        "_interaction_service": service,
        "agent_id": "default",
        "os_conversation_id": "chat-spec-1",
        "os_invocation_id": str(INVOCATION_ID),
        "os_correlation_id": ("00000000-0000-0000-0000-000000000499"),
    }

    broker = runtime_interaction_broker_from_context(context)

    assert broker is not None
    assert broker.service is service
    assert broker.conversation_id == "chat-spec-1"
    assert broker.invocation_id == INVOCATION_ID
    assert broker.correlation_id == UUID(
        "00000000-0000-0000-0000-000000000499",
    )
    assert (
        runtime_interaction_broker_from_context(
            {**context, "os_invocation_id": "forged"},
        )
        is None
    )
