# -*- coding: utf-8 -*-
"""Benchmark byte-bounded normalized Chat page reads."""

from __future__ import annotations

import argparse
import gc
import json
import platform
import tempfile
import time
from pathlib import Path
from typing import Any, Callable
from uuid import NAMESPACE_URL, uuid5

from qwenpaw.app.chats.acceptance_datasets import (
    DATASET_SPECS,
    DatasetSpec,
    iter_dataset,
)
from qwenpaw.app.chats.integrity import build_transcript_manifest
from qwenpaw.app.chats.migration.normalize import NormalizedTranscript
from qwenpaw.app.chats.migration.snapshot import LegacySnapshotManifest
from qwenpaw.app.chats.pagination_cursor import ChatCursorCodec
from qwenpaw.app.chats.storage import materialize_normalized_transcript
from qwenpaw.app.chats.storage.common import connect_database
from qwenpaw.app.chats.storage.pagination_repository import (
    PAGE_MAX_BYTES,
    read_block_fragment,
    read_turn_page,
)
from qwenpaw.schemas import Message


_PROFILES = ("typical", "long", "extreme", "tool_dense", "huge_block")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profiles", nargs="+", choices=_PROFILES)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--git-commit", default="unknown")
    return parser.parse_args()


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction + 0.999) - 1)
    return ordered[index]


def _summary(samples: list[dict[str, float]]) -> dict[str, float | int]:
    values = [sample["duration_ms"] for sample in samples]
    return {
        "count": len(values),
        "p50_ms": _percentile(values, 0.5),
        "p95_ms": _percentile(values, 0.95),
        "max_ms": max(values),
    }


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


def _measure(
    operation: Callable[[], Any],
    *,
    sample_count: int,
) -> tuple[list[dict[str, float]], Any]:
    samples: list[dict[str, float]] = []
    result = operation()
    for index in range(sample_count):
        started = time.perf_counter()
        result = operation()
        finished = time.perf_counter()
        samples.append(
            {
                "index": float(index),
                "duration_ms": (finished - started) * 1_000,
            },
        )
    return samples, result


def benchmark_spec(
    spec: DatasetSpec,
    *,
    sample_count: int,
) -> dict[str, Any]:
    """Materialize one verified transcript and benchmark bounded reads."""
    if sample_count < 1:
        raise ValueError("sample_count must be positive")
    conversation_id = str(uuid5(NAMESPACE_URL, f"qwenpaw:page:{spec.name}"))
    session_id = f"benchmark:{spec.name}"
    messages = tuple(
        Message.model_validate(item) for item in iter_dataset(spec)
    )
    expected = build_transcript_manifest(messages)
    with tempfile.TemporaryDirectory(
        prefix=f"qwenpaw-pages-{spec.name}-",
    ) as raw_directory:
        path = Path(raw_directory) / f"{conversation_id}.db"
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
        codec = ChatCursorCodec(b"p" * 32)
        turn_operation = lambda: read_turn_page(
            path,
            conversation_id=conversation_id,
            subject="benchmark",
            codec=codec,
        )
        turn_samples, turn_page = _measure(
            turn_operation,
            sample_count=sample_count,
        )
        if not turn_page.items or turn_page.bytes > PAGE_MAX_BYTES:
            raise AssertionError(f"{spec.name}: invalid bounded turn page")
        metrics: dict[str, Any] = {
            "turn_tail": {
                "samples": turn_samples,
                "summary": _summary(turn_samples),
                "response_bytes": turn_page.bytes,
                "items": len(turn_page.items),
            },
        }
        if spec.kind == "huge_block":
            block_id = str(
                turn_page.items[-1]["messages"][-1]["blocks"][-1]["block_id"],
            )
            fragment_operation = lambda: read_block_fragment(
                path,
                conversation_id=conversation_id,
                block_id=block_id,
                subject="benchmark",
                codec=codec,
                head_turn_id=turn_page.view.head_turn_id,
                view_version=turn_page.view.view_version,
            )
            fragment_samples, fragment = _measure(
                fragment_operation,
                sample_count=sample_count,
            )
            if fragment.bytes > PAGE_MAX_BYTES:
                raise AssertionError("huge_block: fragment exceeded page cap")
            metrics["block_fragment"] = {
                "samples": fragment_samples,
                "summary": _summary(fragment_samples),
                "response_bytes": fragment.bytes,
            }
        with connect_database(path, readonly=True) as connection:
            counts = {
                "turns": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM chat_turns",
                    ).fetchone()[0],
                ),
                "messages": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM chat_messages",
                    ).fetchone()[0],
                ),
                "blocks": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM chat_blocks",
                    ).fetchone()[0],
                ),
            }
        if counts["messages"] != expected.message_count:
            raise AssertionError(f"{spec.name}: transcript count changed")
        return {
            "scenario": spec.name,
            "database_bytes": path.stat().st_size,
            "cache_state": "warm-os-page-cache-after-one-warmup",
            "correctness_manifest": expected.to_dict(),
            "stored_counts": counts,
            "metrics": metrics,
        }


def main() -> int:
    """Run selected profiles and persist every raw timing sample."""
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
        "samples_per_metric": args.samples,
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    for result in results:
        summaries = {
            name: metric["summary"]
            for name, metric in result["metrics"].items()
        }
        print(json.dumps({result["scenario"]: summaries}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
