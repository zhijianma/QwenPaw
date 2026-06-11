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
    "chart",
    "audio",
    "video",
    "file",
    "divider",
    "alert",
    "collapse",
    "stat",
    "image_buttons",
    "scene3d",
    "mermaid",
    "canvas",
    "dag",
    "mindmap",
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
            - ``{"type": "form", "title": "Please provide details:", "fields": [{"name": "x", "label": "X", "field_type": "text"}], "submit_label": "Submit", "skip_label": "Skip", "result_header": "User provided the following:"}``
            - ``{"type": "choice", "prompt": "Pick one", "options": [{"label": "A", "value": "a"}], "multi_select": false}``
            - ``{"type": "chart", "chartType": "line|column|bar|pie|area|scatter|radar|gauge", "data": [{"month": "Jan", "value": 100}], "xField": "month", "yField": "value", "title": "Chart Title", "config": {...}}``
            - ``{"type": "audio", "url": "audio URL or path", "title": "Track name"}``
            - ``{"type": "video", "url": "video URL or path", "title": "Video title", "poster": "thumbnail URL"}``
            - ``{"type": "file", "url": "download URL or path", "filename": "report.pdf", "size": "2.3 MB"}``
            - ``{"type": "divider", "text": "optional label", "orientation": "left|center|right"}``
            - ``{"type": "alert", "message": "title", "description": "details", "alertType": "info|success|warning|error"}``
            - ``{"type": "collapse", "items": [{"title": "Section", "content": "markdown body"}], "defaultOpen": false}``
            - ``{"type": "stat", "stats": [{"label": "Users", "value": 1234, "trend": "up", "trendValue": "+12%"}]}``
            - ``{"type": "image_buttons", "url": "image URL", "buttons": [{"label": "Click", "value": "click", "position": [50, 80], "style": "primary"}]}``
            - ``{"type": "scene3d", "shapes": [{"shape": "box|sphere|cylinder|cone|torus", "position": [0,0,0], "color": "#4096ff"}], "modelUrl": "URL to .glb/.gltf", "height": 400}``
            - ``{"type": "mermaid", "code": "graph LR\\n  A-->B", "title": "Flow Diagram"}``
            - ``{"type": "canvas", "width": 800, "height": 400, "grid": true, "background": "#fafafa", "backgroundImage": "url", "elements": [{"shape": "rect|circle|ellipse|diamond|arrow|line|text|image", ...}], "interactive": false}``
            - ``{"type": "dag", "nodes": [{"id": "1", "label": "Task", "status": "completed|running|pending|error", "icon": "emoji", "value": "click_val"}], "edges": [{"source": "1", "target": "2", "label": "edge", "animated": true}], "direction": "TB|LR|BT|RL", "height": 400, "interactive": false}``
            - ``{"type": "mindmap", "root": {"label": "Root", "children": [{"label": "Child", "color": "#1890ff", "children": [...]}]}, "direction": "LR|TB", "height": 400, "interactive": false}``

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

    # --- Convert local paths to file:// URLs for media blocks ---
    from .send_file import _path_to_file_url

    _URL_BLOCK_TYPES = {"image", "audio", "video", "file", "image_buttons"}
    for block in blocks:
        if block.get("type") in _URL_BLOCK_TYPES and block.get("url"):
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
