# -*- coding: utf-8 -*-
"""Unit tests for the a2ui tool."""
import os
import tempfile

import pytest

from qwenpaw.agents.tools.a2ui import a2ui


@pytest.mark.asyncio
async def test_a2ui_returns_displayed():
    """Non-blocking call returns confirmation text."""
    result = await a2ui(blocks=[{"type": "text", "content": "hello"}])
    texts = [b["text"] for b in result.content if b.get("type") == "text"]
    assert any("Displayed" in t for t in texts)


@pytest.mark.asyncio
async def test_a2ui_with_title():
    """Title param is accepted without error."""
    result = await a2ui(
        blocks=[{"type": "text", "content": "hi"}],
        title="My Title",
    )
    texts = [b["text"] for b in result.content if b.get("type") == "text"]
    assert any("Displayed" in t for t in texts)


@pytest.mark.asyncio
async def test_a2ui_empty_blocks_error():
    """Empty blocks list returns error."""
    result = await a2ui(blocks=[])
    texts = [b["text"] for b in result.content if b.get("type") == "text"]
    assert any("Error" in t for t in texts)


@pytest.mark.asyncio
async def test_a2ui_invalid_block_no_type():
    """Block without 'type' field returns error."""
    result = await a2ui(blocks=[{"content": "missing type"}])
    texts = [b["text"] for b in result.content if b.get("type") == "text"]
    assert any("Error" in t for t in texts)


@pytest.mark.asyncio
async def test_a2ui_converts_local_image_path():
    """Local image path is converted to file:// URL."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"\x89PNG")
        tmp_path = f.name
    try:
        blocks = [{"type": "image", "url": tmp_path}]
        await a2ui(blocks=blocks)
        assert blocks[0]["url"].startswith("file://")
    finally:
        os.unlink(tmp_path)


@pytest.mark.asyncio
async def test_a2ui_skips_http_image_url():
    """HTTP URLs are not converted."""
    blocks = [{"type": "image", "url": "https://example.com/img.png"}]
    await a2ui(blocks=blocks)
    assert blocks[0]["url"] == "https://example.com/img.png"


@pytest.mark.asyncio
async def test_a2ui_multiple_blocks():
    """Multiple mixed block types are accepted."""
    result = await a2ui(blocks=[
        {"type": "text", "content": "hello"},
        {"type": "code", "language": "python", "content": "x = 1"},
        {"type": "table", "headers": ["A"], "rows": [["1"]]},
    ])
    texts = [b["text"] for b in result.content if b.get("type") == "text"]
    assert any("Displayed" in t for t in texts)
