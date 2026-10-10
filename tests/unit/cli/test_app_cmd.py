# -*- coding: utf-8 -*-
"""Focused tests for the managed QwenPaw Host CLI contract."""

from __future__ import annotations

import json
import os
import socket
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from qwenpaw.cli.app_cmd import (
    MANAGED_HOST_LAUNCH_PREFIX,
    app_cmd,
)


@pytest.mark.parametrize("host", ["0.0.0.0", "::"])
def test_managed_host_requires_loopback(host: str) -> None:
    result = CliRunner().invoke(app_cmd, ["--managed", "--host", host])

    assert result.exit_code != 0
    assert "requires a loopback" in result.output


def test_managed_host_rejects_reload() -> None:
    result = CliRunner().invoke(app_cmd, ["--managed", "--reload"])

    assert result.exit_code != 0
    assert "cannot be combined" in result.output


def test_managed_host_emits_bound_launch_record() -> None:
    bound_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    bound_socket.bind(("127.0.0.1", 0))
    bound_socket.listen(1)
    config = MagicMock()
    config.bind_socket.return_value = bound_socket
    server = MagicMock()

    with (
        patch("qwenpaw.cli.app_cmd.uvicorn.Config", return_value=config),
        patch("qwenpaw.cli.app_cmd.uvicorn.Server", return_value=server),
        patch(
            "qwenpaw.cli.app_cmd.configure_server_process",
        ) as configure,
    ):
        result = CliRunner().invoke(
            app_cmd,
            ["--managed", "--port", "0", "--log-level", "warning"],
        )

    assert result.exit_code == 0
    line = next(
        item
        for item in result.output.splitlines()
        if item.startswith(f"{MANAGED_HOST_LAUNCH_PREFIX} ")
    )
    launch = json.loads(line.split(" ", 1)[1])
    port = int(launch["api_url"].rsplit(":", 1)[1].removesuffix("/api"))
    assert launch == {
        "schema": "qwenpaw.managed-host-launch.v1",
        "api_url": f"http://127.0.0.1:{port}/api",
        "pid": os.getpid(),
    }
    assert port > 0
    configure.assert_called_once_with(
        "127.0.0.1",
        port,
        "warning",
        ("/console/push-messages", "/console/inbox/events"),
        record_last_api=False,
    )
    server.run.assert_called_once_with(sockets=[bound_socket])
    assert bound_socket.fileno() == -1


def test_regular_host_still_uses_uvicorn_run() -> None:
    with (
        patch("qwenpaw.cli.app_cmd.configure_server_process") as configure,
        patch("qwenpaw.cli.app_cmd.uvicorn.run") as run,
    ):
        result = CliRunner().invoke(
            app_cmd,
            ["--host", "127.0.0.1", "--port", "8089"],
        )

    assert result.exit_code == 0
    configure.assert_called_once()
    run.assert_called_once()
