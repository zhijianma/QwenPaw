# -*- coding: utf-8 -*-
"""Benchmark the legacy session JSON to Chat history read path."""

from __future__ import annotations

import argparse
import gc
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from agentscope.message import Msg
from agentscope.state import AgentState

from qwenpaw.app.chats.acceptance_datasets import (
    DATASET_SPECS,
    DatasetSpec,
    iter_dataset,
)
from qwenpaw.app.chats.session import _read_session_json
from qwenpaw.app.chats.utils import agentscope_msg_to_message


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


def _legacy_context(spec: DatasetSpec) -> list[Msg]:
    messages: list[Msg] = []
    for item in iter_dataset(spec):
        content = item["content"]
        messages.append(
            Msg(
                name=str(item["role"]),
                role=str(item["role"]),
                content=content,
                id=str(item["metadata"]["original_id"]),
                created_at=str(item["metadata"]["timestamp"]),
            ),
        )
    return messages


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


def _write_legacy_session(spec: DatasetSpec, path: Path) -> int:
    context = _legacy_context(spec)
    state = AgentState(
        session_id=f"baseline-{spec.name}",
        context=context,
    )
    payload = {"agent": {"state": state.model_dump(mode="json")}}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    path.write_bytes(encoded)
    return len(encoded)


def benchmark_spec(
    spec: DatasetSpec,
    *,
    sample_count: int,
) -> dict[str, Any]:
    """Measure the exact legacy parse, validation, and conversion stages."""
    if sample_count < 1:
        raise ValueError("sample_count must be positive")
    handle, raw_path = tempfile.mkstemp(
        prefix=f"qwenpaw-{spec.name}-",
        suffix=".json",
    )
    os.close(handle)
    path = Path(raw_path)
    try:
        serialized_bytes = _write_legacy_session(spec, path)
        samples: list[dict[str, float]] = []
        for index in range(sample_count + 1):
            started = time.perf_counter()
            payload = _read_session_json(str(path))
            parsed = time.perf_counter()
            state = AgentState.model_validate(payload["agent"]["state"])
            validated = time.perf_counter()
            messages = agentscope_msg_to_message(
                list(state.context),
                truncate_oversized_outputs=True,
            )
            finished = time.perf_counter()
            if len(messages) != spec.expected_message_count:
                raise AssertionError(
                    f"{spec.name}: expected {spec.expected_message_count} "
                    f"messages, got {len(messages)}",
                )
            if index > 0:
                samples.append(
                    {
                        "index": float(index - 1),
                        "read_parse_ms": (parsed - started) * 1_000,
                        "validate_ms": (validated - parsed) * 1_000,
                        "convert_ms": (finished - validated) * 1_000,
                        "total_ms": (finished - started) * 1_000,
                    },
                )
            del payload, state, messages
            gc.collect()
        return {
            "scenario": spec.name,
            "serialized_bytes": serialized_bytes,
            "cache_state": "warm-os-page-cache-after-one-warmup",
            "samples": samples,
            "summary": _summary(samples),
        }
    finally:
        path.unlink(missing_ok=True)


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
