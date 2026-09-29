# -*- coding: utf-8 -*-
"""Tests for sharing the OS capability catalog across workspaces."""

from qwenpaw.app.workspace_registry import WorkspaceRegistry
from qwenpaw.plugins.loader import PluginLoader


def test_workspace_registry_injects_one_shared_capability_catalog(
    tmp_path,
) -> None:
    registry = WorkspaceRegistry()

    first = registry._create_workspace(  # pylint: disable=protected-access
        "one",
        str(tmp_path / "one"),
    )
    second = registry._create_workspace(  # pylint: disable=protected-access
        "two",
        str(tmp_path / "two"),
    )
    loader = PluginLoader(
        [],
        capability_registry=registry.capability_registry,
    )

    assert first.capability_registry is registry.capability_registry
    assert second.capability_registry is registry.capability_registry
    assert loader.capability_registry is registry.capability_registry
