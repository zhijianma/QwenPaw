# -*- coding: utf-8 -*-
"""Contract tests shared by every declared product profile."""

import ast
from pathlib import Path

import pytest
import qwenpaw.editions

from qwenpaw.editions import EDITION_PROFILES, describe_edition
from qwenpaw.kernel import CONTRIBUTION_SLOTS


def test_catalog_declares_all_product_profiles() -> None:
    assert tuple(EDITION_PROFILES) == ("lite", "workstation", "hub")
    assert {profile.edition for profile in EDITION_PROFILES.values()} == set(
        EDITION_PROFILES,
    )


@pytest.mark.parametrize("edition", ["lite", "workstation", "hub"])
def test_profiles_share_contribution_contract(edition: str) -> None:
    profile = describe_edition(edition)

    assert profile.supported_slots == CONTRIBUTION_SLOTS
    assert "runner" in profile.supported_slots
    assert "ui.artifact.preview" in profile.supported_slots


def test_profiles_express_deployment_differences_as_data() -> None:
    lite = describe_edition("lite")
    workstation = describe_edition("workstation")
    hub = describe_edition("hub")

    assert lite.runner == "local"
    assert workstation.runner == "local_pool"
    assert hub.runner == "distributed"
    assert lite.ledger == workstation.ledger == "sqlite_wal"
    assert hub.ledger == "database"
    assert not lite.remote_runner
    assert workstation.remote_runner
    assert hub.remote_runner
    assert not workstation.distributed_scheduler
    assert hub.distributed_scheduler


def test_describe_unknown_edition_fails_closed() -> None:
    with pytest.raises(ValueError, match="unknown edition"):
        describe_edition("enterprise")


def test_profiles_do_not_import_product_implementations() -> None:
    editions_dir = Path(qwenpaw.editions.__file__).parent
    forbidden = (
        "qwenpaw.app",
        "qwenpaw.tasks",
        "qwenpaw.plugins",
        "qwenpaw.hub",
        "qwenpaw.harnesses",
    )
    violations: list[str] = []

    for name in ("lite.py", "workstation.py", "hub.py"):
        path = editions_dir / name
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(forbidden):
                        violations.append(f"{name}:{node.lineno}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith(forbidden):
                    violations.append(f"{name}:{node.lineno}")

    assert not violations
