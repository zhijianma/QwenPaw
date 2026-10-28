# -*- coding: utf-8 -*-
"""Tests for deterministic browser acceptance datasets."""

from unittest.mock import Mock

from scripts.chat_refactor.benchmark_v2_browser import (
    INITIAL_PAGE_TURNS,
    TAIL_TURNS,
    TIMELINE_ROOT_SELECTOR,
    _timeline_root_count,
)
from scripts.chat_refactor.soak_v2_browser_heap import (
    TOOL_DENSE_BYTES,
    TOOL_DENSE_TURNS,
    TOOL_PAIRS_PER_TURN,
    _tool_page,
)


def test_timeline_root_metric_excludes_nested_bubbles() -> None:
    """The DOM budget must count BubbleList children, not nested cards."""
    page = Mock()
    page.locator.return_value.count.return_value = 6

    assert _timeline_root_count(page) == 6
    page.locator.assert_called_once_with(TIMELINE_ROOT_SELECTOR)
    assert TIMELINE_ROOT_SELECTOR == (".qwenpaw-bubble-list > .qwenpaw-bubble")


def test_long_browser_profile_uses_five_turn_pages() -> None:
    """The benchmark must exercise the production turn-page contract."""
    assert INITIAL_PAGE_TURNS == 5
    assert TAIL_TURNS == 5


def test_tool_dense_page_preserves_complete_turn_pairs() -> None:
    """The bounded UI page must retain complete tool call/result pairs."""
    page = _tool_page("chat-tool", 3)

    assert len(page["items"]) == 3
    assert page["view"]["head_turn_id"] == "chat-tool-turn-499"
    for turn in page["items"]:
        assert len(turn["messages"]) == 1 + 2 * TOOL_PAIRS_PER_TURN
        assert turn["messages"][0]["role"] == "user"
        for pair in range(TOOL_PAIRS_PER_TURN):
            call = turn["messages"][1 + pair * 2]
            result = turn["messages"][2 + pair * 2]
            call_data = call["blocks"][0]["content"]["data"]
            result_data = result["blocks"][0]["content"]["data"]
            assert call["metadata"]["type"] == "plugin_call"
            assert result["metadata"]["type"] == "plugin_call_output"
            assert call_data["call_id"] == result_data["call_id"]


def test_tool_dense_result_size_matches_frozen_logical_payload() -> None:
    """Each generated result must represent the frozen 256 MiB profile."""
    page = _tool_page("chat-tool", 1)
    result = page["items"][0]["messages"][2]
    output = result["blocks"][0]["content"]["data"]["output"]
    expected = TOOL_DENSE_BYTES // (TOOL_DENSE_TURNS * TOOL_PAIRS_PER_TURN)

    assert len(output.encode("utf-8")) == expected
