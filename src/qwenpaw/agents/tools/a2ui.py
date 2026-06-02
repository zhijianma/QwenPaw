# -*- coding: utf-8 -*-
# flake8: noqa: E501
"""a2ui — Agent-to-UI rich content rendering tool."""

import os
import unicodedata

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

# Valid block types for validation
_VALID_BLOCK_TYPES = frozenset({
    "text",
    "diff",
    "code",
    "image",
    "table",
    "card",
    "progress",
    "buttons",
    "form",
    "choice",
})


async def a2ui(
    blocks: list[dict],
    title: str = "",
) -> ToolResponse:
    """Display rich visual content to the user in the Console Web UI.

    Use this tool to show structured content beyond plain text:
    diffs, code, images, tables, cards, progress bars, buttons, forms, or choices.

    The content is rendered immediately in the UI. This tool does NOT block —
    it returns right away. For interactive blocks (buttons, form, choice),
    the user's response arrives as a normal chat message in the next turn.

    Args:
        blocks (`list[dict]`):
            List of UI blocks. Each block must have a ``"type"`` field.
            Supported types and their fields:

            - ``{"type": "text", "content": "markdown text"}``
            - ``{"type": "diff", "file": "path", "language": "python", "old_content": "before", "new_content": "after"}``
            - ``{"type": "code", "language": "python", "content": "code string", "filename": "example.py"}``
            - ``{"type": "image", "url": "local path or URL", "alt": "description"}``
            - ``{"type": "table", "headers": ["Col1", "Col2"], "rows": [["a", "b"]]}``
            - ``{"type": "card", "title": "Title", "content": "Description", "tags": ["tag1"], "image": "url"}``
            - ``{"type": "progress", "label": "Task", "value": 75, "max": 100, "status": "running"}``
            - ``{"type": "buttons", "buttons": [{"label": "OK", "value": "ok", "style": "primary"}]}``
            - ``{"type": "form", "fields": [{"name": "x", "label": "X", "field_type": "text"}], "submit_label": "Go"}``
            - ``{"type": "choice", "prompt": "Pick one", "options": [{"label": "A", "value": "a"}], "multi_select": false}``

        title (`str`):
            Optional heading displayed above the blocks.

    Returns:
        `ToolResponse`: Confirmation that the content was displayed.
    """
    # --- Validate ---
    if not blocks or not isinstance(blocks, list):
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text="Error: blocks must be a non-empty list.",
                ),
            ],
        )

    for i, block in enumerate(blocks):
        if not isinstance(block, dict) or "type" not in block:
            return ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text=f"Error: block at index {i} must be a dict with a 'type' field.",
                    ),
                ],
            )

    # --- Convert local image paths to file:// URLs ---
    from .send_file import _path_to_file_url

    for block in blocks:
        if block.get("type") == "image" and block.get("url"):
            url = block["url"]
            if not url.startswith(("http://", "https://", "file://", "data:")):
                expanded = os.path.expanduser(
                    unicodedata.normalize("NFC", url),
                )
                if os.path.isfile(expanded):
                    block["url"] = _path_to_file_url(os.path.abspath(expanded))

    return ToolResponse(
        content=[
            TextBlock(type="text", text="Displayed to user."),
        ],
    )
