# -*- coding: utf-8 -*-
"""Provider-neutral implementation contracts for capability slots."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ..kernel.models import CapabilityContribution
from ..kernel.ports import (
    AgentFactory,
    AgentModeProvider,
    ArtifactRenderer,
    CommandProvider,
    DeliveryAdapter,
    DriverProvider,
    HookProvider,
    MemoryProvider,
    PromptProvider,
    ProposalSensor,
    RuntimeStrategy,
    SchedulerPort,
    SchedulerProvider,
    StopGateProvider,
    TaskPlanner,
    TaskRunner,
    ToolProvider,
)


class CapabilityImplementationError(ValueError):
    """Raised when any provider violates one shared slot contract."""


SYSTEM_IMPLEMENTATION_CONTRACTS: Mapping[
    str,
    tuple[type[object], str | None],
] = MappingProxyType(
    {
        "agent.factory": (AgentFactory, "factory_id"),
    },
)

PUBLIC_IMPLEMENTATION_CONTRACTS: Mapping[
    str,
    tuple[type[object], str | None],
] = MappingProxyType(
    {
        "agent.mode.provider": (AgentModeProvider, "provider_id"),
        "command.provider": (CommandProvider, "provider_id"),
        "hook.provider": (HookProvider, "provider_id"),
        "loop.gate.provider": (StopGateProvider, "provider_id"),
        "planner": (TaskPlanner, "planner_id"),
        "strategy": (RuntimeStrategy, "strategy_id"),
        "tool.provider": (ToolProvider, "provider_id"),
        "runner": (TaskRunner, "runner_id"),
        "harness.runner": (TaskRunner, "runner_id"),
        "driver.provider": (DriverProvider, "provider_id"),
        "memory.provider": (MemoryProvider, "provider_id"),
        "prompt.provider": (PromptProvider, "provider_id"),
        "sensor": (ProposalSensor, "sensor_id"),
        "scheduler.provider": (SchedulerProvider, "provider_id"),
        "delivery.adapter": (DeliveryAdapter, "adapter_id"),
        "artifact.renderer": (ArtifactRenderer, "renderer_id"),
    },
)

COMPATIBILITY_IMPLEMENTATION_CONTRACTS: Mapping[
    str,
    tuple[type[object], str | None],
] = MappingProxyType(
    {
        "scheduler": (SchedulerPort, None),
    },
)

IMPLEMENTATION_CONTRACTS: Mapping[
    str,
    tuple[type[object], str | None],
] = MappingProxyType(
    {
        **SYSTEM_IMPLEMENTATION_CONTRACTS,
        **PUBLIC_IMPLEMENTATION_CONTRACTS,
        **COMPATIBILITY_IMPLEMENTATION_CONTRACTS,
    },
)


def validate_capability_implementation(
    provider_id: str,
    declaration: CapabilityContribution,
    implementation: object,
) -> None:
    """Apply one implementation contract to system and plugin providers."""
    capability_id = f"{provider_id}.{declaration.contribution_id}"
    contract = IMPLEMENTATION_CONTRACTS.get(declaration.slot)
    if contract is not None:
        protocol, identity_field = contract
        if not isinstance(implementation, protocol):
            raise CapabilityImplementationError(
                f"capability '{capability_id}' does not implement the "
                f"'{declaration.slot}' contract",
            )
        if identity_field is not None:
            actual_id = getattr(implementation, identity_field, None)
            if actual_id != capability_id:
                raise CapabilityImplementationError(
                    f"capability '{capability_id}' declares "
                    f"{identity_field}={actual_id!r}",
                )
        return

    if declaration.slot.startswith("ui."):
        if not isinstance(implementation, Mapping):
            raise CapabilityImplementationError(
                f"UI capability '{capability_id}' must return a mapping",
            )
        entrypoint = implementation.get("entrypoint")
        if not isinstance(entrypoint, str) or not entrypoint.strip():
            raise CapabilityImplementationError(
                f"UI capability '{capability_id}' requires a non-empty "
                f"entrypoint",
            )
        return

    if declaration.slot == "tool" and not callable(implementation):
        raise CapabilityImplementationError(
            f"compatibility tool '{capability_id}' must be callable",
        )
    if declaration.slot in {"engine", "memory"} and implementation is None:
        raise CapabilityImplementationError(
            f"compatibility capability '{capability_id}' cannot be None",
        )


__all__ = [
    "CapabilityImplementationError",
    "COMPATIBILITY_IMPLEMENTATION_CONTRACTS",
    "IMPLEMENTATION_CONTRACTS",
    "PUBLIC_IMPLEMENTATION_CONTRACTS",
    "SYSTEM_IMPLEMENTATION_CONTRACTS",
    "validate_capability_implementation",
]
