# -*- coding: utf-8 -*-
"""Contract tests for the public contribution slot registry."""

import pytest

from qwenpaw.kernel import (
    CONTRIBUTION_SLOTS,
    SLOT_CONTRACTS,
    slot_contract,
)


def test_every_supported_slot_has_one_complete_contract() -> None:
    assert frozenset(SLOT_CONTRACTS) == CONTRIBUTION_SLOTS
    for slot, contract in SLOT_CONTRACTS.items():
        assert contract.slot == slot
        assert contract.input_contract
        assert contract.output_contract
        assert contract.lifecycle in {
            "process",
            "invocation",
            "task",
            "experience",
        }
        assert contract.failure_mode in {
            "fail_closed",
            "fallback_next",
            "isolated",
        }
        assert contract.promotion_risk in {"low", "medium", "high"}


def test_execution_slots_fail_closed_except_renderer_fallback() -> None:
    execution_slots = {
        "agent.factory",
        "agent.mode.provider",
        "command.provider",
        "hook.provider",
        "loop.gate.provider",
        "planner",
        "strategy",
        "tool.provider",
        "runner",
        "harness.runner",
        "driver.provider",
        "memory.provider",
        "prompt.provider",
        "sensor",
        "scheduler",
    }
    assert all(
        slot_contract(slot).failure_mode == "fail_closed"
        for slot in execution_slots
    )
    assert slot_contract("artifact.renderer").failure_mode == "fallback_next"
    assert slot_contract("delivery.adapter").failure_mode == "fail_closed"


def test_legacy_slots_are_explicit_compatibility_boundaries() -> None:
    assert {
        slot
        for slot, contract in SLOT_CONTRACTS.items()
        if contract.stability == "compatibility"
    } == {"engine", "tool", "memory"}


def test_agent_factory_is_an_explicit_system_boundary() -> None:
    contract = slot_contract("agent.factory")

    assert contract.stability == "system"
    assert contract.lifecycle == "invocation"
    assert contract.failure_mode == "fail_closed"


def test_promotion_risk_and_scenarios_are_machine_readable() -> None:
    assert slot_contract("tool.provider").promotion_risk == "high"
    assert slot_contract("tool.provider").promotion_scenarios == (
        "tool-provider.catalog",
    )
    assert slot_contract("memory.provider").promotion_scenarios == (
        "memory-provider.session",
    )
    assert slot_contract("runner").promotion_risk == "high"
    assert slot_contract("ui.settings").promotion_risk == "low"
    assert slot_contract("artifact.renderer").promotion_scenarios == (
        "artifact-renderer.roundtrip",
    )


def test_unknown_slot_contract_fails_closed() -> None:
    with pytest.raises(LookupError, match="unknown contribution slot"):
        slot_contract("unknown.slot")
