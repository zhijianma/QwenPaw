# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Cancellation persistence must respect ephemeral internal requests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from qwenpaw.agents.acp.meta import ACP_EPHEMERAL_META_KEY
from qwenpaw.runtime.runtime import Runtime


@pytest.mark.asyncio
async def test_ephemeral_cancel_does_not_create_hidden_session() -> None:
    class FailIfSaved:
        async def save_session_state(self, **_kwargs) -> None:
            raise AssertionError("ephemeral request must not be saved")

    ctx = SimpleNamespace(
        request=SimpleNamespace(
            request_context={ACP_EPHEMERAL_META_KEY: True},
        ),
        agent=SimpleNamespace(state_dict=lambda: {"state": {}}),
        workspace=SimpleNamespace(session=FailIfSaved()),
    )
    runtime = Runtime(workspace=object(), app_services=None)

    await runtime._try_save_on_cancel(ctx)  # pylint: disable=protected-access


@pytest.mark.asyncio
async def test_v2_cancel_serializes_context_without_legacy_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailIfSaved:
        async def save_session_state(self, **_kwargs) -> None:
            raise AssertionError("V2 cancellation must not write legacy JSON")

    monkeypatch.setattr(
        "qwenpaw.runtime.console_turn_state.stamp_console_turn",
        lambda *_args, **_kwargs: None,
    )
    request = SimpleNamespace(
        request_context={},
        user_id="user",
        channel="console",
        chat_conversation_id="conversation",
    )
    workspace = SimpleNamespace(
        session=FailIfSaved(),
        chat_application=object(),
    )
    ctx = SimpleNamespace(
        request=request,
        agent=SimpleNamespace(state_dict=lambda: {"state": {"context": []}}),
        workspace=workspace,
        session_id="session",
        mode_state={"mode": {"active": True}},
        extras={},
        error=RuntimeError("failed"),
    )

    await Runtime(
        workspace=workspace,
        app_services=None,
    )._try_save_on_cancel(ctx)

    assert request.chat_runtime_context
