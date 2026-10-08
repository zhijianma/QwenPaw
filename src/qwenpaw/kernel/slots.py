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
SlotPromotionRisk = Literal["low", "medium", "high"]


@dataclass(frozen=True, slots=True)
class SlotContract:
    """Stable protocol and failure boundary for one contribution slot."""

    slot: str
    input_contract: tuple[str, ...]
    output_contract: tuple[str, ...]
    lifecycle: SlotLifecycle
    failure_mode: SlotFailureMode
    stability: SlotStability = "public"
    promotion_risk: SlotPromotionRisk = "medium"
    promotion_scenarios: tuple[str, ...] = ()


def _contract(
    slot: str,
    inputs: tuple[str, ...],
    outputs: tuple[str, ...],
    lifecycle: SlotLifecycle,
    failure_mode: SlotFailureMode = "fail_closed",
    stability: SlotStability = "public",
    promotion_risk: SlotPromotionRisk = "medium",
    promotion_scenarios: tuple[str, ...] = (),
) -> SlotContract:
    return SlotContract(
        slot=slot,
        input_contract=inputs,
        output_contract=outputs,
        lifecycle=lifecycle,
        failure_mode=failure_mode,
        stability=stability,
        promotion_risk=promotion_risk,
        promotion_scenarios=promotion_scenarios,
    )


_SLOT_CONTRACTS = {
    "engine": _contract(
        "engine",
        ("legacy.engine.config",),
        ("legacy.engine.instance",),
        "process",
        stability="compatibility",
        promotion_risk="high",
    ),
    "agent.factory": _contract(
        "agent.factory",
        ("InvocationScope", "internal.HookContext", "internal.AppServices"),
        ("internal.AgentScopeAgent",),
        "invocation",
        stability="system",
        promotion_risk="high",
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
        promotion_risk="high",
    ),
    "hook.provider": _contract(
        "hook.provider",
        ("InvocationScope", "HookHost"),
        ("HookSession",),
        "invocation",
        promotion_risk="high",
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
        promotion_risk="high",
    ),
    "tool.provider": _contract(
        "tool.provider",
        ("InvocationScope", "ToolSelection", "ToolHost"),
        ("Sequence[ToolDefinition]",),
        "invocation",
        promotion_risk="high",
        promotion_scenarios=("tool-provider.catalog",),
    ),
    "runner": _contract(
        "runner",
        ("TaskOrder", "Run", "RuntimeContext"),
        ("AsyncIterator[RunnerSignal]",),
        "task",
        promotion_risk="high",
        promotion_scenarios=("runner.preflight",),
    ),
    "harness.runner": _contract(
        "harness.runner",
        ("TaskOrder", "Run", "RuntimeContext"),
        ("AsyncIterator[RunnerSignal]",),
        "task",
        promotion_risk="high",
        promotion_scenarios=("runner.preflight",),
    ),
    "driver.provider": _contract(
        "driver.provider",
        ("InvocationScope", "DriverHost"),
        ("DriverSession",),
        "invocation",
        promotion_risk="high",
        promotion_scenarios=("driver-provider.catalog",),
    ),
    "memory": _contract(
        "memory",
        ("legacy.memory.config",),
        ("legacy.memory.backend",),
        "process",
        stability="compatibility",
        promotion_risk="high",
    ),
    "memory.provider": _contract(
        "memory.provider",
        ("InvocationScope", "MemoryHost"),
        ("MemorySession",),
        "invocation",
        promotion_risk="high",
        promotion_scenarios=("memory-provider.session",),
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
        promotion_risk="high",
    ),
    "scheduler": _contract(
        "scheduler",
        ("ScheduleDefinition", "ScheduleFire"),
        ("SchedulerPort", "ScheduleLease"),
        "process",
        promotion_risk="high",
        stability="compatibility",
    ),
    "scheduler.provider": _contract(
        "scheduler.provider",
        ("SchedulerHost",),
        ("SchedulerPort",),
        "process",
        promotion_risk="high",
        promotion_scenarios=("scheduler-provider.catalog",),
    ),
    "delivery.adapter": _contract(
        "delivery.adapter",
        ("DeliveryRequest",),
        ("DeliveryReceipt",),
        "process",
        failure_mode="fail_closed",
        promotion_risk="high",
        promotion_scenarios=("delivery-adapter.routing",),
    ),
    "artifact.renderer": _contract(
        "artifact.renderer",
        ("ArtifactRenderRequest",),
        ("ArtifactRenderResult",),
        "invocation",
        failure_mode="fallback_next",
        promotion_scenarios=("artifact-renderer.roundtrip",),
    ),
    "ui.task.toolbar": _contract(
        "ui.task.toolbar",
        ("TaskProjection",),
        ("UIContribution",),
        "experience",
        failure_mode="isolated",
        stability="experience",
        promotion_risk="low",
    ),
    "ui.task.tab": _contract(
        "ui.task.tab",
        ("TaskProjection",),
        ("UIContribution",),
        "experience",
        failure_mode="isolated",
        stability="experience",
        promotion_risk="low",
    ),
    "ui.task.inspector": _contract(
        "ui.task.inspector",
        ("TaskProjection",),
        ("UIContribution",),
        "experience",
        failure_mode="isolated",
        stability="experience",
        promotion_risk="low",
    ),
    "ui.artifact.preview": _contract(
        "ui.artifact.preview",
        ("ArtifactPreviewDescriptor",),
        ("UIContribution",),
        "experience",
        failure_mode="isolated",
        stability="experience",
        promotion_risk="low",
    ),
    "ui.settings": _contract(
        "ui.settings",
        ("SettingsProjection",),
        ("UIContribution",),
        "experience",
        failure_mode="isolated",
        stability="experience",
        promotion_risk="low",
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
    "SlotPromotionRisk",
    "slot_contract",
]
