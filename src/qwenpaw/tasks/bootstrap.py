# -*- coding: utf-8 -*-
"""Workspace-scoped construction for the Lite durable task service."""

from pathlib import Path
from typing import Any

from .ledger import SQLiteExecutionLedger
from .service import TaskService


def task_service_for_workspace(
    workspace: Any,
    *,
    registry_generation: int = 1,
) -> TaskService:
    """Reuse one service while allowing future runs to see new generations."""
    service = getattr(workspace, "_lite_task_service", None)
    if isinstance(service, TaskService):
        service.set_registry_generation(registry_generation)
        return service
    database_path = (
        Path(workspace.workspace_dir) / ".qwenpaw" / "lite" / "tasks.db"
    )
    service = TaskService(
        store=SQLiteExecutionLedger(database_path),
        registry_generation=registry_generation,
    )
    setattr(workspace, "_lite_task_service", service)
    return service
