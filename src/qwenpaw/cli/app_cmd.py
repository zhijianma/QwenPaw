# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import logging
import os
import socket

import click
import uvicorn

from ..app.auth import is_auth_enabled
from ..browser.control_link.chrome.protocol import NM_MAX_INBOUND_BYTES
from ..config.utils import write_last_api
from ..constant import LOG_LEVEL_ENV
from ..editions.resolver import EDITION_ENV, resolve_edition
from ..utils.http import is_loopback_host, probe_host_for_bind_host
from ..utils.logging import SuppressPathAccessLogFilter, setup_logger
from ..utils.platform import warn_unelevated_sandbox

logger = logging.getLogger(__name__)

MANAGED_HOST_LAUNCH_PREFIX = "QWENPAW_MANAGED_HOST"


def _format_bind_address(host: str, port: int) -> str:
    """Return a readable bind address for startup logs."""
    normalized_host = host.strip()
    if ":" in normalized_host and not normalized_host.startswith("["):
        normalized_host = f"[{normalized_host}]"
    return f"{normalized_host}:{port}"


def _warn_if_auth_off_non_loopback_bind(host: str, port: int) -> None:
    """Warn when QwenPaw is reachable beyond loopback without auth."""
    if is_auth_enabled() or is_loopback_host(host):
        return

    bind_address = _format_bind_address(host, port)
    warning = f"""
============================================================
SECURITY NOTICE: QwenPaw is bound to {bind_address} without authentication.

Anyone who can reach this address may access QwenPaw APIs without login.

Recommended:
  - Restrict access to a trusted network interface or protected environment.
  - Enable authentication with QWENPAW_AUTH_ENABLED=true if untrusted users or
    processes may reach this address.
============================================================
""".strip()
    if logger.isEnabledFor(logging.WARNING):
        logger.warning("\n%s", warning)
    else:
        click.echo(warning, err=True)


def configure_server_process(
    host: str,
    port: int,
    log_level: str,
    hide_access_paths: tuple[str, ...],
    *,
    reload: bool = False,
    record_last_api: bool = True,
) -> None:
    """Configure shared process state for an HTTP server command."""
    if record_last_api:
        write_last_api(probe_host_for_bind_host(host), port)
    os.environ[LOG_LEVEL_ENV] = log_level
    if reload:
        os.environ["QWENPAW_RELOAD_MODE"] = "1"

    setup_logger(log_level)
    if log_level in ("debug", "trace"):
        from .main import log_init_timings

        log_init_timings()

    paths = [path for path in hide_access_paths if path]
    if paths:
        logging.getLogger("uvicorn.access").addFilter(
            SuppressPathAccessLogFilter(paths),
        )
    warn_unelevated_sandbox()


def _socket_port(sock: socket.socket) -> int:
    """Return the TCP port owned by a bound server socket."""
    address = sock.getsockname()
    if not isinstance(address, tuple) or len(address) < 2:
        raise RuntimeError(f"unexpected managed Host address: {address!r}")
    return int(address[1])


def _managed_api_url(host: str, port: int) -> str:
    """Return the loopback API root published to a managed SDK."""
    display_host = host.strip()
    if ":" in display_host and not display_host.startswith("["):
        display_host = f"[{display_host}]"
    return f"http://{display_host}:{port}/api"


def _run_managed_server(
    host: str,
    port: int,
    log_level: str,
    hide_access_paths: tuple[str, ...],
) -> None:
    """Run one SDK-owned Host and publish its pre-bound API address."""
    config = uvicorn.Config(
        "qwenpaw.app._app:app",
        host=host,
        port=port,
        reload=False,
        workers=1,
        log_level=log_level,
        timeout_graceful_shutdown=5,
        ws_max_size=NM_MAX_INBOUND_BYTES,
    )
    managed_socket = config.bind_socket()
    try:
        actual_port = _socket_port(managed_socket)
        configure_server_process(
            host,
            actual_port,
            log_level,
            hide_access_paths,
            record_last_api=False,
        )
        launch = {
            "schema": "qwenpaw.managed-host-launch.v1",
            "api_url": _managed_api_url(host, actual_port),
            "pid": os.getpid(),
        }
        click.echo(
            f"{MANAGED_HOST_LAUNCH_PREFIX} "
            f"{json.dumps(launch, separators=(',', ':'))}",
        )
        uvicorn.Server(config).run(sockets=[managed_socket])
    finally:
        managed_socket.close()


@click.command("app")
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Bind host",
)
@click.option(
    "--port",
    default=8088,
    type=click.IntRange(0, 65535),
    show_default=True,
    help="Bind port",
)
@click.option("--reload", is_flag=True, help="Enable auto-reload (dev only)")
@click.option(
    "--managed",
    is_flag=True,
    help="Run an SDK-owned loopback Host and emit its launch record.",
)
@click.option(
    "--log-level",
    default="info",
    type=click.Choice(
        ["critical", "error", "warning", "info", "debug", "trace"],
        case_sensitive=False,
    ),
    show_default=True,
    help="Log level",
)
@click.option(
    "--hide-access-paths",
    multiple=True,
    default=("/console/push-messages", "/console/inbox/events"),
    show_default=True,
    help="Path substrings to hide from uvicorn access log (repeatable).",
)
@click.option(
    "--workers",
    type=int,
    default=None,
    help="[DEPRECATED] Number of worker processes. "
    "This option is deprecated and will be removed in a future version. "
    "QwenPaw always uses 1 worker.",
)
@click.option(
    "--edition",
    type=click.Choice(["lite", "workstation", "hub"]),
    default=None,
    help="Product profile; defaults to QWENPAW_EDITION or lite.",
)
def app_cmd(
    host: str,
    port: int,
    reload: bool,
    managed: bool,
    workers: int,  # pylint: disable=unused-argument
    log_level: str,
    hide_access_paths: tuple[str, ...],
    edition: str | None,
) -> None:
    """Run QwenPaw FastAPI app."""
    # NOTE: the server intentionally runs UNPRIVILEGED. The Windows
    # restricted-token sandbox no longer requires the whole server to be
    # elevated (which PR #5931 forced via ShellExecuteW("runas"), breaking
    # headless / VBS launchers with a surprise UAC prompt and a detached,
    # un-closable window). If sandbox is enabled but the process is not
    # admin, warn_unelevated_sandbox() below will log a warning about
    # reduced isolation before the server starts.

    if workers is not None:
        click.echo(
            "⚠️  WARNING: --workers option is deprecated and will be removed "
            "in a future version.",
            err=True,
        )
        click.echo(
            "   QwenPaw always uses 1 worker for stability. "
            "Your specified value will be ignored.",
            err=True,
        )
        click.echo(err=True)

    if managed and reload:
        raise click.UsageError("--managed cannot be combined with --reload")
    if managed and not is_loopback_host(host):
        raise click.UsageError("--managed requires a loopback --host")

    profile = resolve_edition(edition)
    os.environ[EDITION_ENV] = profile.edition

    if managed:
        _run_managed_server(
            host,
            port,
            log_level,
            hide_access_paths,
        )
        return

    configure_server_process(
        host,
        port,
        log_level,
        hide_access_paths,
        reload=reload,
    )
    _warn_if_auth_off_non_loopback_bind(host, port)

    uvicorn.run(
        "qwenpaw.app._app:app",
        host=host,
        port=port,
        reload=reload,
        workers=1,
        log_level=log_level,
        # Bound shutdown so workspace SSE connections cannot block exit.
        timeout_graceful_shutdown=5,
        # Chrome Native Messaging inbound limit; this server-wide value is a
        # protocol fact rather than a user-configurable WebSocket capacity.
        ws_max_size=NM_MAX_INBOUND_BYTES,
    )
