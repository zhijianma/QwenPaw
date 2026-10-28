# -*- coding: utf-8 -*-
"""Correctness-oracle tests for Chat transcript migration."""

from __future__ import annotations

from copy import deepcopy

from qwenpaw.app.chats.integrity import build_transcript_manifest
from qwenpaw.schemas import DataContent, Message, MessageType, TextContent


def _message(
    text: str,
    *,
    role: str = "assistant",
    metadata: dict | None = None,
) -> Message:
    stored_metadata = {"original_id": f"source-{text}"}
    stored_metadata.update(metadata or {})
    return Message(
        role=role,
        content=[TextContent(text=text)],
        metadata=stored_metadata,
    ).completed()


def _tool_message(
    message_type: MessageType,
    *,
    call_id: str,
    name: str,
) -> Message:
    return Message(
        type=message_type,
        role="assistant",
        content=[DataContent(data={"call_id": call_id, "name": name})],
        metadata={"original_id": f"source-{message_type.value}"},
    ).completed()


def test_manifest_is_stable_across_derived_message_ids() -> None:
    left = _message("same", role="user")
    right = _message("same", role="user")

    assert left.id != right.id
    assert build_transcript_manifest([left]) == build_transcript_manifest(
        [right],
    )


def test_manifest_detects_missing_duplicate_and_reordered_messages() -> None:
    messages = [
        _message("one", role="user"),
        _message("two"),
        _message("three", role="user"),
    ]
    expected = build_transcript_manifest(messages)

    missing = build_transcript_manifest(messages[:-1])
    duplicated = build_transcript_manifest([*messages, messages[-1]])
    reordered = build_transcript_manifest(
        [messages[1], messages[0], messages[2]],
    )

    assert missing.transcript_hash != expected.transcript_hash
    assert duplicated.transcript_hash != expected.transcript_hash
    assert reordered.transcript_hash != expected.transcript_hash
    assert missing.message_count == expected.message_count - 1
    assert duplicated.message_count == expected.message_count + 1


def test_manifest_detects_block_content_changes() -> None:
    original = _message("完整内容")
    changed = deepcopy(original)
    changed.content[0].text = "内容发生变化"

    expected = build_transcript_manifest([original])
    actual = build_transcript_manifest([changed])

    assert actual.block_count == expected.block_count
    assert actual.message_hashes != expected.message_hashes
    assert actual.transcript_hash != expected.transcript_hash


def test_manifest_reports_tool_link_mismatch() -> None:
    call = _tool_message(
        MessageType.PLUGIN_CALL,
        call_id="call-1",
        name="read_file",
    )
    result = _tool_message(
        MessageType.PLUGIN_CALL_OUTPUT,
        call_id="call-2",
        name="read_file",
    )

    manifest = build_transcript_manifest([call, result])

    assert manifest.orphan_tool_calls == ("call-1",)
    assert manifest.orphan_tool_results == ("call-2",)


def test_manifest_preserves_valid_tool_positions_and_names() -> None:
    call = _tool_message(
        MessageType.PLUGIN_CALL,
        call_id="call-1",
        name="read_file",
    )
    result = _tool_message(
        MessageType.PLUGIN_CALL_OUTPUT,
        call_id="call-1",
        name="read_file",
    )

    manifest = build_transcript_manifest([call, result])

    assert not manifest.orphan_tool_calls
    assert not manifest.orphan_tool_results
    assert manifest.tool_links[0].call_positions == (0,)
    assert manifest.tool_links[0].result_positions == (1,)
    assert manifest.tool_links[0].call_names == ("read_file",)
    assert manifest.tool_links[0].result_names == ("read_file",)
    assert not manifest.duplicate_tool_calls
    assert not manifest.duplicate_tool_results
    assert not manifest.tool_name_mismatches


def test_manifest_reports_duplicate_and_misnamed_tool_links() -> None:
    call = _tool_message(
        MessageType.PLUGIN_CALL,
        call_id="call-1",
        name="read_file",
    )
    duplicate = deepcopy(call)
    result = _tool_message(
        MessageType.PLUGIN_CALL_OUTPUT,
        call_id="call-1",
        name="write_file",
    )

    manifest = build_transcript_manifest([call, duplicate, result])

    assert manifest.duplicate_tool_calls == ("call-1",)
    assert not manifest.duplicate_tool_results
    assert manifest.tool_name_mismatches == ("call-1",)


def test_manifest_consumes_input_iterable_once() -> None:
    class OnePassMessages:
        def __init__(self) -> None:
            self.iterations = 0

        def __iter__(self):
            self.iterations += 1
            if self.iterations > 1:
                raise AssertionError("messages were iterated more than once")
            yield _message("one")
            yield _message("two")

    messages = OnePassMessages()

    manifest = build_transcript_manifest(messages)

    assert manifest.message_count == 2
    assert messages.iterations == 1


def test_manifest_mapping_key_order_does_not_change_hash() -> None:
    first = _tool_message(
        MessageType.PLUGIN_CALL,
        call_id="call-1",
        name="search",
    ).model_dump(mode="json")
    second = deepcopy(first)
    data = second["content"][0]["data"]
    second["content"][0]["data"] = {
        "name": data["name"],
        "call_id": data["call_id"],
    }

    assert build_transcript_manifest([first]) == build_transcript_manifest(
        [second],
    )


def test_manifest_tracks_served_and_nested_usage() -> None:
    served = _message(
        "served",
        metadata={
            "qwenpaw_turn_usage": {"usage": {"total_tokens": 10}},
        },
    )
    nested = _message(
        "nested",
        metadata={
            "metadata": {
                "qwenpaw_turn_usage": {"usage": {"total_tokens": 20}},
            },
        },
    )

    manifest = build_transcript_manifest([served, nested])

    assert len(manifest.usage_hashes) == 2
    assert manifest.usage_hashes[0] != manifest.usage_hashes[1]


def test_manifest_detects_usage_changes() -> None:
    original = _message(
        "answer",
        metadata={
            "qwenpaw_turn_usage": {"usage": {"total_tokens": 10}},
        },
    )
    changed = _message(
        "answer",
        metadata={
            "qwenpaw_turn_usage": {"usage": {"total_tokens": 11}},
        },
    )

    expected = build_transcript_manifest([original])
    actual = build_transcript_manifest([changed])

    assert expected.usage_hashes != actual.usage_hashes
    assert expected.transcript_hash != actual.transcript_hash
