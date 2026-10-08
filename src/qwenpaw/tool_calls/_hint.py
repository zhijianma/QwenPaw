# -*- coding: utf-8 -*-
"""Construct hint messages for completed background tool calls."""

from __future__ import annotations

from typing import Any

from agentscope.message import Msg, TextBlock

from ..kernel import COMMITTED_ACTION_ITEM_METADATA_KEY


def make_offload_hint_msg(entry: Any) -> Any:
    """Construct a hint Msg for a completed offloaded tool call.

    The hint flattens the finalized response content blocks (TextBlock,
    ImageBlock, etc.) directly into the message so that provider formatters
    treat it as an ordinary assistant message — no ToolResultBlock means no
    orphan ``role=tool`` wire message and no tool-call pairing issues.
    """
    end = entry.end_state or "unknown"
    notification = TextBlock(
        type="text",
        text=(
            "<system-notification>\n"
            f"Background tool call `{entry.ctx.tool_name}` "
            f"(id={entry.ctx.tool_call_id}) "
            f"completed with state={end}. "
            "Result below.\n"
            "</system-notification>"
        ),
    )
    result_blocks = list(entry.final_response.content or [])
    response_metadata = entry.final_response.metadata or {}
    committed_item = response_metadata.get(
        COMMITTED_ACTION_ITEM_METADATA_KEY,
    )
    metadata = {}
    if isinstance(committed_item, dict):
        metadata[COMMITTED_ACTION_ITEM_METADATA_KEY] = dict(committed_item)
    return Msg(
        name="system",
        role="assistant",
        content=[notification] + result_blocks,
        metadata=metadata,
    )
