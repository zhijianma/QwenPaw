# -*- coding: utf-8 -*-
"""Translate stable plugin tool results into the active tool framework."""

from __future__ import annotations

import functools
import inspect
from collections.abc import AsyncGenerator, Callable, Generator
from typing import Any

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk

from ..kernel.models import (
    ACTION_RETRY_HINT_METADATA_KEY,
    TOOL_ERROR_CODE_METADATA_KEY,
    ToolExecutionResult,
    ToolExecutionStatus,
)


def _to_tool_chunk(result: Any) -> Any:
    if not isinstance(result, ToolExecutionResult):
        return result
    succeeded = result.status is ToolExecutionStatus.SUCCEEDED
    metadata = {}
    if not succeeded:
        metadata = {
            ACTION_RETRY_HINT_METADATA_KEY: result.retryable,
            TOOL_ERROR_CODE_METADATA_KEY: result.error_code,
        }
    return ToolChunk(
        content=[TextBlock(type="text", text=result.content)],
        is_last=True,
        state=(
            ToolResultState.SUCCESS if succeeded else ToolResultState.ERROR
        ),
        metadata=metadata,
    )


def adapt_tool_execution_results(function: Callable[..., Any]) -> Callable:
    """Preserve one tool signature while translating stable result values."""
    if inspect.isasyncgenfunction(function):

        @functools.wraps(function)
        async def async_generator(*args: Any, **kwargs: Any) -> AsyncGenerator:
            async for item in function(*args, **kwargs):
                yield _to_tool_chunk(item)
                if isinstance(item, ToolExecutionResult):
                    return

        return async_generator

    if inspect.isgeneratorfunction(function):

        @functools.wraps(function)
        def generator(*args: Any, **kwargs: Any) -> Generator:
            for item in function(*args, **kwargs):
                yield _to_tool_chunk(item)
                if isinstance(item, ToolExecutionResult):
                    return

        return generator

    if inspect.iscoroutinefunction(function):

        @functools.wraps(function)
        async def coroutine(*args: Any, **kwargs: Any) -> Any:
            return _to_tool_chunk(await function(*args, **kwargs))

        return coroutine

    @functools.wraps(function)
    def call(*args: Any, **kwargs: Any) -> Any:
        return _to_tool_chunk(function(*args, **kwargs))

    return call


__all__ = ["adapt_tool_execution_results"]
