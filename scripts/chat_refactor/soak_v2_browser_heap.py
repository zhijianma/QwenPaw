# -*- coding: utf-8 -*-
"""Measure retained Chromium heap while switching bounded Chat profiles."""

# pylint: disable=wrong-import-position

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import psutil
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.chat_refactor.benchmark_v2_browser import (  # noqa: E402
    DEFAULT_CHROME,
    TIMELINE_ROOT_SELECTOR,
    FrozenChatApi,
    _chat_ids,
    _frontend_mode,
    _navigate,
    _new_page,
    _node_version,
    _power_state,
    _timeline_root_count,
    _wait_ready,
)

TOOL_DENSE_TURNS = 500
TOOL_PAIRS_PER_TURN = 20
TOOL_DENSE_BYTES = 256 * 1024 * 1024


def _tool_message(
    chat_id: str,
    turn_index: int,
    ordinal: int,
) -> dict[str, Any]:
    is_user = ordinal == 0
    pair = max(0, (ordinal - 1) // 2)
    is_call = not is_user and ordinal % 2 == 1
    call_id = f"call-{turn_index}-{pair}"
    if is_user:
        role = "user"
        message_type = "message"
        marker = (
            f" tail-marker-{chat_id}"
            if turn_index == TOOL_DENSE_TURNS - 1
            else ""
        )
        content: dict[str, Any] = {
            "type": "text",
            "text": f"tool turn {turn_index}{marker}",
        }
    elif is_call:
        role = "assistant"
        message_type = "plugin_call"
        content = {
            "type": "data",
            "data": {
                "call_id": call_id,
                "name": "benchmark_tool",
                "arguments": f'{{"turn":{turn_index},"pair":{pair}}}',
            },
        }
    else:
        role = "tool"
        message_type = "plugin_call_output"
        prefix = f"{call_id}:"
        result_bytes = TOOL_DENSE_BYTES // (
            TOOL_DENSE_TURNS * TOOL_PAIRS_PER_TURN
        )
        output = f"{prefix}{'x' * (result_bytes - len(prefix))}"
        content = {
            "type": "data",
            "data": {
                "call_id": call_id,
                "name": "benchmark_tool",
                "output": output,
            },
        }
    message_id = f"{chat_id}-message-{turn_index}-{ordinal}"
    timestamp = turn_index * 100 + ordinal
    return {
        "message_id": message_id,
        "turn_id": f"{chat_id}-turn-{turn_index}",
        "role": role,
        "ordinal": ordinal,
        "state": "completed",
        "metadata": {
            "type": message_type,
            "timestamp": "2026-01-01T00:00:00Z",
        },
        "metadata_complete": True,
        "created_at_us": timestamp,
        "updated_at_us": timestamp,
        "blocks": [
            {
                "block_id": f"{message_id}-block",
                "message_id": message_id,
                "kind": content["type"],
                "ordinal": 0,
                "state": "completed",
                "total_bytes": len(json.dumps(content)),
                "content_sha256": None,
                "entity_version": 1,
                "metadata": {},
                "metadata_complete": True,
                "created_at_us": timestamp,
                "updated_at_us": timestamp,
                "content": content,
                "content_loaded": True,
                "content_cursor": None,
            },
        ],
        "child_cursor": None,
        "children_complete": True,
    }


def _tool_turn(chat_id: str, turn_index: int) -> dict[str, Any]:
    message_count = 1 + 2 * TOOL_PAIRS_PER_TURN
    timestamp = turn_index * 100
    return {
        "turn_id": f"{chat_id}-turn-{turn_index}",
        "parent_turn_id": (
            f"{chat_id}-turn-{turn_index - 1}" if turn_index else None
        ),
        "state": "completed",
        "lineage_depth": turn_index,
        "usage": None,
        "usage_complete": True,
        "error_digest": None,
        "created_at_us": timestamp,
        "updated_at_us": timestamp + message_count,
        "terminal_at_us": timestamp + message_count,
        "messages": [
            _tool_message(chat_id, turn_index, ordinal)
            for ordinal in range(message_count)
        ],
        "child_cursor": None,
        "children_complete": True,
    }


def _tool_page(chat_id: str, limit: int) -> dict[str, Any]:
    page_turns = min(3, limit)
    return {
        "items": [
            _tool_turn(chat_id, index)
            for index in range(
                TOOL_DENSE_TURNS - page_turns,
                TOOL_DENSE_TURNS,
            )
        ],
        "older_cursor": f"older-{chat_id}",
        "newer_cursor": None,
        "view": {
            "conversation_id": chat_id,
            "head_turn_id": f"{chat_id}-turn-499",
            "view_version": f"view-{chat_id}",
        },
        "bytes": page_turns
        * TOOL_PAIRS_PER_TURN
        * (TOOL_DENSE_BYTES // (TOOL_DENSE_TURNS * TOOL_PAIRS_PER_TURN)),
        "truncated_entity": False,
    }


class ToolDenseChatApi(FrozenChatApi):
    """Serve bounded pages from the frozen Tool dense dataset."""

    def _chat_body(self, chat_id: str, path: str, query: str) -> Any:
        if path.endswith("/view"):
            body = super()._chat_body(chat_id, path, query)
            body["head_turn_id"] = f"{chat_id}-turn-499"
            return body
        if path.endswith("/turns"):
            limit = int(parse_qs(query).get("limit", [5])[0])
            return _tool_page(chat_id, limit)
        return super()._chat_body(chat_id, path, query)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-seconds", type=int, default=30 * 60)
    parser.add_argument("--sample-interval-seconds", type=int, default=30)
    parser.add_argument("--base-url", default="http://localhost:5173")
    parser.add_argument("--chrome", type=Path, default=DEFAULT_CHROME)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--git-commit", default="unknown")
    parser.add_argument(
        "--scenario",
        choices=("long", "tool-dense"),
        default="long",
    )
    return parser.parse_args()


def _heap_sample(session: Any, page: Any, elapsed: float) -> dict[str, Any]:
    session.send("HeapProfiler.collectGarbage")
    usage = session.send("Runtime.getHeapUsage")
    counters = session.send("Memory.getDOMCounters")
    return {
        "elapsed_seconds": elapsed,
        "used_heap_bytes": usage["usedSize"],
        "total_heap_bytes": usage["totalSize"],
        "dom_nodes": counters["nodes"],
        "documents": counters["documents"],
        "js_event_listeners": counters["jsEventListeners"],
        "timeline_roots": _timeline_root_count(page),
    }


def main() -> int:
    """Run the retained-heap soak and save every sample."""
    args = _parse_args()
    if args.duration_seconds < 1 or args.sample_interval_seconds < 1:
        raise ValueError("duration and interval must be positive")
    api_type = (
        ToolDenseChatApi if args.scenario == "tool-dense" else FrozenChatApi
    )
    api = api_type(_chat_ids(1))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path=str(args.chrome),
        )
        context, page, faults = _new_page(browser, api)
        first, second = api.chat_ids[-2:]
        page.goto(
            f"{args.base_url}/chat/{first}",
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        _wait_ready(page, first)
        _navigate(page, second)
        session = context.new_cdp_session(page)
        session.send("Performance.enable")
        session.send("HeapProfiler.enable")
        started = time.monotonic()
        samples = [_heap_sample(session, page, 0.0)]
        index = 0
        while True:
            elapsed = time.monotonic() - started
            if elapsed >= args.duration_seconds:
                break
            remaining = min(
                args.sample_interval_seconds,
                args.duration_seconds - elapsed,
            )
            time.sleep(remaining)
            target = first if index % 2 == 0 else second
            timings = _navigate(page, target)
            elapsed = time.monotonic() - started
            sample = _heap_sample(session, page, elapsed)
            sample.update(timings)
            samples.append(sample)
            index += 1
            print(json.dumps(sample), flush=True)
        browser_version = browser.version
        context.close()
        browser.close()
    if any(faults.values()):
        raise AssertionError(f"browser faults: {faults}")
    baseline = samples[0]["used_heap_bytes"]
    final = samples[-1]["used_heap_bytes"]
    growth = ((final - baseline) / baseline) * 100 if baseline else 0.0
    baseline_nodes = samples[0]["dom_nodes"]
    final_nodes = samples[-1]["dom_nodes"]
    baseline_listeners = samples[0]["js_event_listeners"]
    final_listeners = samples[-1]["js_event_listeners"]
    payload = {
        "schema_version": 1,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "git_commit": args.git_commit,
        "platform": platform.platform(),
        "environment": {
            "machine": platform.machine(),
            "processor": platform.processor(),
            "cpu_count": os.cpu_count(),
            "physical_memory_bytes": psutil.virtual_memory().total,
            "python_version": platform.python_version(),
            "node_version": _node_version(),
            "browser_name": "Google Chrome via Playwright Chromium",
            "browser_version": browser_version,
            "browser_executable": str(args.chrome),
            "power_state": _power_state(),
            "frontend_mode": _frontend_mode(args.base_url),
        },
        "scenario": f"{args.scenario}-two-chat-switch",
        "metric_contract": {
            "timeline_roots": {
                "selector": TIMELINE_ROOT_SELECTOR,
                "semantics": "direct BubbleList message children",
            },
        },
        "duration_seconds": args.duration_seconds,
        "sample_interval_seconds": args.sample_interval_seconds,
        "samples": samples,
        "summary": {
            "baseline_used_heap_bytes": baseline,
            "final_used_heap_bytes": final,
            "peak_used_heap_bytes": max(
                sample["used_heap_bytes"] for sample in samples
            ),
            "final_growth_percent": growth,
            "baseline_dom_nodes": baseline_nodes,
            "final_dom_nodes": final_nodes,
            "dom_nodes_delta": final_nodes - baseline_nodes,
            "max_dom_nodes": max(sample["dom_nodes"] for sample in samples),
            "baseline_js_event_listeners": baseline_listeners,
            "final_js_event_listeners": final_listeners,
            "js_event_listeners_delta": (final_listeners - baseline_listeners),
            "max_timeline_roots": max(
                sample["timeline_roots"] for sample in samples
            ),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "correctness": "passed",
                "output": str(args.output),
                "summary": payload["summary"],
            },
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
