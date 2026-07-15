# -*- coding: utf-8 -*-
"""get_ctx — FastAPI dependency that creates PawAppContext per request.

Usage in Router mode:
    from qwenpaw.pawapp import get_ctx
    from fastapi import Depends

    @router.get("/projects")
    async def list_projects(ctx=Depends(get_ctx)):
        return await ctx.storage.get("projects", default=[])

The dependency extracts ``app_id`` from the request path and constructs
a PawAppContext wired to the running services (workspace_registry,
app_services, plugin_registry, session).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import Request

from .context import PawAppContext

logger = logging.getLogger(__name__)

_APP_ID_RE = re.compile(r"/(?:api/)?pawapp(?:s)?/([^/]+)")


def _extract_app_id_from_request(request: Request) -> str:
    """Extract PawApp ID from the request path."""
    match = _APP_ID_RE.search(request.url.path)
    if match:
        return match.group(1)

    app_id = request.headers.get("X-PawApp-Id", "")
    if app_id:
        return app_id

    return "unknown"


def _get_session(request: Request) -> Any:
    """Get or create a SafeJSONSession for PawApp storage."""
    # pylint: disable=unused-argument
    try:
        from ..app.chats.session import SafeJSONSession
        from ..constant import WORKING_DIR

        return SafeJSONSession(save_dir=str(WORKING_DIR))
    except Exception:
        return None


async def get_ctx(request: Request) -> PawAppContext:
    """FastAPI dependency that provides PawAppContext.

    Injects all available services from ``request.app.state``.
    """
    app_id = _extract_app_id_from_request(request)

    # Read services from app state (set by lifespan)
    app_state = request.app.state
    workspace_registry = getattr(app_state, "multi_agent_manager", None)
    app_services = getattr(app_state, "app_services", None)
    plugin_registry = getattr(app_state, "plugin_registry", None)

    # Get or create session for storage
    session = _get_session(request)

    # Determine agent_id (from path, query param, or default)
    agent_id = request.query_params.get("agent_id", "default")

    return PawAppContext(
        app_id=app_id,
        agent_id=agent_id,
        _workspace_registry=workspace_registry,
        _app_services=app_services,
        _plugin_registry=plugin_registry,
        _session=session,
        _sse_channel=None,  # Set by TaskManager for long-running tasks
    )
