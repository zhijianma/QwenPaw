# -*- coding: utf-8 -*-
"""Runtime adapters for provider-neutral prompt capability contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..utils.io_utils import run_sync_io


@dataclass(frozen=True)
class WorkspacePromptHost:
    """Bind the legacy PromptManager to one invocation build."""

    builder: Any
    context: Any
    agent_config: Any

    async def build_workspace_prompt(self) -> str:
        """Build the current Workspace prompt outside the event loop."""
        return await run_sync_io(
            self.builder.build_prompt,
            self.context,
            self.agent_config,
        )


__all__ = ["WorkspacePromptHost"]
