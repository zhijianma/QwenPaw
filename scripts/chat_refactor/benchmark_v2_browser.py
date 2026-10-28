# -*- coding: utf-8 -*-
"""Benchmark bounded V2 Chat rendering in a real Chromium browser."""

# pylint: disable=too-many-return-statements,wrong-import-position

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from statistics import mean, pstdev
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

import psutil
from playwright.sync_api import Browser, BrowserContext, Page, Route
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "e2e"))

from mocks import register_all  # noqa: E402


DEFAULT_CHROME = Path(
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)
TAIL_TURNS = 5
INITIAL_PAGE_TURNS = 5
MESSAGES_PER_TURN = 5
TEXT_BYTES = 8_000
LOGICAL_TURNS = 2_000
TIMELINE_ROOT_SELECTOR = ".qwenpaw-bubble-list > .qwenpaw-bubble"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--base-url", default="http://localhost:5173")
    parser.add_argument("--chrome", type=Path, default=DEFAULT_CHROME)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trace-directory", type=Path, required=True)
    parser.add_argument("--git-commit", default="unknown")
    return parser.parse_args()


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction + 0.999) - 1)
    return ordered[index]


def _summary(values: list[float]) -> dict[str, float | int]:
    return {
        "count": len(values),
        "p50_ms": _percentile(values, 0.5),
        "p95_ms": _percentile(values, 0.95),
        "max_ms": max(values),
        "mean_ms": mean(values),
        "stddev_ms": pstdev(values),
    }


def _node_version() -> str:
    try:
        return subprocess.check_output(
            ["node", "--version"],
            text=True,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return "unavailable"


def _power_state() -> str:
    battery = psutil.sensors_battery()
    if battery is None:
        return "no-battery-detected"
    return "plugged" if battery.power_plugged else "battery"


def _frontend_mode(base_url: str) -> str:
    try:
        with urlopen(base_url, timeout=5) as response:  # noqa: S310
            body = response.read(128 * 1024).decode("utf-8", errors="replace")
    except OSError:
        return "unavailable"
    return "vite-development" if "/@vite/client" in body else "built"


def _chat_ids(sample_count: int) -> list[str]:
    return [f"chat-long-{index:02d}" for index in range(sample_count + 2)]


def _chat_specs(chat_ids: list[str]) -> list[dict[str, Any]]:
    now = "2026-10-22T00:00:00Z"
    return [
        {
            "id": chat_id,
            "session_id": f"console:{chat_id}",
            "user_id": "default",
            "channel": "console",
            "name": f"Long benchmark {index:02d}",
            "created_at": now,
            "updated_at": now,
            "meta": {},
            "status": "idle",
            "pinned": False,
            "archived": False,
        }
        for index, chat_id in enumerate(chat_ids)
    ]


def _message(chat_id: str, turn_index: int, ordinal: int) -> dict[str, Any]:
    role = "user" if ordinal == 0 else "assistant"
    marker = (
        f"tail-marker-{chat_id}"
        if turn_index == 1_999 and ordinal == MESSAGES_PER_TURN - 1
        else ""
    )
    prefix = f"{chat_id} turn {turn_index} message {ordinal} {marker} "
    text = prefix + "x" * max(0, TEXT_BYTES - len(prefix))
    message_id = f"{chat_id}-message-{turn_index}-{ordinal}"
    timestamp = turn_index * 10 + ordinal
    return {
        "message_id": message_id,
        "turn_id": f"{chat_id}-turn-{turn_index}",
        "role": role,
        "ordinal": ordinal,
        "state": "completed",
        "metadata": {
            "type": "message",
            "timestamp": "2026-01-01T00:00:00Z",
        },
        "metadata_complete": True,
        "created_at_us": timestamp,
        "updated_at_us": timestamp,
        "blocks": [
            {
                "block_id": f"{message_id}-block",
                "message_id": message_id,
                "kind": "text",
                "ordinal": 0,
                "state": "completed",
                "total_bytes": len(text),
                "content_sha256": None,
                "entity_version": 1,
                "metadata": {},
                "metadata_complete": True,
                "created_at_us": timestamp,
                "updated_at_us": timestamp,
                "content": {"type": "text", "text": text},
                "content_loaded": True,
                "content_cursor": None,
            },
        ],
        "child_cursor": None,
        "children_complete": True,
    }


def _turn(chat_id: str, turn_index: int) -> dict[str, Any]:
    timestamp = turn_index * 10
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
        "updated_at_us": timestamp + 9,
        "terminal_at_us": timestamp + 9,
        "messages": [
            _message(chat_id, turn_index, ordinal)
            for ordinal in range(MESSAGES_PER_TURN)
        ],
        "child_cursor": None,
        "children_complete": True,
    }


def _turn_page(chat_id: str, limit: int) -> dict[str, Any]:
    page_turns = min(TAIL_TURNS, limit)
    return {
        "items": [
            _turn(chat_id, index)
            for index in range(LOGICAL_TURNS - page_turns, LOGICAL_TURNS)
        ],
        "older_cursor": f"older-{chat_id}",
        "newer_cursor": None,
        "view": {
            "conversation_id": chat_id,
            "head_turn_id": f"{chat_id}-turn-1999",
            "view_version": f"view-{chat_id}",
        },
        "bytes": page_turns * MESSAGES_PER_TURN * TEXT_BYTES,
        "truncated_entity": False,
    }


def _thinking_view() -> dict[str, Any]:
    return {
        "decision": {
            "enabled": False,
            "configured": False,
            "override": None,
        },
        "model_source": "global",
        "provider_id": None,
        "model": None,
        "control": {
            "kind": "unknown",
            "efforts": [],
            "supports_off": False,
        },
        "value": {"level": "inherit"},
        "effective": {"level": "inherit"},
        "source": "model",
        "reason": None,
    }


class FrozenChatApi:
    """Serve deterministic V2 tail pages without touching user data."""

    def __init__(self, chat_ids: list[str]) -> None:
        self.chat_ids = chat_ids
        self.specs = _chat_specs(chat_ids)

    def _chat_id(self, path: str) -> str | None:
        return next(
            (
                chat_id
                for chat_id in self.chat_ids
                if f"/chats/{chat_id}" in path
            ),
            None,
        )

    def route(self, route: Route) -> None:
        parsed = urlparse(route.request.url)
        path = parsed.path
        body: Any = None
        if path == "/api/chats":
            body = self.specs
        elif path == "/api/chats/groups":
            body = [
                {
                    "id": "default",
                    "name": "Uncategorized",
                    "order": 0,
                    "kind": "default",
                    "source": None,
                    "pinned": False,
                },
            ]
        elif path == "/api/chats/resolve":
            chat_id = parse_qs(parsed.query).get(
                "chat_id",
                [self.chat_ids[0]],
            )[0]
            spec = next(item for item in self.specs if item["id"] == chat_id)
            body = {"agent_id": "default", "chat": spec}
        elif path == "/api/chats/thinking-default":
            body = _thinking_view()
        else:
            chat_id = self._chat_id(path)
            if chat_id:
                body = self._chat_body(chat_id, path, parsed.query)
        if body is None:
            route.fallback()
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(body),
        )

    def _chat_body(self, chat_id: str, path: str, query: str) -> Any:
        if path.endswith("/view"):
            return {
                "conversation_id": chat_id,
                "display_name": chat_id,
                "lifecycle": "active",
                "migration_state": "ready_v2",
                "catalog_generation": 1,
                "head_turn_id": f"{chat_id}-turn-1999",
                "view_version": f"view-{chat_id}",
                "context_revision_id": f"context-{chat_id}",
                "active_run": None,
                "capabilities": {
                    "turn_pagination": True,
                    "message_pagination": True,
                    "block_pagination": True,
                    "block_fragments": True,
                    "live_resync": True,
                    "chat_copy": True,
                    "edit_last_user_message": False,
                },
            }
        if path.endswith("/turns"):
            limit = int(
                parse_qs(query).get(
                    "limit",
                    [TAIL_TURNS],
                )[0],
            )
            return _turn_page(chat_id, limit)
        if path.endswith("/status"):
            return {"status": "idle"}
        if path.endswith("/thinking"):
            return _thinking_view()
        if path.endswith("/project-dirs"):
            return {
                "project_dirs": [
                    {
                        "path": "/tmp/qwenpaw-benchmark",
                        "label": "Benchmark",
                        "exists": True,
                        "nested_with": None,
                        "is_workspace": True,
                    },
                ],
                "source": "session",
                "agent_project_dir": "/tmp/qwenpaw-benchmark",
            }
        if path.rstrip("/").endswith(chat_id):
            return {"messages": [], "status": "idle"}
        return None


def _register_routes(page: Page, api: FrozenChatApi) -> None:
    register_all(page)

    def guard_source_modules(route: Route) -> None:
        if urlparse(route.request.url).path.startswith("/api/"):
            route.fallback()
        else:
            route.continue_()

    page.route("**/*", guard_source_modules)
    page.route(
        "**/api/auth/status",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"enabled":false,"has_users":false}',
        ),
    )
    page.route(
        "**/api/tool-calls/**",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"items":[],"total":0}',
        ),
    )
    page.route(
        "**/api/workspace/project-directory/dirs",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "project_dirs": [],
                    "source": "workspace_fallback",
                    "workspace_dir": "/tmp/qwenpaw-benchmark",
                    "workspace_exists": True,
                },
            ),
        ),
    )
    page.route("**/api/chats**", api.route)


def _install_observers(context: BrowserContext) -> None:
    context.add_init_script(
        """
        window.__QWENPAW_CHAT_BENCHMARK__ = { commits: [] };
        window.__qwenpawLongTasks = [];
        new PerformanceObserver((list) => {
          for (const entry of list.getEntries()) {
            window.__qwenpawLongTasks.push({
              startTime: entry.startTime,
              duration: entry.duration,
            });
          }
        }).observe({ type: "longtask", buffered: true });
        """,
    )


def _new_page(
    browser: Browser,
    api: FrozenChatApi,
) -> tuple[BrowserContext, Page, dict[str, list[str]]]:
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    _install_observers(context)
    page = context.new_page()
    _register_routes(page, api)
    faults: dict[str, list[str]] = {
        "http": [],
        "page": [],
        "console": [],
    }
    page.on(
        "response",
        lambda response: faults["http"].append(
            f"{response.status} {response.url}",
        )
        if response.status >= 400
        else None,
    )
    page.on(
        "pageerror",
        lambda error: faults["page"].append(
            getattr(error, "stack", None) or str(error),
        ),
    )
    page.on(
        "console",
        lambda message: faults["console"].append(message.text)
        if message.type == "error" and not message.text.startswith("Warning:")
        else None,
    )
    return context, page, faults


def _wait_ready(page: Page, chat_id: str) -> None:
    ready = page.wait_for_function(
        """marker => {
          const textarea = document.querySelector(
            'textarea:not([disabled])',
          );
          return Boolean(
            document.body.textContent?.includes(marker) &&
            textarea &&
            textarea.getClientRects().length,
          );
        }""",
        arg=f"tail-marker-{chat_id}",
        timeout=30_000,
    )
    ready.dispose()


def _navigate(page: Page, chat_id: str) -> dict[str, float]:
    started = page.evaluate("performance.now()")
    page.evaluate(
        """chatId => {
          history.pushState({}, "", `/chat/${chatId}`);
          window.dispatchEvent(new PopStateEvent("popstate"));
        }""",
        chat_id,
    )
    _wait_ready(page, chat_id)
    tail_finished = float(page.evaluate("performance.now()"))
    full_page = page.wait_for_function(
        """target =>
          document.querySelectorAll(target.selector).length >= target.expected
        """,
        arg={
            "selector": TIMELINE_ROOT_SELECTOR,
            "expected": INITIAL_PAGE_TURNS * 2,
        },
        timeout=30_000,
    )
    full_page.dispose()
    full_finished = float(page.evaluate("performance.now()"))
    return {
        "tail_duration_ms": tail_finished - float(started),
        "full_duration_ms": full_finished - float(started),
    }


def _reset_metrics(page: Page) -> None:
    page.evaluate(
        """() => {
          window.__QWENPAW_CHAT_BENCHMARK__.commits.length = 0;
          window.__qwenpawLongTasks.length = 0;
        }""",
    )


def _timeline_root_count(page: Page) -> int:
    """Count only top-level SDK message bubbles, excluding nested cards."""
    return page.locator(TIMELINE_ROOT_SELECTOR).count()


def _sample_metrics(
    page: Page,
    timings: dict[str, float],
) -> dict[str, Any]:
    browser_metrics = page.evaluate(
        """() => ({
          commits: [...window.__QWENPAW_CHAT_BENCHMARK__.commits],
          longTasks: [...window.__qwenpawLongTasks],
          domElements: document.querySelectorAll("*").length,
        })""",
    )
    commits = browser_metrics["commits"]
    long_tasks = browser_metrics["longTasks"]
    return {
        "duration_ms": timings["tail_duration_ms"],
        **timings,
        "react_commits": commits,
        "react_commit_count": len(commits),
        "react_commit_max_ms": max(
            (item["actualDuration"] for item in commits),
            default=0.0,
        ),
        "long_tasks": long_tasks,
        "long_task_max_ms": max(
            (item["duration"] for item in long_tasks),
            default=0.0,
        ),
        "timeline_roots": _timeline_root_count(page),
        "dom_elements": browser_metrics["domElements"],
    }


def _assert_fault_free(faults: dict[str, list[str]], label: str) -> None:
    if any(faults.values()):
        raise AssertionError(f"{label} browser faults: {faults}")


def _capture_trace(
    browser: Browser,
    api: FrozenChatApi,
    *,
    base_url: str,
    trace_path: Path,
    warm: bool,
) -> None:
    context, page, faults = _new_page(browser, api)
    context.tracing.start(screenshots=False, snapshots=True)
    if warm:
        first, second = api.chat_ids[-2:]
        page.goto(
            f"{base_url}/chat/{first}",
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        _wait_ready(page, first)
        _navigate(page, second)
        _navigate(page, first)
    else:
        page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
        empty_ready = page.wait_for_function(
            """() => Boolean(
              document.querySelector('textarea:not([disabled])'),
            )""",
            timeout=30_000,
        )
        empty_ready.dispose()
        _navigate(page, api.chat_ids[0])
    _assert_fault_free(faults, "representative trace")
    context.tracing.stop(path=trace_path)
    context.close()


def _cold_samples(
    browser: Browser,
    api: FrozenChatApi,
    *,
    base_url: str,
    sample_count: int,
) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for index in range(sample_count):
        context, page, faults = _new_page(browser, api)
        page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
        page.locator("textarea:not([disabled])").first.wait_for(
            state="visible",
            timeout=30_000,
        )
        _reset_metrics(page)
        timings = _navigate(page, api.chat_ids[index])
        samples.append(_sample_metrics(page, timings))
        _assert_fault_free(faults, f"cold sample {index}")
        context.close()
        print(
            json.dumps(
                {
                    "phase": "cold",
                    "sample": index + 1,
                    **timings,
                },
            ),
            flush=True,
        )
    return samples


def _warm_samples(
    browser: Browser,
    api: FrozenChatApi,
    *,
    base_url: str,
    sample_count: int,
) -> list[dict[str, Any]]:
    context, page, faults = _new_page(browser, api)
    first, second = api.chat_ids[-2:]
    page.goto(
        f"{base_url}/chat/{first}",
        wait_until="domcontentloaded",
        timeout=30_000,
    )
    _wait_ready(page, first)
    _navigate(page, second)
    samples: list[dict[str, Any]] = []
    for index in range(sample_count):
        target = first if index % 2 == 0 else second
        _reset_metrics(page)
        timings = _navigate(page, target)
        samples.append(_sample_metrics(page, timings))
        print(
            json.dumps(
                {
                    "phase": "warm",
                    "sample": index + 1,
                    **timings,
                },
            ),
            flush=True,
        )
    _assert_fault_free(faults, "warm samples")
    context.close()
    return samples


def _aggregate(samples: list[dict[str, Any]]) -> dict[str, Any]:
    commit_durations = [
        commit["actualDuration"]
        for sample in samples
        for commit in sample["react_commits"]
    ]
    long_tasks = [
        task["duration"] for sample in samples for task in sample["long_tasks"]
    ]
    return {
        "navigation": _summary(
            [sample["duration_ms"] for sample in samples],
        ),
        "full_page": _summary(
            [sample["full_duration_ms"] for sample in samples],
        ),
        "react_commits": (
            _summary(commit_durations) if commit_durations else {"count": 0}
        ),
        "long_tasks": (_summary(long_tasks) if long_tasks else {"count": 0}),
        "max_timeline_roots": max(
            sample["timeline_roots"] for sample in samples
        ),
        "max_dom_elements": max(sample["dom_elements"] for sample in samples),
    }


def main() -> int:
    """Run cold and warm samples and save raw browser evidence."""
    args = _parse_args()
    if args.samples < 1:
        raise ValueError("samples must be positive")
    if not args.chrome.is_file():
        raise FileNotFoundError(args.chrome)
    args.trace_directory.mkdir(parents=True, exist_ok=True)
    chat_ids = _chat_ids(args.samples)
    api = FrozenChatApi(chat_ids)
    cold_trace = args.trace_directory / "v2-browser-cold.zip"
    warm_trace = args.trace_directory / "v2-browser-warm.zip"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path=str(args.chrome),
        )
        _capture_trace(
            browser,
            api,
            base_url=args.base_url,
            trace_path=cold_trace,
            warm=False,
        )
        _capture_trace(
            browser,
            api,
            base_url=args.base_url,
            trace_path=warm_trace,
            warm=True,
        )
        cold = _cold_samples(
            browser,
            api,
            base_url=args.base_url,
            sample_count=args.samples,
        )
        warm = _warm_samples(
            browser,
            api,
            base_url=args.base_url,
            sample_count=args.samples,
        )
        browser_version = browser.version
        browser.close()
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
        "metric_contract": {
            "timeline_roots": {
                "selector": TIMELINE_ROOT_SELECTOR,
                "semantics": "direct BubbleList message children",
            },
        },
        "viewport": {"width": 1440, "height": 1000},
        "cpu_throttling": "none",
        "network_throttling": "none; frozen in-process route mocks",
        "dataset": {
            "logical_turns": LOGICAL_TURNS,
            "loaded_tail_turns": TAIL_TURNS,
            "initial_page_turns": INITIAL_PAGE_TURNS,
            "messages_per_turn": MESSAGES_PER_TURN,
            "text_bytes_per_message": TEXT_BYTES,
        },
        "timing_boundary": (
            "SPA navigation after shell preload until tail marker and an "
            "enabled composer textarea are visible"
        ),
        "cold_definition": (
            "fresh browser context and empty Chat data cache after shell "
            "preload"
        ),
        "warm_definition": (
            "both target V2 Chats opened before timed switching"
        ),
        "traces": {
            "cold": str(cold_trace),
            "warm": str(warm_trace),
        },
        "cold": {"samples": cold, "summary": _aggregate(cold)},
        "warm": {"samples": warm, "summary": _aggregate(warm)},
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
                "cold": payload["cold"]["summary"],
                "warm": payload["warm"]["summary"],
            },
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
