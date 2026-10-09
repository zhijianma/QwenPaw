# -*- coding: utf-8 -*-
"""Host-owned capability conformance declarations.

The matrix describes which evidence exists for each stable contribution slot.
It deliberately does not report live health or the result of a test run.
"""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass
from importlib import import_module
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from ..kernel.slots import SLOT_CONTRACTS, SlotStability

ConformanceLevel = Literal[
    "behavior_contract",
    "system_lifecycle",
    "activation_contract",
    "migration_boundary",
]


@dataclass(frozen=True, slots=True)
class CapabilityConformanceEntry:
    """Declared evidence boundary for one contribution slot."""

    slot: str
    stability: SlotStability
    level: ConformanceLevel
    system_implementation: str | None
    plugin_fixture: str | None
    evidence_refs: tuple[str, ...]
    limitation: str | None = None


def _entry(
    slot: str,
    level: ConformanceLevel,
    *,
    system: str | None = None,
    plugin: str | None = None,
    evidence: tuple[str, ...],
    limitation: str | None = None,
) -> CapabilityConformanceEntry:
    return CapabilityConformanceEntry(
        slot=slot,
        stability=SLOT_CONTRACTS[slot].stability,
        level=level,
        system_implementation=system,
        plugin_fixture=plugin,
        evidence_refs=evidence,
        limitation=limitation,
    )


_BEHAVIOR_ENTRIES = (
    _entry(
        "agent.mode.provider",
        "behavior_contract",
        system="qwenpaw.capabilities.system_modes:WorkspaceAgentModeProvider",
        plugin=(
            "tests/contract/os/test_agent_mode_provider_contract.py::"
            "test_system_and_plugin_modes_share_behavioral_contract"
        ),
        evidence=("tests/contract/os/test_agent_mode_provider_contract.py",),
    ),
    _entry(
        "command.provider",
        "behavior_contract",
        system=(
            "qwenpaw.capabilities.system_commands:WorkspaceCommandProvider"
        ),
        plugin=(
            "tests/contract/os/test_command_provider_contract.py::"
            "test_system_and_plugin_command_providers_share_"
            "behavioral_contract"
        ),
        evidence=("tests/contract/os/test_command_provider_contract.py",),
    ),
    _entry(
        "hook.provider",
        "behavior_contract",
        system="qwenpaw.capabilities.system_hooks:WorkspaceHookProvider",
        plugin=(
            "tests/contract/os/test_hook_provider_contract.py::"
            "test_system_and_plugin_hooks_share_behavioral_contract"
        ),
        evidence=("tests/contract/os/test_hook_provider_contract.py",),
    ),
    _entry(
        "loop.gate.provider",
        "behavior_contract",
        system=(
            "qwenpaw.capabilities.system_stop_gates:"
            "WorkspaceStopGateProvider"
        ),
        plugin=(
            "tests/contract/os/test_stop_gate_provider_contract.py::"
            "test_system_and_plugin_stop_gates_share_behavioral_contract"
        ),
        evidence=("tests/contract/os/test_stop_gate_provider_contract.py",),
    ),
    _entry(
        "planner",
        "behavior_contract",
        system="qwenpaw.tasks.system_contributions:BasicTaskPlanner",
        plugin=(
            "tests/contract/os/test_planner_strategy_contract.py::"
            "test_system_and_plugin_planning_share_orchestration_contract"
        ),
        evidence=("tests/contract/os/test_planner_strategy_contract.py",),
    ),
    _entry(
        "strategy",
        "behavior_contract",
        system=("qwenpaw.tasks.system_contributions:DefaultRuntimeStrategy"),
        plugin=(
            "tests/contract/os/test_planner_strategy_contract.py::"
            "test_system_and_plugin_planning_share_orchestration_contract"
        ),
        evidence=("tests/contract/os/test_planner_strategy_contract.py",),
    ),
    _entry(
        "tool.provider",
        "behavior_contract",
        system="qwenpaw.capabilities.system_tools:WorkspaceToolProvider",
        plugin=(
            "tests/contract/os/test_tool_provider_contract.py::"
            "test_system_and_plugin_tools_share_behavioral_contract"
        ),
        evidence=("tests/contract/os/test_tool_provider_contract.py",),
    ),
    _entry(
        "runner",
        "behavior_contract",
        system="qwenpaw.tasks.system_contributions:ConsoleAgentRunner",
        plugin=(
            "tests/contract/os/test_runner_contract.py::"
            "test_system_and_plugin_runners_share_execution_contract"
        ),
        evidence=("tests/contract/os/test_runner_contract.py",),
    ),
    _entry(
        "harness.runner",
        "behavior_contract",
        system="qwenpaw.tasks.harness_runner:HarnessTaskRunner",
        plugin=(
            "tests/contract/os/test_harness_runner_contract.py::"
            "test_system_and_plugin_harnesses_share_execution_contract"
        ),
        evidence=("tests/contract/os/test_harness_runner_contract.py",),
    ),
    _entry(
        "driver.provider",
        "behavior_contract",
        system="qwenpaw.capabilities.system_drivers:WorkspaceDriverProvider",
        plugin=(
            "tests/contract/os/test_driver_provider_contract.py::"
            "test_system_and_plugin_drivers_share_behavioral_contract"
        ),
        evidence=("tests/contract/os/test_driver_provider_contract.py",),
    ),
    _entry(
        "memory.provider",
        "behavior_contract",
        system="qwenpaw.capabilities.system_memory:WorkspaceMemoryProvider",
        plugin=(
            "tests/contract/os/test_memory_provider_contract.py::"
            "test_system_and_plugin_memory_share_behavioral_contract"
        ),
        evidence=("tests/contract/os/test_memory_provider_contract.py",),
    ),
    _entry(
        "prompt.provider",
        "behavior_contract",
        system="qwenpaw.capabilities.system_prompts:WorkspacePromptProvider",
        plugin=(
            "tests/contract/os/test_prompt_provider_contract.py::"
            "test_system_and_plugin_prompt_providers_share_behavioral_contract"
        ),
        evidence=("tests/contract/os/test_prompt_provider_contract.py",),
    ),
    _entry(
        "sensor",
        "behavior_contract",
        system="qwenpaw.tasks.system_contributions:ProactiveMemorySensor",
        plugin=(
            "tests/contract/os/test_sensor_contract.py::"
            "test_system_and_plugin_sensors_share_approval_contract"
        ),
        evidence=("tests/contract/os/test_sensor_contract.py",),
    ),
    _entry(
        "scheduler.provider",
        "behavior_contract",
        system="qwenpaw.scheduling:HostSchedulerProvider",
        plugin=(
            "tests/contract/os/test_scheduler_contract.py::"
            "test_system_and_plugin_schedulers_share_dispatch_contract"
        ),
        evidence=("tests/contract/os/test_scheduler_contract.py",),
    ),
    _entry(
        "delivery.adapter",
        "behavior_contract",
        system="qwenpaw.delivery.inbox:SystemInboxDeliveryAdapter",
        plugin=(
            "tests/contract/os/test_delivery_adapter_contract.py::"
            "test_system_and_plugin_adapters_share_task_delivery_contract"
        ),
        evidence=("tests/contract/os/test_delivery_adapter_contract.py",),
    ),
    _entry(
        "artifact.renderer",
        "behavior_contract",
        system="qwenpaw.tasks.system_contributions:SafeArtifactRenderer",
        plugin=(
            "tests/contract/os/test_artifact_renderer_contract.py::"
            "test_system_and_plugin_renderers_share_behavioral_contract"
        ),
        evidence=("tests/contract/os/test_artifact_renderer_contract.py",),
    ),
)

_SYSTEM_ENTRIES = (
    _entry(
        "agent.factory",
        "system_lifecycle",
        system="qwenpaw.capabilities.system_chat:LegacyAgentFactory",
        evidence=(
            "tests/unit/runtime/test_assembly.py",
            "tests/unit/capabilities/test_system_bundle_namespaces.py",
        ),
        limitation=(
            "Host-only boundary; third-party implementations are not "
            "accepted by the Lite plugin SDK."
        ),
    ),
)

_COMPATIBILITY_ENTRIES = tuple(
    _entry(
        slot,
        "migration_boundary",
        evidence=(
            "tests/unit/kernel/test_slots.py",
            "tests/unit/plugins/test_plugin_migration.py",
        ),
        limitation=(
            "Legacy registration boundary only; migrate to its public "
            "provider slot before claiming behavioral parity."
        ),
    )
    for slot in ("engine", "tool", "memory", "scheduler")
)

_EXPERIENCE_ENTRIES = tuple(
    _entry(
        slot,
        "activation_contract",
        system="qwenpaw.app.routers.frontend_plugin:list_frontend_plugins",
        plugin="examples/plugins/task-insights/plugin.json",
        evidence=(
            "tests/integration/test_lite_plugin_hot_activation.py",
            "tests/unit/app/routers/test_plugins_router_helpers.py",
        ),
        limitation=(
            "Activation and projection are covered; Task UI behavior is "
            "deferred until the infrastructure migration is complete."
        ),
    )
    for slot in (
        "ui.task.toolbar",
        "ui.task.tab",
        "ui.task.inspector",
        "ui.artifact.preview",
    )
) + (
    _entry(
        "ui.settings",
        "activation_contract",
        system="qwenpaw.app.routers.frontend_plugin:list_frontend_plugins",
        plugin=(
            "tests/integration/test_lite_plugin_hot_activation.py::"
            "_write_low_risk_ui_plugin"
        ),
        evidence=("tests/integration/test_lite_plugin_hot_activation.py",),
        limitation=(
            "Activation is covered; rendered settings behavior remains "
            "outside the current Chat-first milestone."
        ),
    ),
)

_ENTRIES = (
    _BEHAVIOR_ENTRIES
    + _SYSTEM_ENTRIES
    + _COMPATIBILITY_ENTRIES
    + _EXPERIENCE_ENTRIES
)
CAPABILITY_CONFORMANCE = MappingProxyType(
    {entry.slot: entry for entry in _ENTRIES},
)


def validate_capability_conformance() -> None:
    """Fail when a slot lacks evidence appropriate to its stability."""
    if set(CAPABILITY_CONFORMANCE) != set(SLOT_CONTRACTS):
        missing = sorted(set(SLOT_CONTRACTS) - set(CAPABILITY_CONFORMANCE))
        extra = sorted(set(CAPABILITY_CONFORMANCE) - set(SLOT_CONTRACTS))
        raise ValueError(
            "capability conformance mismatch: "
            f"missing={missing}, extra={extra}",
        )
    if len(_ENTRIES) != len(CAPABILITY_CONFORMANCE):
        raise ValueError("duplicate capability conformance slot")

    expected_levels = {
        "public": "behavior_contract",
        "system": "system_lifecycle",
        "compatibility": "migration_boundary",
        "experience": "activation_contract",
    }
    for slot, entry in CAPABILITY_CONFORMANCE.items():
        contract = SLOT_CONTRACTS[slot]
        if entry.stability != contract.stability:
            raise ValueError(f"conformance stability mismatch for {slot}")
        if entry.level != expected_levels[contract.stability]:
            raise ValueError(f"invalid conformance level for {slot}")
        if not entry.evidence_refs:
            raise ValueError(f"conformance evidence is required for {slot}")
        if contract.stability in {"public", "experience"}:
            if not entry.system_implementation or not entry.plugin_fixture:
                raise ValueError(
                    f"system and plugin evidence are required for {slot}",
                )
        if contract.stability == "public" and not any(
            ref.startswith("tests/contract/os/") for ref in entry.evidence_refs
        ):
            raise ValueError(f"behavior contract test is required for {slot}")


def validate_conformance_evidence_paths(repo_root: Path) -> None:
    """Verify repository paths and node names without running the tests."""
    validate_capability_conformance()
    for entry in CAPABILITY_CONFORMANCE.values():
        refs = (*entry.evidence_refs, entry.plugin_fixture)
        for reference in refs:
            if reference is None or not reference.startswith(
                ("tests/", "examples/"),
            ):
                continue
            relative_path = reference.split("::", maxsplit=1)[0]
            evidence_path = repo_root / relative_path
            if not evidence_path.is_file():
                raise FileNotFoundError(
                    f"missing conformance evidence for {entry.slot}: "
                    f"{relative_path}",
                )
            if "::" not in reference or evidence_path.suffix != ".py":
                continue
            node_name = reference.split("::", maxsplit=1)[1]
            tree = ast.parse(evidence_path.read_text(encoding="utf-8"))
            declared_names = {
                node.name
                for node in tree.body
                if isinstance(
                    node,
                    (ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef),
                )
            }
            if node_name not in declared_names:
                raise LookupError(
                    f"missing conformance node for {entry.slot}: "
                    f"{reference}",
                )


def validate_conformance_implementations() -> None:
    """Verify that every declared system implementation is importable."""
    validate_capability_conformance()
    for entry in CAPABILITY_CONFORMANCE.values():
        reference = entry.system_implementation
        if reference is None:
            continue
        module_name, separator, symbol_name = reference.partition(":")
        if not separator or not module_name or not symbol_name:
            raise ValueError(
                f"invalid system implementation for {entry.slot}: "
                f"{reference}",
            )
        module = import_module(module_name)
        if not hasattr(module, symbol_name):
            raise LookupError(
                f"missing system implementation for {entry.slot}: "
                f"{reference}",
            )


def capability_conformance_snapshot() -> dict[str, object]:
    """Return a content-safe declaration snapshot for management APIs."""
    validate_capability_conformance()
    entries = [CAPABILITY_CONFORMANCE[key] for key in sorted(SLOT_CONTRACTS)]
    return {
        "schema_version": "qwenpaw.capability-conformance.v1",
        "semantics": "declared_evidence_not_runtime_health",
        "items": [asdict(entry) for entry in entries],
    }


validate_capability_conformance()


__all__ = [
    "CAPABILITY_CONFORMANCE",
    "CapabilityConformanceEntry",
    "ConformanceLevel",
    "capability_conformance_snapshot",
    "validate_capability_conformance",
    "validate_conformance_evidence_paths",
    "validate_conformance_implementations",
]
