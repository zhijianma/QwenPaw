# -*- coding: utf-8 -*-
"""Tests for workspace routing into a selected third-party agent."""

# pylint: disable=protected-access

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from qwenpaw.app.workspace.workspace import Workspace


class FakeHarnessRuntime:
    """Record the routing inputs received from a workspace."""

    def __init__(self) -> None:
        self.call: dict[str, Any] | None = None

    async def stream(self, **kwargs: Any) -> AsyncIterator[str]:
        self.call = kwargs
        yield "harness-output"


def test_workspace_composes_harness_environment_manager(
    tmp_path: Path,
) -> None:
    workspace = Workspace("agent-1", str(tmp_path / "workspace"))

    runtime = workspace.harness_runtime

    assert runtime._environment_manager is not None


@pytest.mark.asyncio
async def test_coding_mode_routes_directly_to_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = SimpleNamespace(
        backend="codex",
        coding_mode=SimpleNamespace(enabled=False),
    )
    monkeypatch.setattr(
        "qwenpaw.app.workspace.workspace.load_agent_config",
        lambda _agent_id: config,
    )
    workspace = Workspace("agent-1", str(tmp_path / "workspace"))
    runtime = FakeHarnessRuntime()
    workspace._harness_runtime = runtime
    request = object()

    output = [item async for item in workspace.stream_query(request)]

    assert output == ["harness-output"]
    assert runtime.call == {
        "backend": "codex",
        "request": request,
        "cwd": (tmp_path / "workspace").resolve(),
        "settings": {
            "_request_context": {
                "agent_id": "agent-1",
                "session_id": None,
                "user_id": None,
                "channel": "console",
            },
        },
    }


@pytest.mark.asyncio
async def test_harness_uses_chat_project_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prefer the canonical Chat override over the Agent workspace."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    config = SimpleNamespace(
        backend="codex",
        backend_settings={},
        project_dir=None,
        project_dirs=[],
    )
    monkeypatch.setattr(
        "qwenpaw.app.workspace.workspace.load_agent_config",
        lambda _agent_id: config,
    )
    workspace = Workspace("agent-1", str(tmp_path / "workspace"))
    workspace._service_manager.services["chat_manager"] = SimpleNamespace(
        get_chat=AsyncMock(
            return_value=SimpleNamespace(
                meta={
                    "runtime_context": {
                        "project_dirs": [
                            {"path": str(project_dir), "label": "Project"},
                        ],
                    },
                },
            ),
        ),
    )
    runtime = FakeHarnessRuntime()
    workspace._harness_runtime = runtime
    request = SimpleNamespace(
        request_context={"os_conversation_id": "chat-1"},
        session_id="console:chat-1",
        user_id="local-user",
        channel="console",
    )

    output = [item async for item in workspace.stream_query(request)]

    assert output == ["harness-output"]
    assert runtime.call is not None
    assert runtime.call["cwd"] == project_dir.resolve()
    workspace.chat_manager.get_chat.assert_awaited_once_with("chat-1")


@pytest.mark.asyncio
async def test_harness_missing_canonical_chat_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not fall back to another directory for a bound Chat identity."""
    monkeypatch.setattr(
        "qwenpaw.app.workspace.workspace.load_agent_config",
        lambda _agent_id: SimpleNamespace(
            backend="codex",
            backend_settings={},
            project_dir=None,
            project_dirs=[],
        ),
    )
    workspace = Workspace("agent-1", str(tmp_path / "workspace"))
    workspace._service_manager.services["chat_manager"] = SimpleNamespace(
        get_chat=AsyncMock(return_value=None),
    )
    runtime = FakeHarnessRuntime()
    workspace._harness_runtime = runtime
    request = SimpleNamespace(
        request_context={"os_conversation_id": "missing-chat"},
        session_id="console:missing-chat",
        user_id="local-user",
        channel="console",
    )

    with pytest.raises(RuntimeError, match="Chat identity is unavailable"):
        _ = [item async for item in workspace.stream_query(request)]

    assert runtime.call is None


@pytest.mark.asyncio
async def test_portability_adaptation_cannot_route_to_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "qwenpaw.app.workspace.workspace.load_agent_config",
        lambda _agent_id: SimpleNamespace(backend="codex"),
    )
    workspace = Workspace("agent-1", str(tmp_path / "workspace"))
    runtime = FakeHarnessRuntime()
    workspace._harness_runtime = runtime
    request = SimpleNamespace(
        request_context={"source": "portability_adaptation"},
        session_id="migration-worker",
        user_id="migration-worker",
        channel="console",
    )

    with pytest.raises(PermissionError, match="PawPort compatibility"):
        _ = [item async for item in workspace.stream_query(request)]

    assert runtime.call is None


@pytest.mark.asyncio
@pytest.mark.parametrize("configured_backend", ["qoder", "qwenpaw"])
async def test_harness_continuation_requires_original_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured_backend: str,
) -> None:
    monkeypatch.setattr(
        "qwenpaw.app.workspace.workspace.load_agent_config",
        lambda _agent_id: SimpleNamespace(backend=configured_backend),
    )
    workspace = Workspace("agent-1", str(tmp_path / "workspace"))
    runtime = FakeHarnessRuntime()
    workspace._harness_runtime = runtime
    request = SimpleNamespace(
        request_context={"harness_backend": "codex"},
    )

    with pytest.raises(RuntimeError, match="no longer matches"):
        _ = [item async for item in workspace.stream_query(request)]

    assert runtime.call is None
