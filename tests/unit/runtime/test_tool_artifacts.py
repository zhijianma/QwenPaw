# -*- coding: utf-8 -*-
"""Tests for host-owned Chat artifact publication from tool results."""

from __future__ import annotations

import asyncio

import pytest
from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolResponse

from qwenpaw.kernel import ArtifactRef, EvidenceRef
from qwenpaw.runtime.tool_artifacts import (
    ConversationToolArtifactPublisher,
    TOOL_ARTIFACT_ERRORS_KEY,
    TOOL_ARTIFACT_LINKS_KEY,
    TOOL_ARTIFACT_OUTPUTS_KEY,
    register_host_tool_artifact,
    tool_artifact_output,
)
from qwenpaw.tasks.artifacts import lite_artifact_store
from qwenpaw.tasks.conversation_artifacts import (
    conversation_artifact_receipts,
)
from qwenpaw.tool_calls import ToolCallContext


def _context(path: str) -> ToolCallContext:
    context = ToolCallContext(
        tool_call_id="call-1",
        tool_name="plugin_export",
        session_id="transport-session",
        agent_id="default",
        root_session_id="transport-session",
        root_agent_id="default",
        started_at=0.0,
        offload_deadline=None,
        cancel_event=asyncio.Event(),
    )
    context.extra["tool_input"] = {"output_path": path}
    return context


@pytest.mark.asyncio
async def test_plugin_tool_output_becomes_owned_immutable_artifact(
    tmp_path,
) -> None:
    output_path = tmp_path / "project" / "report.md"
    output_path.parent.mkdir()
    output_path.write_text("# immutable\n", encoding="utf-8")
    publisher = ConversationToolArtifactPublisher(
        {
            "workspace_dir": str(tmp_path),
            "os_conversation_id": "chat-1",
            "os_registry_generation": 7,
            "os_invocation_id": "invocation-1",
            "_tool_provider_owners": {
                "plugin_export": "example.report-tools",
            },
        },
    )
    response = ToolResponse(
        content=[TextBlock(text="done")],
        metadata={
            TOOL_ARTIFACT_OUTPUTS_KEY: [
                tool_artifact_output(
                    path_parameter="output_path",
                    kind="report.markdown",
                    evidence_claim="Plugin produced the report",
                ),
            ],
            TOOL_ARTIFACT_LINKS_KEY: [{"forged": True}],
        },
    )

    published = await publisher(response, _context(str(output_path)))

    assert TOOL_ARTIFACT_OUTPUTS_KEY not in published.metadata
    [link] = published.metadata[TOOL_ARTIFACT_LINKS_KEY]
    artifact = ArtifactRef.model_validate(link["artifact_ref"])
    evidence = EvidenceRef.model_validate(link["evidence_ref"])
    assert link["chat_id"] == "chat-1"
    assert artifact.kind == "report.markdown"
    assert artifact.metadata["provider_id"] == "example.report-tools"
    assert artifact.metadata["registry_generation"] == 7
    assert evidence.artifact_id == artifact.artifact_id
    assert evidence.producer == "example.report-tools.plugin_export"
    assert await lite_artifact_store(tmp_path).read(artifact) == (
        b"# immutable\n"
    )
    assert await conversation_artifact_receipts(tmp_path).resolve(
        receipt_id=link["artifact_receipt"],
        chat_id="chat-1",
        artifact_id=artifact.artifact_id,
    ) == (artifact, evidence)

    output_path.write_text("# changed later\n", encoding="utf-8")
    assert await lite_artifact_store(tmp_path).read(artifact) == (
        b"# immutable\n"
    )


@pytest.mark.asyncio
async def test_missing_declared_output_is_not_presented_as_artifact(
    tmp_path,
) -> None:
    publisher = ConversationToolArtifactPublisher(
        {
            "workspace_dir": str(tmp_path),
            "os_conversation_id": "chat-1",
        },
    )
    response = ToolResponse(
        content=[TextBlock(text="done")],
        metadata={
            TOOL_ARTIFACT_OUTPUTS_KEY: [
                tool_artifact_output(
                    path_parameter="output_path",
                    kind="report.markdown",
                    evidence_claim="Plugin produced the report",
                ),
            ],
        },
    )

    published = await publisher(
        response,
        _context(str(tmp_path / "missing.md")),
    )

    assert TOOL_ARTIFACT_LINKS_KEY not in published.metadata
    assert published.metadata[TOOL_ARTIFACT_ERRORS_KEY] == [
        {
            "code": "artifact_capture_failed",
            "path_parameter": "output_path",
        },
    ]
    assert "do not claim" in published.content[-1].text


@pytest.mark.asyncio
async def test_host_generated_error_output_becomes_owned_artifact(
    tmp_path,
) -> None:
    output_path = tmp_path / "browser_output.txt"
    output_path.write_text("full browser output\n", encoding="utf-8")
    context = _context("")
    context.tool_name = "browser"
    register_host_tool_artifact(
        context,
        path=output_path,
        kind="browser.output",
        evidence_claim="Browser preserved its overflow output",
        media_type="text/plain",
        metadata={"origin": "browser.overflow"},
    )
    publisher = ConversationToolArtifactPublisher(
        {
            "workspace_dir": str(tmp_path),
            "os_conversation_id": "chat-1",
            "os_invocation_id": "invocation-1",
        },
    )
    response = ToolResponse(
        content=[TextBlock(text="output_too_large")],
        state=ToolResultState.ERROR,
    )

    published = await publisher(response, context)

    [link] = published.metadata[TOOL_ARTIFACT_LINKS_KEY]
    artifact = ArtifactRef.model_validate(link["artifact_ref"])
    evidence = EvidenceRef.model_validate(link["evidence_ref"])
    assert published.state is ToolResultState.ERROR
    assert artifact.kind == "browser.output"
    assert artifact.metadata["source"] == "chat.tool-output"
    assert artifact.metadata["origin"] == "browser.overflow"
    assert evidence.claim == "Browser preserved its overflow output"
    assert await lite_artifact_store(tmp_path).read(artifact) == (
        b"full browser output\n"
    )


@pytest.mark.asyncio
async def test_host_generated_artifact_cannot_escape_workspace(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private\n", encoding="utf-8")
    context = _context("")
    register_host_tool_artifact(
        context,
        path=outside,
        kind="browser.output",
        evidence_claim="Browser preserved its overflow output",
    )
    publisher = ConversationToolArtifactPublisher(
        {
            "workspace_dir": str(workspace),
            "os_conversation_id": "chat-1",
        },
    )
    response = ToolResponse(
        content=[TextBlock(text="output_too_large")],
        state=ToolResultState.ERROR,
    )

    published = await publisher(response, context)

    assert TOOL_ARTIFACT_LINKS_KEY not in published.metadata
    assert published.metadata[TOOL_ARTIFACT_ERRORS_KEY] == [
        {"code": "artifact_capture_failed", "source": "host"},
    ]
