# -*- coding: utf-8 -*-
"""Machine-readable contribution slot contracts shared by every edition."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

SlotLifecycle = Literal["process", "invocation", "task", "experience"]
SlotFailureMode = Literal["fail_closed", "fallback_next", "isolated"]
SlotStability = Literal[
    "public",
    "system",
    "compatibility",
    "experience",
]


@dataclass(frozen=True, slots=True)
class SlotContract:
    """Stable protocol and failure boundary for one contribution slot."""

    slot: str
    input_contract: tuple[str, ...]
    output_contract: tuple[str, ...]
    lifecycle: SlotLifecycle
    failure_mode: SlotFailureMode
    stability: SlotStability = "public"


def _contract(
    slot: str,
    inputs: tuple[str, ...],
    outputs: tuple[str, ...],
    lifecycle: SlotLifecycle,
    failure_mode: SlotFailureMode = "fail_closed",
    stability: SlotStability = "public",
) -> SlotContract:
    return SlotContract(
        slot=slot,
        input_contract=inputs,
        output_contract=outputs,
        lifecycle=lifecycle,
        failure_mode=failure_mode,
        stability=stability,
    )


_SLOT_CONTRACTS = {
    "engine": _contract(
        "engine",
        ("legacy.engine.config",),
        ("legacy.engine.instance",),
        "process",
        stability="compatibility",
    ),
    "agent.factory": _contract(
        "agent.factory",
        ("InvocationScope", "internal.HookContext", "internal.AppServices"),
        ("internal.AgentScopeAgent",),
        "invocation",
        stability="system",
    ),
    "agent.mode.provider": _contract(
        "agent.mode.provider",
        ("InvocationScope", "AgentModeHost"),
        ("AgentModeSession",),
        "invocation",
    ),
    "command.provider": _contract(
        "command.provider",
        ("InvocationScope", "CommandHost"),
        ("CommandSession",),
        "invocation",
    ),
    "hook.provider": _contract(
        "hook.provider",
        ("InvocationScope", "HookHost"),
        ("HookSession",),
        "invocation",
    ),
    "loop.gate.provider": _contract(
        "loop.gate.provider",
        ("InvocationScope", "StopGateHost"),
        ("StopGateSession",),
        "invocation",
    ),
    "planner": _contract(
        "planner",
        ("TaskOrder",),
        ("Sequence[PlanStep]",),
        "task",
    ),
    "strategy": _contract(
        "strategy",
        ("RuntimeContext", "TaskOrder"),
        ("JsonObject",),
        "task",
    ),
    "tool": _contract(
        "tool",
        ("legacy.tool.input",),
        ("legacy.tool.output",),
        "invocation",
        stability="compatibility",
    ),
    "tool.provider": _contract(
        "tool.provider",
        ("InvocationScope", "ToolSelection", "ToolHost"),
        ("Sequence[ToolDefinition]",),
        "invocation",
    ),
    "runner": _contract(
        "runner",
        ("TaskOrder", "Run", "RuntimeContext"),
        ("AsyncIterator[RunnerSignal]",),
        "task",
    ),
    "harness.runner": _contract(
        "harness.runner",
        ("TaskOrder", "Run", "RuntimeContext"),
        ("AsyncIterator[RunnerSignal]",),
        "task",
    ),
    "driver.provider": _contract(
        "driver.provider",
        ("InvocationScope", "DriverHost"),
        ("DriverSession",),
        "invocation",
    ),
    "memory": _contract(
        "memory",
        ("legacy.memory.config",),
        ("legacy.memory.backend",),
        "process",
        stability="compatibility",
    ),
    "memory.provider": _contract(
        "memory.provider",
        ("InvocationScope", "MemoryHost"),
        ("MemorySession",),
        "invocation",
    ),
    "prompt.provider": _contract(
        "prompt.provider",
        ("InvocationScope", "PromptHost"),
        ("Sequence[PromptFragment]",),
        "invocation",
    ),
    "sensor": _contract(
        "sensor",
        ("SensorContext",),
        ("Sequence[Proposal]",),
        "process",
    ),
    "scheduler": _contract(
        "scheduler",
        ("ScheduleDefinition", "ScheduleFire"),
        ("SchedulerPort", "ScheduleLease"),
        "process",
    ),
    "delivery.adapter": _contract(
        "delivery.adapter",
        ("DeliveryRequest",),
        ("DeliveryReceipt",),
        "process",
        failure_mode="fail_closed",
    ),
    "artifact.renderer": _contract(
        "artifact.renderer",
        ("ArtifactRenderRequest",),
        ("ArtifactRenderResult",),
        "invocation",
        failure_mode="fallback_next",
    ),
    "ui.task.toolbar": _contract(
        "ui.task.toolbar",
        ("TaskProjection",),
        ("UIContribution",),
        "experience",
        failure_mode="isolated",
        stability="experience",
    ),
    "ui.task.tab": _contract(
        "ui.task.tab",
        ("TaskProjection",),
        ("UIContribution",),
        "experience",
        failure_mode="isolated",
        stability="experience",
    ),
    "ui.task.inspector": _contract(
        "ui.task.inspector",
        ("TaskProjection",),
        ("UIContribution",),
        "experience",
        failure_mode="isolated",
        stability="experience",
    ),
    "ui.artifact.preview": _contract(
        "ui.artifact.preview",
        ("ArtifactPreviewDescriptor",),
        ("UIContribution",),
        "experience",
        failure_mode="isolated",
        stability="experience",
    ),
    "ui.settings": _contract(
        "ui.settings",
        ("SettingsProjection",),
        ("UIContribution",),
        "experience",
        failure_mode="isolated",
        stability="experience",
    ),
}

SLOT_CONTRACTS = MappingProxyType(_SLOT_CONTRACTS)
CONTRIBUTION_SLOTS = frozenset(SLOT_CONTRACTS)


def slot_contract(slot: str) -> SlotContract:
    """Return one public contract or fail closed for an unknown slot."""
    try:
        return SLOT_CONTRACTS[slot]
    except KeyError as error:
        raise LookupError(f"unknown contribution slot: {slot}") from error


__all__ = [
    "CONTRIBUTION_SLOTS",
    "SLOT_CONTRACTS",
    "SlotContract",
    "slot_contract",
]
