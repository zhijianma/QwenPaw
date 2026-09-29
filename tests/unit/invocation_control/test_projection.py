# -*- coding: utf-8 -*-
"""Tests for recoverable conversation runtime projections."""

from uuid import uuid4

import pytest

from qwenpaw.interactions import InteractionService
from qwenpaw.invocation_control import (
    ConversationRuntimeProjectionService,
    InvocationControlService,
    SQLiteInvocationControl,
)
from qwenpaw.kernel import (
    InteractionKind,
    InteractionMode,
    InteractionRequest,
    TurnSubmissionRequest,
)


@pytest.mark.asyncio
async def test_projection_cursor_tracks_both_durable_sources(tmp_path) -> None:
    control = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    interactions = InteractionService(tmp_path / "interactions.sqlite3")
    projection_service = ConversationRuntimeProjectionService(
        control,
        interactions,
    )
    first = await projection_service.read(
        agent_id="default",
        conversation_id="chat-1",
    )
    repeated = await projection_service.read(
        agent_id="default",
        conversation_id="chat-1",
    )
    unchanged = await projection_service.wait_for_change(
        agent_id="default",
        conversation_id="chat-1",
        after_cursor=first.cursor,
        timeout=0.01,
        poll_interval=0.001,
    )

    invocation_id = uuid4()
    interaction = InteractionRequest(
        kind=InteractionKind.USER_INPUT,
        mode=InteractionMode.BLOCKING,
        agent_id="default",
        conversation_id="chat-1",
        invocation_id=invocation_id,
        title="Need input",
        prompt="Choose one option.",
    )
    await interactions.open(interaction)
    opened = await projection_service.wait_for_change(
        agent_id="default",
        conversation_id="chat-1",
        after_cursor=first.cursor,
        timeout=1,
    )
    assert opened is not None

    await interactions.cancel_invocation(
        invocation_id,
        detail="test finished",
        include_non_blocking=False,
    )
    closed = await projection_service.wait_for_change(
        agent_id="default",
        conversation_id="chat-1",
        after_cursor=opened.cursor,
        timeout=1,
    )

    assert repeated.cursor == first.cursor
    assert unchanged is None
    assert opened.interactions == (interaction,)
    assert closed is not None
    assert closed.interactions == ()
    assert closed.cursor == first.cursor

    assert control.store is not None
    await control.store.submit(
        TurnSubmissionRequest(
            agent_id="default",
            conversation_id="chat-1",
            content="queued input",
            idempotency_key="turn-1",
        ),
        expected_revision=0,
    )
    queued = await projection_service.wait_for_change(
        agent_id="default",
        conversation_id="chat-1",
        after_cursor=closed.cursor,
        timeout=1,
    )
    assert queued is not None
    assert queued.queue.revision == 1
    assert len(queued.queue.submissions) == 1
    await control.close()
