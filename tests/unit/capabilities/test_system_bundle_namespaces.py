# -*- coding: utf-8 -*-
"""Guard system capability bundles against provider ownership collisions."""

from qwenpaw.capabilities.system_chat import SYSTEM_CHAT_CAPABILITY_BUNDLE
from qwenpaw.capabilities.system_commands import (
    SYSTEM_COMMAND_CAPABILITY_BUNDLE,
)
from qwenpaw.capabilities.system_drivers import (
    SYSTEM_DRIVER_CAPABILITY_BUNDLE,
)
from qwenpaw.capabilities.system_hooks import SYSTEM_HOOK_CAPABILITY_BUNDLE
from qwenpaw.capabilities.system_memory import SYSTEM_MEMORY_CAPABILITY_BUNDLE
from qwenpaw.capabilities.system_modes import SYSTEM_MODE_CAPABILITY_BUNDLE
from qwenpaw.capabilities.system_prompts import (
    SYSTEM_PROMPT_CAPABILITY_BUNDLE,
)
from qwenpaw.capabilities.system_stop_gates import (
    SYSTEM_STOP_GATE_CAPABILITY_BUNDLE,
)
from qwenpaw.capabilities.system_tools import SYSTEM_TOOL_CAPABILITY_BUNDLE
from qwenpaw.tasks.system_contributions import SYSTEM_CAPABILITY_BUNDLE


SYSTEM_BUNDLES = (
    SYSTEM_CHAT_CAPABILITY_BUNDLE,
    SYSTEM_COMMAND_CAPABILITY_BUNDLE,
    SYSTEM_DRIVER_CAPABILITY_BUNDLE,
    SYSTEM_HOOK_CAPABILITY_BUNDLE,
    SYSTEM_MEMORY_CAPABILITY_BUNDLE,
    SYSTEM_MODE_CAPABILITY_BUNDLE,
    SYSTEM_PROMPT_CAPABILITY_BUNDLE,
    SYSTEM_STOP_GATE_CAPABILITY_BUNDLE,
    SYSTEM_TOOL_CAPABILITY_BUNDLE,
    SYSTEM_CAPABILITY_BUNDLE,
)


def test_system_bundles_have_unique_provider_namespaces() -> None:
    provider_ids = [bundle.provider_id for bundle in SYSTEM_BUNDLES]

    assert len(provider_ids) == len(set(provider_ids))


def test_system_capability_ids_are_unique_across_bundles() -> None:
    capability_ids = [
        f"{bundle.provider_id}.{contribution.contribution_id}"
        for bundle in SYSTEM_BUNDLES
        for contribution in bundle.contributions
    ]

    assert len(capability_ids) == len(set(capability_ids))
