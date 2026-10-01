# -*- coding: utf-8 -*-
"""Unit tests for Browser tool result publication."""

from __future__ import annotations

import asyncio

import pytest
from agentscope.tool import ToolResponse

from qwenpaw.browser.execution.wire import ExecResult
from qwenpaw.browser.tool_entrypoint import run_browser_tool
from qwenpaw.config.context import current_workspace_dir
from qwenpaw.kernel import ArtifactRef, EvidenceRef
from qwenpaw.runtime.tool_artifacts import (
    ConversationToolArtifactPublisher,
    TOOL_ARTIFACT_LINKS_KEY,
)
from qwenpaw.tasks.artifacts import lite_artifact_store
from qwenpaw.tool_calls import ToolCallContext
from qwenpaw.tool_calls._ctxvars import (
    reset_call_context,
    set_call_context,
)


class _OverflowManager:
    """Return a worker overflow reference without launching Chromium."""

    def __init__(self, staged_path: str) -> None:
        self._staged_path = staged_path

    async def execute(self, request) -> ExecResult:
        return ExecResult(
            request_id=request.request_id,
            error={
                "category": "FATAL",
                "reason": "output_too_large",
                "teaching": "Filter the output.",
                "overflow_stdout_path": self._staged_path,
            },
        )


@pytest.mark.asyncio
async def test_browser_overflow_is_published_as_chat_artifact(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    staged = tmp_path / "worker-output.txt"
    staged.write_text("large browser output\n", encoding="utf-8")
    context = ToolCallContext(
        tool_call_id="call-browser",
        tool_name="browser",
        session_id="transport-session",
        agent_id="default",
        root_session_id="transport-session",
        root_agent_id="default",
        started_at=0.0,
        offload_deadline=None,
        cancel_event=asyncio.Event(),
    )
    monkeypatch.setattr(
        "qwenpaw.browser.tool_entrypoint.get_default_kernel_manager",
        lambda: _OverflowManager(str(staged)),
    )
    workspace_token = current_workspace_dir.set(workspace)
    call_token = set_call_context(context)
    try:
        chunk = await run_browser_tool("print('too much')")
    finally:
        reset_call_context(call_token)
        current_workspace_dir.reset(workspace_token)

    response = ToolResponse(
        content=list(chunk.content),
        state=chunk.state,
    )
    published = await ConversationToolArtifactPublisher(
        {
            "workspace_dir": str(workspace),
            "os_conversation_id": "chat-browser",
            "os_invocation_id": "invocation-browser",
        },
    )(response, context)

    assert not staged.exists()
    [link] = published.metadata[TOOL_ARTIFACT_LINKS_KEY]
    artifact = ArtifactRef.model_validate(link["artifact_ref"])
    evidence = EvidenceRef.model_validate(link["evidence_ref"])
    assert artifact.kind == "browser.output"
    assert artifact.metadata["origin"] == "browser.overflow"
    assert evidence.artifact_id == artifact.artifact_id
    assert await lite_artifact_store(workspace).read(artifact) == (
        b"large browser output\n"
    )
