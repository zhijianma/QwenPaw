# -*- coding: utf-8 -*-
"""Provider-neutral capability catalog infrastructure."""

from .authorization import (
    CapabilityPromotionAuthorizationRequired,
    authorize_capability_promotion,
    promotion_risk_for_slots,
    validate_promotion_authorization,
)
from .registry import (
    ActivatedContribution,
    ActivationError,
    GenerationLease,
    GenerationRegistry,
    ProviderDeactivationError,
    ReleaseRollbackError,
    RegistrySnapshot,
)
from .system_tools import (
    SYSTEM_TOOL_CAPABILITY_BUNDLE,
    WorkspaceToolProvider,
)
from .system_memory import (
    SYSTEM_MEMORY_CAPABILITY_BUNDLE,
    WorkspaceMemoryProvider,
)
from .system_modes import (
    SYSTEM_MODE_CAPABILITY_BUNDLE,
    WorkspaceAgentModeProvider,
)
from .system_prompts import (
    SYSTEM_PROMPT_CAPABILITY_BUNDLE,
    WorkspacePromptProvider,
)
from .system_drivers import (
    SYSTEM_DRIVER_CAPABILITY_BUNDLE,
    WorkspaceDriverProvider,
)
from .system_commands import (
    SYSTEM_COMMAND_CAPABILITY_BUNDLE,
    WorkspaceCommandProvider,
)
from .system_hooks import (
    SYSTEM_HOOK_CAPABILITY_BUNDLE,
    WorkspaceHookProvider,
)
from .system_stop_gates import (
    SYSTEM_STOP_GATE_CAPABILITY_BUNDLE,
    WorkspaceStopGateProvider,
)
from .conformance import (
    CAPABILITY_CONFORMANCE,
    CapabilityConformanceEntry,
    capability_conformance_snapshot,
)

__all__ = [
    "ActivatedContribution",
    "ActivationError",
    "CAPABILITY_CONFORMANCE",
    "CapabilityConformanceEntry",
    "CapabilityPromotionAuthorizationRequired",
    "GenerationLease",
    "GenerationRegistry",
    "ProviderDeactivationError",
    "ReleaseRollbackError",
    "RegistrySnapshot",
    "SYSTEM_MEMORY_CAPABILITY_BUNDLE",
    "SYSTEM_COMMAND_CAPABILITY_BUNDLE",
    "SYSTEM_DRIVER_CAPABILITY_BUNDLE",
    "SYSTEM_HOOK_CAPABILITY_BUNDLE",
    "SYSTEM_MODE_CAPABILITY_BUNDLE",
    "SYSTEM_PROMPT_CAPABILITY_BUNDLE",
    "SYSTEM_STOP_GATE_CAPABILITY_BUNDLE",
    "SYSTEM_TOOL_CAPABILITY_BUNDLE",
    "WorkspaceMemoryProvider",
    "WorkspaceCommandProvider",
    "WorkspaceDriverProvider",
    "WorkspaceHookProvider",
    "WorkspaceAgentModeProvider",
    "WorkspacePromptProvider",
    "WorkspaceStopGateProvider",
    "WorkspaceToolProvider",
    "authorize_capability_promotion",
    "capability_conformance_snapshot",
    "promotion_risk_for_slots",
    "validate_promotion_authorization",
]
