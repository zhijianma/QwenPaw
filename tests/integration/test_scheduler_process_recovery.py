# -*- coding: utf-8 -*-
"""Process-boundary recovery tests for durable Scheduler retries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PROCESS_HELPER = Path(__file__).with_name(
    "_scheduler_retry_process.py",
)


def _subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    source_path = str(_PROJECT_ROOT / "src")
    inherited = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        f"{source_path}{os.pathsep}{inherited}" if inherited else source_path
    )
    return environment


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


def test_retry_cursor_recovers_after_forced_process_termination(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "scheduler.db"
    ready_path = tmp_path / "retry-committed.json"
    command = [
        sys.executable,
        str(_PROCESS_HELPER),
        "defer",
        str(database_path),
        "--ready-path",
        str(ready_path),
    ]
    process = subprocess.Popen(  # pylint: disable=consider-using-with
        command,
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
        assert committed == {
            "retry_not_before": "2026-10-09T00:00:30+00:00",
            "scheduled_for": "2026-10-09T00:00:00+00:00",
        }
        process.kill()
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)

    recovered = subprocess.run(
        [
            sys.executable,
            str(_PROCESS_HELPER),
            "recover",
            str(database_path),
        ],
        cwd=_PROJECT_ROOT,
        env=_subprocess_environment(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=True,
    )
    evidence = json.loads(recovered.stdout)
    assert evidence == {
        "disposition": "dispatched",
        "handled": ["2026-10-09T00:00:00+00:00"],
        "last_fire_at": "2026-10-09T00:00:00+00:00",
        "retry_count": 0,
        "retry_not_before": None,
        "scheduled_for": "2026-10-09T00:00:00+00:00",
    }
