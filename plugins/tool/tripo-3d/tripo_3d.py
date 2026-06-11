# -*- coding: utf-8 -*-
"""Tripo 3D Generation Tool Plugin Entry Point."""

import importlib.util
import logging
import os

from qwenpaw.plugins.api import PluginApi

logger = logging.getLogger(__name__)

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_tool_module():
    """Load tripo_3d_tool.py from this plugin's directory."""
    tool_path = os.path.join(_PLUGIN_DIR, "tripo_3d_tool.py")
    spec = importlib.util.spec_from_file_location(
        "tripo_3d_tool",
        tool_path,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Tripo3DToolPlugin:
    """Tripo 3D Generation Tool Plugin.

    Registers text_to_3d_tripo and image_to_3d_tripo tools
    into the Agent's toolkit.
    """

    def register(self, api: PluginApi):
        """Register Tripo 3D tools.

        Args:
            api: PluginApi instance.
        """
        tool = _load_tool_module()

        api.register_tool(
            tool_name="text_to_3d_tripo",
            tool_func=tool.text_to_3d_tripo,
            description=(
                "Generate 3D models from text prompts "
                "using Tripo"
            ),
            icon="\U0001fa69",  # 🪩
        )

        api.register_tool(
            tool_name="image_to_3d_tripo",
            tool_func=tool.image_to_3d_tripo,
            description=(
                "Generate 3D models from a single image "
                "using Tripo"
            ),
            icon="\U0001f5bc\ufe0f",  # 🖼️
        )

        api.register_tool(
            tool_name="multi_images_to_3d_tripo",
            tool_func=tool.multi_images_to_3d_tripo,
            description=(
                "Generate 3D models from multiple images "
                "(2-4 angles) using Tripo"
            ),
            icon="\U0001f5bc\ufe0f",  # 🖼️
        )

        logger.info("Tripo 3D tool plugin registered")


# Export plugin instance
plugin = Tripo3DToolPlugin()
