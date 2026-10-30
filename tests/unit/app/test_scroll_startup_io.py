# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Regression test for lazy legacy recall migration."""

from __future__ import annotations

import importlib

import pytest

from qwenpaw.agents.context.scroll import sync as scroll_sync


@pytest.mark.asyncio
async def test_scroll_history_is_not_migrated_during_startup(
    monkeypatch,
) -> None:
    """Conversation access owns migration from JSON and legacy history."""
    monkeypatch.setattr(
        "qwenpaw.envs.load_envs_into_environ",
        lambda: None,
    )
    app_module = importlib.import_module("qwenpaw.app._app")
    called = False

    def startup_sync() -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(scroll_sync, "sync_all_scroll_agents", startup_sync)

    await app_module._sync_scroll_history_on_startup()

    assert called is False
