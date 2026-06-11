# -*- coding: utf-8 -*-
"""@a2ui_visual — decorator that enriches tool output with a2ui visual blocks.

Usage example::

    def _edit_pre(file_path, **_):
        return {"old": open(file_path).read()}

    def _edit_blocks(pre, file_path, old_text, new_text, **_):
        return {
            "title": f"Edit: {file_path}",
            "blocks": [{"type": "diff", "file": file_path,
                         "old_content": pre["old"],
                         "new_content": pre["old"].replace(old_text, new_text)}],
        }

    @a2ui_visual(blocks_builder=_edit_blocks, pre_hook=_edit_pre)
    async def edit_file(file_path, old_text, new_text):
        ...

The decorator embeds a JSON payload (after a marker) in the tool's text
response.  The Console Web UI detects this marker and renders the blocks
using the A2UIRenderer.
"""

import inspect
import json
from functools import wraps
from typing import Any, Callable, Optional

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

# Marker separating normal text output from a2ui visual payload.
# Must be kept in sync with the frontend constant in A2UIRenderer.
A2UI_VISUAL_MARKER = "\n<!-- __a2ui_visual__ -->\n"


def a2ui_visual(
    blocks_builder: Callable[..., dict],
    pre_hook: Optional[Callable[..., dict]] = None,
) -> Callable:
    """Decorator that appends a2ui visual blocks to a tool's response.

    Args:
        blocks_builder:
            ``(pre_state, **tool_kwargs) -> {"title": str, "blocks": [...]}``
            Called after the tool runs (on success) to build the visual data.
        pre_hook:
            ``(**tool_kwargs) -> dict``  (optional)
            Called *before* the tool runs.  Return value is passed to
            *blocks_builder* as the first positional arg ``pre_state``.
            Useful for capturing file content before an edit, etc.
    """

    def decorator(fn: Callable) -> Callable:
        sig = inspect.signature(fn)

        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> ToolResponse:
            # Bind positional args to keyword names so builders get kwargs
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            all_kwargs = dict(bound.arguments)

            # --- Pre-hook (capture state before tool runs) ---
            pre_state: dict = {}
            if pre_hook is not None:
                try:
                    pre_state = pre_hook(**all_kwargs) or {}
                except Exception:  # noqa: BLE001
                    pass

            # --- Run original tool ---
            result: ToolResponse = await fn(*args, **kwargs)

            # Don't enhance error responses
            if result.content:
                text = result.content[0].get("text", "")
                if text.startswith("Error:"):
                    return result

            # --- Build visual payload ---
            try:
                visual = blocks_builder(pre_state, **all_kwargs)
            except Exception:  # noqa: BLE001
                return result

            payload = json.dumps(
                {
                    "__a2ui__": True,
                    "title": visual.get("title", ""),
                    "blocks": visual.get("blocks", []),
                },
                ensure_ascii=False,
            )

            # Append payload after marker
            original_text = (
                result.content[0].get("text", "") if result.content else ""
            )
            return ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text=original_text + A2UI_VISUAL_MARKER + payload,
                    ),
                ],
            )

        return wrapper

    return decorator
