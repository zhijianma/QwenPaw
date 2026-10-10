# -*- coding: utf-8 -*-
"""Tests for framework-neutral plugin tool result adaptation."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator

import pytest
from agentscope.message import ToolResultState
from agentscope.tool import ToolChunk

from qwenpaw.kernel import ToolExecutionResult, ToolExecutionStatus
from qwenpaw.runtime.tool_results import adapt_tool_execution_results


def _success() -> ToolExecutionResult:
    return ToolExecutionResult(
        status=ToolExecutionStatus.SUCCEEDED,
        content="done",
    )


def test_sync_generator_stops_after_terminal_result() -> None:
    effects: list[str] = []

    def stream() -> Generator[object, None, None]:
        yield "progress"
        yield _success()
        effects.append("continued")
        yield "invalid trailing output"

    output = list(adapt_tool_execution_results(stream)())

    assert output[0] == "progress"
    assert isinstance(output[1], ToolChunk)
    assert output[1].state is ToolResultState.SUCCESS
    assert output[1].is_last is True
    assert not effects


@pytest.mark.asyncio
async def test_async_generator_stops_after_terminal_result() -> None:
    effects: list[str] = []

    async def stream() -> AsyncGenerator[object, None]:
        yield "progress"
        yield _success()
        effects.append("continued")
        yield "invalid trailing output"

    wrapped = adapt_tool_execution_results(stream)
    output = [item async for item in wrapped()]

    assert output[0] == "progress"
    assert isinstance(output[1], ToolChunk)
    assert output[1].state is ToolResultState.SUCCESS
    assert output[1].is_last is True
    assert not effects
