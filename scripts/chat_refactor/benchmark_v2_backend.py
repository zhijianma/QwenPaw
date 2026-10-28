# -*- coding: utf-8 -*-
"""Benchmark complete transcript reads from normalized Chat storage."""

from __future__ import annotations

import argparse
import gc
import json
import platform
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from qwenpaw.app.chats.acceptance_datasets import (
    DATASET_SPECS,
    DatasetSpec,
    iter_dataset,
)
from qwenpaw.app.chats.integrity import build_transcript_manifest
from qwenpaw.app.chats.migration.normalize import NormalizedTranscript
from qwenpaw.app.chats.migration.snapshot import LegacySnapshotManifest
from qwenpaw.app.chats.storage import (
    materialize_normalized_transcript,
    read_transcript,
)
from qwenpaw.schemas import Message


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "profiles",
        nargs="+",
        choices=["typical", "long"],
    )
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--git-commit", default="unknown")
    return parser.parse_args()


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction + 0.999) - 1)
    return ordered[index]


def _summary(samples: list[dict[str, float]]) -> dict[str, float | int]:
    totals = [sample["total_ms"] for sample in samples]
    return {
        "count": len(totals),
        "p50_ms": _percentile(totals, 0.5),
        "p95_ms": _percentile(totals, 0.95),
        "max_ms": max(totals),
    }


def _messages(spec: DatasetSpec) -> tuple[Message, ...]:
    return tuple(Message.model_validate(item) for item in iter_dataset(spec))


def _snapshot(conversation_id: str, session_id: str) -> LegacySnapshotManifest:
    return LegacySnapshotManifest(
        version=1,
        conversation_id=conversation_id,
        session_id=session_id,
        files=(),
        history_rows_path=None,
        history_rows=0,
        history_min_seq=None,
        history_max_seq=None,
        history_sha256=None,
        manifest_sha256="benchmark",
    )


def benchmark_spec(
    spec: DatasetSpec,
    *,
    sample_count: int,
) -> dict[str, Any]:
    """Materialize once, prove correctness, then time immutable reads."""
    if sample_count < 1:
        raise ValueError("sample_count must be positive")
    conversation_id = str(uuid5(NAMESPACE_URL, f"qwenpaw:{spec.name}"))
    session_id = f"benchmark:{spec.name}"
    with tempfile.TemporaryDirectory(
        prefix=f"qwenpaw-v2-{spec.name}-",
    ) as raw_directory:
        path = Path(raw_directory) / f"{conversation_id}.db"
        messages = _messages(spec)
        expected = build_transcript_manifest(messages)
        materialize_normalized_transcript(
            path,
            conversation_id=conversation_id,
            agent_id="benchmark",
            owner_scope="benchmark:local",
            backend="qwenpaw",
            normalized=NormalizedTranscript(
                messages=messages,
                manifest=expected,
                history_parts=0,
                session_parts=len(messages),
                deduplicated_parts=0,
            ),
            snapshot=_snapshot(conversation_id, session_id),
            now_us=1,
            allocated_recall_sequences=range(
                1,
                spec.expected_block_count + 1,
            ),
        )
        del messages
        gc.collect()
        warmup = read_transcript(path)
        actual = build_transcript_manifest(warmup)
        if actual != expected:
            raise AssertionError(f"{spec.name}: correctness manifest changed")
        del warmup
        gc.collect()
        samples: list[dict[str, float]] = []
        for index in range(sample_count):
            started = time.perf_counter()
            transcript = read_transcript(path)
            finished = time.perf_counter()
            if len(transcript) != expected.message_count:
                raise AssertionError(
                    f"{spec.name}: expected {expected.message_count} "
                    f"messages, got {len(transcript)}",
                )
            samples.append(
                {
                    "index": float(index),
                    "total_ms": (finished - started) * 1_000,
                },
            )
            del transcript
            gc.collect()
        return {
            "scenario": spec.name,
            "database_bytes": path.stat().st_size,
            "cache_state": "warm-os-page-cache-after-one-warmup",
            "correctness_manifest": expected.to_dict(),
            "samples": samples,
            "summary": _summary(samples),
        }


def main() -> int:
    """Run selected profiles and save raw samples as JSON."""
    args = _parse_args()
    results = [
        benchmark_spec(DATASET_SPECS[name], sample_count=args.samples)
        for name in args.profiles
    ]
    payload = {
        "schema_version": 1,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "git_commit": args.git_commit,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    for result in results:
        print(json.dumps(result["summary"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
