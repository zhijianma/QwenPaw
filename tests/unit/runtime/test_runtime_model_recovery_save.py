# -*- coding: utf-8 -*-
"""Session persistence at a partial-model-step recovery boundary."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from agentscope.message import Msg, TextBlock
from agentscope.state import AgentState

from qwenpaw.app.chats.session import SafeJSONSession
from qwenpaw.runtime.runtime import Runtime


class _Agent:
    def __init__(self, context: list[Msg]) -> None:
        self.name = "QwenPaw"
        self.state = AgentState(context=context)

    def _save_to_context(self, blocks: list[object]) -> None:
        self.state.context.append(
            Msg(name=self.name, role="assistant", content=blocks),
        )

    def state_dict(self) -> dict:
        return {"state": self.state.model_dump(mode="json")}


@pytest.mark.asyncio
async def test_model_step_recovery_save_excludes_partial_output(
    tmp_path,
) -> None:
    original = Msg(
        name="user",
        role="user",
        content=[TextBlock(text="finish the task")],
    )
    agent = _Agent([original])
    session = SafeJSONSession(save_dir=str(tmp_path))
    envelope = SimpleNamespace(
        collect_partial_blocks=lambda: [
            ("text", "uncommitted partial output"),
        ],
        collect_tool_output=lambda: {},
    )
    ctx = SimpleNamespace(
        extras={},
        error=RuntimeError("partial model stream"),
        agent=agent,
        session_id="chat:model-recovery",
        request=SimpleNamespace(
            user_id="user",
            channel="console",
            request_context={},
            input=[],
        ),
        workspace=SimpleNamespace(session=session),
        _envelope=envelope,
    )

    runtime = Runtime(workspace=ctx.workspace, app_services=None)
    await runtime._try_save_on_cancel(
        ctx,
        include_partial=False,
    )

    saved = await session.get_session_state_dict(
        "chat:model-recovery",
        "user",
        "console",
    )
    context = saved["agent"]["state"]["context"]
    serialized = str(context)
    assert len(context) == 1
    assert context[0]["id"] == original.id
    assert "uncommitted partial output" not in serialized


@pytest.mark.asyncio
async def test_user_interruption_save_still_includes_partial_output(
    tmp_path,
) -> None:
    agent = _Agent([])
    session = SafeJSONSession(save_dir=str(tmp_path))
    envelope = SimpleNamespace(
        collect_partial_blocks=lambda: [("text", "visible partial output")],
        collect_tool_output=lambda: {},
    )
    ctx = SimpleNamespace(
        extras={},
        error=RuntimeError("interrupted"),
        agent=agent,
        session_id="chat:user-interrupt",
        request=SimpleNamespace(
            user_id="user",
            channel="console",
            request_context={},
            input=[],
        ),
        workspace=SimpleNamespace(session=session),
        _envelope=envelope,
    )

    runtime = Runtime(workspace=ctx.workspace, app_services=None)
    await runtime._try_save_on_cancel(ctx)

    saved = await session.get_session_state_dict(
        "chat:user-interrupt",
        "user",
        "console",
    )
    context = saved["agent"]["state"]["context"]
    assert len(context) == 1
    assert context[0]["role"] == "assistant"
    assert context[0]["content"][0]["text"] == "visible partial output"
