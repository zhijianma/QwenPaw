# -*- coding: utf-8 -*-
"""Tests for privacy-safe, per-model-call context manifests."""

# pylint: disable=protected-access

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from agentscope.agent import Agent
from agentscope.message import Msg, TextBlock, ThinkingBlock, ToolResultBlock

from qwenpaw.agents.react_agent import QwenPawAgent
from qwenpaw.kernel import ContextFragmentKind, ContextPolicy
from qwenpaw.kernel.invocation import InvocationScope
from qwenpaw.runtime.context_manifests import (
    ContextManifestCompiler,
    ContextManifestConflictError,
    ContextManifestLimitError,
    lite_context_manifest_store,
)


def _scope() -> InvocationScope:
    return InvocationScope(
        agent_id="default",
        conversation_id="chat-1",
        session_id="transport-session",
        root_agent_id="default",
        root_session_id="transport-session",
        workspace_dir="/tmp/qwenpaw-workspace",
        registry_generation=12,
        capability_lock_id=UUID(int=1),
        capability_lock_hash=f"sha256:{'1' * 64}",
    )


def _messages() -> list[Msg]:
    return [
        Msg(
            name="system",
            role="system",
            content=[TextBlock(text="system contract")],
        ),
        Msg(
            name="user",
            role="user",
            content=[TextBlock(text="token secret-value")],
        ),
        Msg(
            name="assistant",
            role="assistant",
            content=[ThinkingBlock(thinking="private chain of thought")],
        ),
        Msg(
            name="assistant",
            role="assistant",
            content=[
                ToolResultBlock(
                    id="call-1",
                    name="write_file",
                    output=[TextBlock(text="done")],
                ),
            ],
        ),
    ]


def _tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write text",
                "parameters": {
                    "type": "object",
                    "properties": {"file_path": {"type": "string"}},
                },
            },
        },
    ]


def test_manifest_covers_actual_messages_and_capability_disclosure() -> None:
    compiler = ContextManifestCompiler(
        _scope(),
        tool_owners={
            "write_file": "qwenpaw.system.workspace-tools",
        },
    )

    manifest = compiler.compile(
        messages=_messages(),
        tools=_tools(),
        model_call_index=1,
        attempt_kind="primary",
    )

    assert manifest.conversation_id == "chat-1"
    assert manifest.registry_generation == 12
    assert manifest.capability_lock_id == UUID(int=1)
    assert manifest.capability_lock_hash == f"sha256:{'1' * 64}"
    assert manifest.model_call_index == 1
    assert manifest.disclosed_tool_count == 1
    assert manifest.total_size_bytes > 0
    assert manifest.total_estimated_tokens > 0
    assert manifest.manifest_hash.startswith("sha256:")
    assert [fragment.kind for fragment in manifest.fragments].count(
        ContextFragmentKind.REDACTED,
    ) == 1
    tool = manifest.fragments[-1]
    assert tool.kind is ContextFragmentKind.TOOL_SCHEMA
    assert tool.source == "qwenpaw.system.workspace-tools"
    assert tool.source_version == "registry-generation-12"

    persisted = manifest.model_dump_json()
    assert "secret-value" not in persisted
    assert "private chain of thought" not in persisted
    assert "system contract" not in persisted
    assert "Write text" not in persisted


def test_manifest_fails_closed_instead_of_omitting_fragments() -> None:
    compiler = ContextManifestCompiler(
        _scope(),
        policy=ContextPolicy(max_fragments=1),
    )

    with pytest.raises(ContextManifestLimitError):
        compiler.compile(
            messages=_messages(),
            tools=[],
            model_call_index=1,
            attempt_kind="primary",
        )


def test_nested_ids_remain_part_of_tool_and_manifest_fingerprints() -> None:
    first_tools = _tools()
    second_tools = _tools()
    first_tools[0]["function"]["parameters"]["properties"]["id"] = {
        "const": "first",
    }
    second_tools[0]["function"]["parameters"]["properties"]["id"] = {
        "const": "second",
    }
    compiler = ContextManifestCompiler(_scope())

    first = compiler.compile(
        messages=[],
        tools=first_tools,
        model_call_index=1,
        attempt_kind="primary",
    )
    second = compiler.compile(
        messages=[],
        tools=second_tools,
        model_call_index=1,
        attempt_kind="primary",
    )

    assert first.fragments[0].content_hash != (
        second.fragments[0].content_hash
    )
    assert first.manifest_hash != second.manifest_hash


def test_provider_visible_tool_call_id_remains_in_fingerprint() -> None:
    compiler = ContextManifestCompiler(_scope())
    first = compiler.compile(
        messages=[
            Msg(
                name="assistant",
                role="assistant",
                content=[
                    ToolResultBlock(
                        id="call-first",
                        name="write_file",
                        output="done",
                    ),
                ],
            ),
        ],
        tools=[],
        model_call_index=1,
        attempt_kind="primary",
    )
    second = compiler.compile(
        messages=[
            Msg(
                name="assistant",
                role="assistant",
                content=[
                    ToolResultBlock(
                        id="call-second",
                        name="write_file",
                        output="done",
                    ),
                ],
            ),
        ],
        tools=[],
        model_call_index=1,
        attempt_kind="primary",
    )

    assert first.fragments[0].content_hash != (
        second.fragments[0].content_hash
    )


@pytest.mark.asyncio
async def test_filesystem_store_is_immutable_and_queryable(tmp_path) -> None:
    compiler = ContextManifestCompiler(_scope())
    first = compiler.compile(
        messages=_messages()[:1],
        tools=[],
        model_call_index=1,
        attempt_kind="primary",
    )
    store = lite_context_manifest_store(tmp_path)

    await store.append(first)
    await store.append(
        first.model_copy(
            update={"created_at": first.created_at + timedelta(seconds=1)},
        ),
    )

    assert await store.list_for_conversation("chat-1") == [first]

    conflicting = compiler.compile(
        messages=_messages()[1:2],
        tools=[],
        model_call_index=1,
        attempt_kind="primary",
    )
    with pytest.raises(ContextManifestConflictError):
        await store.append(conflicting)


class _Compiler:
    def __init__(self, manifest) -> None:
        self.manifest = manifest
        self.attempts: list[tuple[int, str]] = []

    def compile(self, **kwargs):
        self.attempts.append(
            (kwargs["model_call_index"], kwargs["attempt_kind"]),
        )
        return self.manifest


class _Store:
    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self.events = events
        self.fail = fail

    async def append(self, _manifest) -> None:
        self.events.append("manifest")
        if self.fail:
            raise OSError("audit store unavailable")


def _bare_agent(compiler, store) -> QwenPawAgent:
    agent = object.__new__(QwenPawAgent)
    agent._tool_schema_index = {}
    agent._context_manifest_compiler = compiler
    agent._context_manifest_store = store
    agent._model_call_index = 0
    agent._context_manager = None
    agent.state = SimpleNamespace(context=[])
    return agent


@pytest.mark.asyncio
async def test_manifest_precedes_provider_call(monkeypatch) -> None:
    manifest = ContextManifestCompiler(_scope()).compile(
        messages=_messages()[:1],
        tools=[],
        model_call_index=1,
        attempt_kind="primary",
    )
    events: list[str] = []
    compiler = _Compiler(manifest)
    agent = _bare_agent(compiler, _Store(events))

    async def provider_call(self, messages, tools, tool_choice=None):
        del self, messages, tools, tool_choice
        events.append("provider")
        return "ok"

    monkeypatch.setattr(Agent, "_call_model", provider_call)

    result = await agent._call_model(messages=[], tools=[])

    assert result == "ok"
    assert events == ["manifest", "provider"]
    assert compiler.attempts == [(1, "primary")]


@pytest.mark.asyncio
async def test_agent_fails_closed_before_provider_when_store_fails(
    monkeypatch,
) -> None:
    manifest = ContextManifestCompiler(_scope()).compile(
        messages=_messages()[:1],
        tools=[],
        model_call_index=1,
        attempt_kind="primary",
    )
    events: list[str] = []
    agent = _bare_agent(_Compiler(manifest), _Store(events, fail=True))

    async def provider_call(self, messages, tools, tool_choice=None):
        del self, messages, tools, tool_choice
        events.append("provider")
        return "should not run"

    monkeypatch.setattr(Agent, "_call_model", provider_call)

    with pytest.raises(OSError, match="audit store unavailable"):
        await agent._call_model(messages=[], tools=[])

    assert events == ["manifest"]
