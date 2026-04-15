# -*- coding: utf-8 -*-
"""Frontend plugin API.

Serves two endpoints consumed by the React frontend plugin loader:

  GET  /api/plugins
      Scans PLUGINS_DIR for subdirectories that contain a ``manifest.json``
      (frontend manifest format) and returns the parsed list.

  GET  /api/plugins/{name}/frontend
      Serves the compiled UMD bundle at
      ``PLUGINS_DIR/<name>/dist/index.umd.js``.

The ``manifest.json`` format (frontend-flavoured) is:

.. code-block:: json

    {
        "name": "weather-plugin",
        "version": "1.0.0",
        "description": "...",
        "author": "...",
        "entry": {
            "frontend": "dist/index.umd.js",
            "backend":  "plugin.py"
        }
    }

This is intentionally separate from the existing Python-side ``plugin.json``
so that purely-frontend plugins (no Python backend) are also supported.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ...constant import PLUGINS_DIR

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/plugins", tags=["plugins"])


# ── helpers ────────────────────────────────────────────────────────────────


def _read_frontend_manifest(plugin_dir: Path) -> Dict[str, Any] | None:
    """Try to read a frontend ``manifest.json`` from *plugin_dir*.

    Returns the parsed dict on success, ``None`` if the file is absent or
    malformed.
    """
    manifest_path = plugin_dir / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        return json.loads(manifest_path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read manifest %s: %s", manifest_path, exc)
        return None


# ── routes ─────────────────────────────────────────────────────────────────


@router.get("", summary="List installed frontend plugins")
async def list_plugins() -> List[Dict[str, Any]]:
    """Return manifest data for every plugin that has a ``manifest.json``.

    Plugins without a ``manifest.json`` (pure-Python plugins only) are
    silently skipped.
    """
    manifests: List[Dict[str, Any]] = []

    if not PLUGINS_DIR.exists():
        return manifests

    for entry in sorted(PLUGINS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        manifest = _read_frontend_manifest(entry)
        if manifest is not None:
            manifests.append(manifest)

    return manifests


@router.get("/{name}/frontend", summary="Serve plugin frontend bundle")
async def get_plugin_frontend(name: str) -> FileResponse:
    """Serve the compiled UMD JS bundle for *name*.

    The expected file is ``PLUGINS_DIR/<name>/dist/index.umd.js``.
    """
    # Basic path-traversal guard
    if ".." in name or "/" in name or "\\" in name:
        raise HTTPException(status_code=400, detail="Invalid plugin name")

    plugin_dir = PLUGINS_DIR / name
    if not plugin_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Plugin '{name}' not found")

    js_path = plugin_dir / "dist" / "index.umd.js"
    if not js_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"No frontend bundle found for plugin '{name}'",
        )

    return FileResponse(
        path=str(js_path),
        media_type="application/javascript",
        filename="index.umd.js",
    )
