# -*- coding: utf-8 -*-
"""Minimal Prompt Provider using only the public plugin SDK."""

from pathlib import Path

from qwenpaw.plugins.sdk import (
    InvocationScope,
    PromptFragment,
    PromptHost,
)


class ProjectPromptProvider:
    """Contribute deterministic project guidance to one invocation."""

    provider_id = "runtime-provider-kit.project-prompt"

    async def health_check(self) -> bool:
        """Report that this stateless provider can be published."""
        return True

    async def list_fragments(
        self,
        scope: InvocationScope,
        host: PromptHost,
    ) -> tuple[PromptFragment, ...]:
        """Return one provider-owned fragment without private Host access."""
        del host
        project_name = Path(scope.workspace_dir).name or "workspace"
        return (
            PromptFragment(
                fragment_id=f"{self.provider_id}.project-guidance",
                content=(
                    f"Keep changes scoped to the {project_name} project."
                ),
                priority=50,
            ),
        )


def create_provider() -> ProjectPromptProvider:
    """Create the manifest contribution implementation."""
    return ProjectPromptProvider()
