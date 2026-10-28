# -*- coding: utf-8 -*-
"""Deterministic transcript datasets for Chat-refactor acceptance."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from .integrity import TranscriptManifestBuilder


MIB = 1024 * 1024


@dataclass(frozen=True)
class DatasetSpec:
    """Frozen logical shape for one generated transcript."""

    name: str
    kind: str
    turn_count: int
    target_payload_bytes: int
    messages_per_turn: int = 0
    tool_pairs_per_turn: int = 0

    @property
    def expected_message_count(self) -> int:
        """Return the deterministic number of generated messages."""
        if self.kind == "conversation":
            return self.turn_count * self.messages_per_turn
        if self.kind == "tool_dense":
            return self.turn_count * (1 + 2 * self.tool_pairs_per_turn)
        if self.kind == "huge_block":
            return 1
        raise ValueError(f"Unsupported dataset kind: {self.kind}")

    @property
    def expected_block_count(self) -> int:
        """Return the deterministic number of generated blocks."""
        return self.expected_message_count


@dataclass(frozen=True)
class CopyDatasetSpec:
    """Frozen source/copy shape for independent-Chat benchmarks."""

    source_turns: int
    copy_count: int
    continuation_turns_per_copy: int


@dataclass(frozen=True)
class LiveRunDatasetSpec:
    """Frozen event rate and duration for bounded-resource soak tests."""

    duration_seconds: int
    batched_events_per_second: int
    tool_interval_seconds: int

    @property
    def expected_event_count(self) -> int:
        """Return the event count before tool structure events are added."""
        return self.duration_seconds * self.batched_events_per_second


@dataclass(frozen=True)
class ConcurrentDatasetSpec:
    """Frozen active-Chat and subscriber concurrency shape."""

    active_chats: int
    subscribers_per_run: int


DATASET_SPECS: dict[str, DatasetSpec] = {
    "typical": DatasetSpec(
        name="typical",
        kind="conversation",
        turn_count=100,
        messages_per_turn=5,
        target_payload_bytes=5 * MIB,
    ),
    "long": DatasetSpec(
        name="long",
        kind="conversation",
        turn_count=2_000,
        messages_per_turn=5,
        target_payload_bytes=100 * MIB,
    ),
    "extreme": DatasetSpec(
        name="extreme",
        kind="conversation",
        turn_count=10_000,
        messages_per_turn=5,
        target_payload_bytes=1_024 * MIB,
    ),
    "tool_dense": DatasetSpec(
        name="tool_dense",
        kind="tool_dense",
        turn_count=500,
        tool_pairs_per_turn=20,
        target_payload_bytes=256 * MIB,
    ),
    "huge_block": DatasetSpec(
        name="huge_block",
        kind="huge_block",
        turn_count=1,
        target_payload_bytes=64 * MIB,
    ),
}

COPY_DATASET_SPEC = CopyDatasetSpec(
    source_turns=2_000,
    copy_count=20,
    continuation_turns_per_copy=200,
)
LIVE_RUN_DATASET_SPEC = LiveRunDatasetSpec(
    duration_seconds=2 * 60 * 60,
    batched_events_per_second=20,
    tool_interval_seconds=30,
)
CONCURRENT_DATASET_SPEC = ConcurrentDatasetSpec(
    active_chats=8,
    subscribers_per_run=2,
)


def _sized_ascii(prefix: str, size: int) -> str:
    """Return exactly *size* UTF-8 bytes of deterministic ASCII text."""
    if size <= 0:
        return ""
    if len(prefix) >= size:
        return prefix[:size]
    return f"{prefix}{'x' * (size - len(prefix))}"


def _message(
    *,
    source_id: str,
    role: str,
    message_type: str,
    content: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "type": message_type,
        "role": role,
        "content": content,
        "status": "completed",
        "metadata": {
            "original_id": source_id,
            "timestamp": "2026-01-01T00:00:00+00:00",
        },
    }


def _iter_conversation(spec: DatasetSpec) -> Iterator[dict[str, Any]]:
    block_count = spec.expected_block_count
    block_bytes = spec.target_payload_bytes // block_count
    remainder = spec.target_payload_bytes % block_count
    block_index = 0
    for turn in range(spec.turn_count):
        for ordinal in range(spec.messages_per_turn):
            role = "user" if ordinal == 0 else "assistant"
            prefix = f"{spec.name}:t{turn}:m{ordinal}:"
            size = block_bytes + (1 if block_index < remainder else 0)
            text = _sized_ascii(prefix, size)
            yield _message(
                source_id=f"{spec.name}-t{turn}-m{ordinal}",
                role=role,
                message_type="message",
                content=[{"type": "text", "text": text}],
            )
            block_index += 1


def _iter_tool_dense(spec: DatasetSpec) -> Iterator[dict[str, Any]]:
    result_count = spec.turn_count * spec.tool_pairs_per_turn
    result_bytes = spec.target_payload_bytes // result_count
    remainder = spec.target_payload_bytes % result_count
    result_index = 0
    for turn in range(spec.turn_count):
        yield _message(
            source_id=f"{spec.name}-t{turn}-user",
            role="user",
            message_type="message",
            content=[{"type": "text", "text": f"tool turn {turn}"}],
        )
        for pair in range(spec.tool_pairs_per_turn):
            call_id = f"call-{turn}-{pair}"
            yield _message(
                source_id=f"{call_id}-request",
                role="assistant",
                message_type="plugin_call",
                content=[
                    {
                        "type": "data",
                        "data": {
                            "call_id": call_id,
                            "name": "benchmark_tool",
                            "arguments": f'{{"turn":{turn},"pair":{pair}}}',
                        },
                    },
                ],
            )
            size = result_bytes + (1 if result_index < remainder else 0)
            output = _sized_ascii(f"{call_id}:", size)
            yield _message(
                source_id=f"{call_id}-result",
                role="tool",
                message_type="plugin_call_output",
                content=[
                    {
                        "type": "data",
                        "data": {
                            "call_id": call_id,
                            "name": "benchmark_tool",
                            "output": output,
                        },
                    },
                ],
            )
            result_index += 1


def iter_dataset(spec: DatasetSpec) -> Iterator[dict[str, Any]]:
    """Yield one frozen dataset without retaining the transcript."""
    if spec.kind == "conversation":
        yield from _iter_conversation(spec)
        return
    if spec.kind == "tool_dense":
        yield from _iter_tool_dense(spec)
        return
    if spec.kind == "huge_block":
        text = _sized_ascii(
            f"{spec.name}:",
            spec.target_payload_bytes,
        )
        yield _message(
            source_id=f"{spec.name}-block",
            role="assistant",
            message_type="message",
            content=[{"type": "text", "text": text}],
        )
        return
    raise ValueError(f"Unsupported dataset kind: {spec.kind}")


def write_dataset(
    spec: DatasetSpec,
    output_dir: Path,
    *,
    overwrite: bool = False,
) -> Mapping[str, Any]:
    """Write JSONL plus its correctness manifest using atomic replacement."""
    output_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = output_dir / f"{spec.name}.jsonl"
    manifest_path = output_dir / f"{spec.name}.manifest.json"
    if not overwrite and (transcript_path.exists() or manifest_path.exists()):
        raise FileExistsError(f"Dataset already exists: {spec.name}")

    transcript_temp = transcript_path.with_suffix(".jsonl.tmp")
    manifest_temp = manifest_path.with_suffix(".json.tmp")
    builder = TranscriptManifestBuilder()
    serialized_bytes = 0
    started = time.perf_counter()
    try:
        with transcript_temp.open("wb") as stream:
            for message in iter_dataset(spec):
                builder.add(message)
                encoded = json.dumps(
                    message,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                stream.write(encoded)
                stream.write(b"\n")
                serialized_bytes += len(encoded) + 1
            stream.flush()
            os.fsync(stream.fileno())
        manifest = builder.build()
        payload = {
            "dataset_schema_version": 1,
            "spec": asdict(spec),
            "serialized_bytes": serialized_bytes,
            "generation_seconds": time.perf_counter() - started,
            "manifest": manifest.to_dict(),
        }
        manifest_temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(transcript_temp, transcript_path)
        os.replace(manifest_temp, manifest_path)
        return payload
    finally:
        transcript_temp.unlink(missing_ok=True)
        manifest_temp.unlink(missing_ok=True)
