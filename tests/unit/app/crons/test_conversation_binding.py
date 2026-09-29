# -*- coding: utf-8 -*-
"""Tests for host-owned Cron Conversation binding."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from qwenpaw.app.crons.conversation_binding import (
    CronConversationBinder,
    CronConversationBinding,
    CronConversationBindingError,
)
from tests.unit.app.conftest import make_cron_job_spec


@pytest.mark.asyncio
async def test_binding_returns_verified_chat_identity() -> None:
    job = make_cron_job_spec(job_id="daily")
    manager = AsyncMock()
    manager.get_or_create_chat.return_value = SimpleNamespace(
        id="chat-daily",
        session_id="cron:daily",
        user_id="u1",
        channel="console",
    )
    workspace = SimpleNamespace(chat_manager=manager)

    binding = await CronConversationBinder(workspace).bind(
        job,
        session_id="cron:daily",
        required=True,
    )

    assert binding is not None
    assert binding.conversation_id == "chat-daily"
    assert binding.session_id == "cron:daily"
    manager.get_or_create_chat.assert_awaited_once_with(
        session_id="cron:daily",
        user_id="u1",
        channel="console",
        name="Test Job",
        source="cron",
    )


@pytest.mark.asyncio
async def test_strict_binding_rejects_missing_chat_service() -> None:
    job = make_cron_job_spec(job_id="daily")

    with pytest.raises(
        CronConversationBindingError,
        match="requires ChatManager",
    ):
        await CronConversationBinder(SimpleNamespace()).bind(
            job,
            session_id="cron:daily",
            required=True,
        )


@pytest.mark.asyncio
async def test_binding_rejects_chat_owned_by_another_target() -> None:
    job = make_cron_job_spec(job_id="daily")
    manager = AsyncMock()
    manager.get_or_create_chat.return_value = SimpleNamespace(
        id="chat-other",
        session_id="cron:daily",
        user_id="another-user",
        channel="console",
    )

    with pytest.raises(
        CronConversationBindingError,
        match="ownership does not match",
    ):
        await CronConversationBinder(
            SimpleNamespace(chat_manager=manager),
        ).bind(
            job,
            session_id="cron:daily",
            required=True,
        )


@pytest.mark.asyncio
async def test_compatibility_binding_tolerates_registration_failure() -> None:
    job = make_cron_job_spec(job_id="daily")
    manager = AsyncMock()
    manager.get_or_create_chat.side_effect = RuntimeError("unavailable")

    binding = await CronConversationBinder(
        SimpleNamespace(chat_manager=manager),
    ).bind(
        job,
        session_id="cron:daily",
        required=False,
    )

    assert binding is None


@pytest.mark.asyncio
async def test_touch_updates_the_bound_chat() -> None:
    manager = AsyncMock()
    workspace = SimpleNamespace(chat_manager=manager)
    binder = CronConversationBinder(workspace)
    binding = CronConversationBinding(
        conversation_id="chat-daily",
        session_id="cron:daily",
        user_id="u1",
        channel="console",
    )

    await binder.touch(binding, required=True)

    manager.touch_chat.assert_awaited_once_with("chat-daily")
