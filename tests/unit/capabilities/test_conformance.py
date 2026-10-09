# -*- coding: utf-8 -*-
"""Tests for the Host-owned capability conformance matrix."""

from dataclasses import replace
from pathlib import Path

import pytest

from qwenpaw.capabilities.conformance import (
    CAPABILITY_CONFORMANCE,
    capability_conformance_snapshot,
    validate_capability_conformance,
    validate_conformance_evidence_paths,
    validate_conformance_implementations,
)
from qwenpaw.kernel import SLOT_CONTRACTS


def test_every_slot_has_stability_appropriate_conformance() -> None:
    validate_capability_conformance()

    assert set(CAPABILITY_CONFORMANCE) == set(SLOT_CONTRACTS)
    assert all(
        entry.level == "behavior_contract"
        for entry in CAPABILITY_CONFORMANCE.values()
        if entry.stability == "public"
    )


def test_every_declared_repository_evidence_path_exists() -> None:
    repo_root = Path(__file__).parents[3]

    validate_conformance_evidence_paths(repo_root)


def test_every_declared_system_implementation_is_importable() -> None:
    validate_conformance_implementations()


def test_new_slot_without_evidence_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reduced = dict(CAPABILITY_CONFORMANCE)
    reduced.pop("tool.provider")
    monkeypatch.setattr(
        "qwenpaw.capabilities.conformance.CAPABILITY_CONFORMANCE",
        reduced,
    )

    with pytest.raises(ValueError, match="missing=.*tool.provider"):
        validate_capability_conformance()


def test_duplicate_slot_declaration_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = tuple(CAPABILITY_CONFORMANCE.values())
    monkeypatch.setattr(
        "qwenpaw.capabilities.conformance._ENTRIES",
        (*entries, entries[0]),
    )

    with pytest.raises(ValueError, match="duplicate"):
        validate_capability_conformance()


def test_public_slot_cannot_claim_activation_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = dict(CAPABILITY_CONFORMANCE)
    entries["tool.provider"] = replace(
        entries["tool.provider"],
        level="activation_contract",
    )
    monkeypatch.setattr(
        "qwenpaw.capabilities.conformance.CAPABILITY_CONFORMANCE",
        entries,
    )

    with pytest.raises(ValueError, match="invalid conformance level"):
        validate_capability_conformance()


def test_snapshot_is_explicitly_not_runtime_health() -> None:
    snapshot = capability_conformance_snapshot()

    assert snapshot["schema_version"] == ("qwenpaw.capability-conformance.v1")
    assert snapshot["semantics"] == "declared_evidence_not_runtime_health"
    assert [item["slot"] for item in snapshot["items"]] == sorted(
        SLOT_CONTRACTS,
    )
