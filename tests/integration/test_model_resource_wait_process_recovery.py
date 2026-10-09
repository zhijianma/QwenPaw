# -*- coding: utf-8 -*-
"""Process-boundary recovery for rate-limited model resources."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PROCESS_HELPER = Path(__file__).with_name(
    "_model_resource_wait_process.py",
)


def _subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    source_path = str(_PROJECT_ROOT / "src")
    inherited = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        f"{source_path}{os.pathsep}{inherited}" if inherited else source_path
    )
    return environment


def _run_helper(
    mode: str,
    database_path: Path,
    *,
    wait_id: str,
) -> dict[str, object]:
    completed = subprocess.run(
        [
            sys.executable,
            str(_PROCESS_HELPER),
            mode,
            str(database_path),
            "--wait-id",
            wait_id,
        ],
        cwd=_PROJECT_ROOT,
        env=_subprocess_environment(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=True,
    )
    return json.loads(completed.stdout)


def _wait_for_ready(
    process: subprocess.Popen[str],
    ready_path: Path,
) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if ready_path.exists():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(
                f"defer process exited early: {stdout}\n{stderr}",
            )
        time.sleep(0.02)
    pytest.fail("defer process did not publish its durable ready marker")


def test_rate_limit_wait_dispatches_after_forced_process_termination(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "model-recovery.sqlite3"
    ready_path = tmp_path / "resource-wait-committed.json"
    process = subprocess.Popen(  # pylint: disable=consider-using-with
        [
            sys.executable,
            str(_PROCESS_HELPER),
            "defer",
            str(database_path),
            "--ready-path",
            str(ready_path),
        ],
        cwd=_PROJECT_ROOT,
        env=_subprocess_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        _wait_for_ready(process, ready_path)
        committed = json.loads(ready_path.read_text(encoding="utf-8"))
        process.kill()
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)

    inspected = _run_helper(
        "inspect",
        database_path,
        wait_id=committed["wait_id"],
    )
    assert inspected["status"] == "waiting"
    assert inspected["submission_id"] is None
    remaining = inspected["remaining_seconds"]
    assert isinstance(remaining, (int, float)) and remaining > 0

    not_before = datetime.fromisoformat(committed["not_before"])
    delay = max(
        (not_before - datetime.now(timezone.utc)).total_seconds(),
        0,
    )
    time.sleep(delay + 0.05)

    recovered = _run_helper(
        "recover",
        database_path,
        wait_id=committed["wait_id"],
    )
    assert recovered == {
        "correlation_id": "10000000-0000-4000-8000-000000000003",
        "conversation_id": "chat-resource-recovery",
        "status": "dispatched",
        "submission_id": "10000000-0000-4000-8000-000000000006",
        "wait_id": committed["wait_id"],
    }
