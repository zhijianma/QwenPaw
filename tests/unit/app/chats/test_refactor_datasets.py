# -*- coding: utf-8 -*-
"""Frozen dataset tests for Chat-refactor acceptance."""

from __future__ import annotations

import json

from qwenpaw.app.chats.acceptance_datasets import (
    CONCURRENT_DATASET_SPEC,
    COPY_DATASET_SPEC,
    DATASET_SPECS,
    LIVE_RUN_DATASET_SPEC,
    DatasetSpec,
    iter_dataset,
    write_dataset,
)


def test_frozen_dataset_shapes_match_acceptance_plan() -> None:
    assert DATASET_SPECS["typical"].expected_block_count == 500
    assert DATASET_SPECS["long"].expected_message_count == 10_000
    assert DATASET_SPECS["extreme"].expected_message_count == 50_000
    tool_dense = DATASET_SPECS["tool_dense"]
    assert tool_dense.turn_count * tool_dense.tool_pairs_per_turn == 10_000
    assert DATASET_SPECS["huge_block"].target_payload_bytes == 64 * 1024 * 1024
    assert COPY_DATASET_SPEC.source_turns == 2_000
    assert COPY_DATASET_SPEC.copy_count == 20
    assert COPY_DATASET_SPEC.continuation_turns_per_copy == 200
    assert LIVE_RUN_DATASET_SPEC.expected_event_count == 144_000
    assert LIVE_RUN_DATASET_SPEC.tool_interval_seconds == 30
    assert CONCURRENT_DATASET_SPEC.active_chats == 8
    assert CONCURRENT_DATASET_SPEC.subscribers_per_run == 2


def test_conversation_generator_is_deterministic() -> None:
    spec = DatasetSpec(
        name="small",
        kind="conversation",
        turn_count=2,
        messages_per_turn=3,
        target_payload_bytes=101,
    )

    first = list(iter_dataset(spec))
    second = list(iter_dataset(spec))

    assert first == second
    assert len(first) == spec.expected_message_count
    payload_bytes = sum(
        len(message["content"][0]["text"].encode("utf-8")) for message in first
    )
    assert payload_bytes == spec.target_payload_bytes


def test_tool_dataset_has_stable_call_result_pairs() -> None:
    spec = DatasetSpec(
        name="tools",
        kind="tool_dense",
        turn_count=2,
        tool_pairs_per_turn=3,
        target_payload_bytes=97,
    )

    messages = list(iter_dataset(spec))
    calls = [item for item in messages if item["type"] == "plugin_call"]
    results = [
        item for item in messages if item["type"] == "plugin_call_output"
    ]

    assert len(calls) == 6
    assert len(results) == 6
    assert calls[0]["content"][0]["data"]["call_id"] == (
        results[0]["content"][0]["data"]["call_id"]
    )


def test_writer_emits_jsonl_and_matching_manifest(tmp_path) -> None:
    spec = DatasetSpec(
        name="write-small",
        kind="conversation",
        turn_count=3,
        messages_per_turn=2,
        target_payload_bytes=256,
    )

    payload = write_dataset(spec, tmp_path)
    lines = (
        (tmp_path / "write-small.jsonl")
        .read_text(
            encoding="utf-8",
        )
        .splitlines()
    )
    manifest = json.loads(
        (tmp_path / "write-small.manifest.json").read_text(
            encoding="utf-8",
        ),
    )

    assert len(lines) == spec.expected_message_count
    assert manifest["manifest"]["message_count"] == len(lines)
    assert manifest["manifest"]["block_count"] == len(lines)
    assert payload["manifest"]["transcript_hash"] == (
        manifest["manifest"]["transcript_hash"]
    )


def test_writer_refuses_to_overwrite_by_default(tmp_path) -> None:
    spec = DatasetSpec(
        name="existing",
        kind="conversation",
        turn_count=1,
        messages_per_turn=1,
        target_payload_bytes=8,
    )
    write_dataset(spec, tmp_path)

    try:
        write_dataset(spec, tmp_path)
    except FileExistsError as exc:
        assert "existing" in str(exc)
    else:
        raise AssertionError("writer overwrote an existing dataset")
