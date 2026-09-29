# -*- coding: utf-8 -*-
"""Built-in Workspace prompt assembly published as an OS capability."""

from __future__ import annotations

from collections.abc import Sequence

from ..kernel.invocation import DEFAULT_PROMPT_PROVIDER_ID, InvocationScope
from ..kernel.models import (
    CapabilityBundle,
    CapabilityContribution,
    CapabilityProviderKind,
    PromptFragment,
    RestartPolicy,
)
from ..kernel.ports import PromptHost

SYSTEM_PROMPT_PROVIDER_ID = "qwenpaw.system.prompts"


class WorkspacePromptProvider:
    """Adapt the existing PromptManager to typed prompt fragments."""

    provider_id = DEFAULT_PROMPT_PROVIDER_ID

    async def health_check(self) -> bool:
        """Report that the stateless compatibility provider is available."""
        return True

    async def list_fragments(
        self,
        scope: InvocationScope,
        host: PromptHost,
    ) -> Sequence[PromptFragment]:
        """Return the existing Workspace prompt as one migration fragment."""
        del scope
        content = (await host.build_workspace_prompt()).strip()
        if not content:
            return ()
        return (
            PromptFragment(
                fragment_id=f"{self.provider_id}.workspace",
                content=content,
                priority=0,
            ),
        )


SYSTEM_PROMPT_CAPABILITY_BUNDLE = CapabilityBundle(
    provider_id=SYSTEM_PROMPT_PROVIDER_ID,
    provider_kind=CapabilityProviderKind.SYSTEM,
    version="1.0.0",
    restart_policy=RestartPolicy.HOT,
    contributions=(
        CapabilityContribution(
            contribution_id="workspace-prompt",
            slot="prompt.provider",
            entrypoint=(
                "qwenpaw.capabilities.system_prompts:WorkspacePromptProvider"
            ),
        ),
    ),
)


def system_prompt_contribution_factory(
    declaration: CapabilityContribution,
) -> object:
    """Construct one built-in prompt capability implementation."""
    if declaration.contribution_id == "workspace-prompt":
        return WorkspacePromptProvider()
    raise LookupError(declaration.contribution_id)


__all__ = [
    "SYSTEM_PROMPT_CAPABILITY_BUNDLE",
    "SYSTEM_PROMPT_PROVIDER_ID",
    "WorkspacePromptProvider",
    "system_prompt_contribution_factory",
]
