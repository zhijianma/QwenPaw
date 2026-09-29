# -*- coding: utf-8 -*-
"""Tests for the Lite composition boundary."""

import pytest

from qwenpaw.editions.resolver import (
    EDITION_ENV,
    EditionUnavailableError,
    resolve_edition,
)


def test_lite_profile_excludes_hub_runtime_services() -> None:
    profile = resolve_edition("lite")

    assert profile.tenant_mode == "single_user"
    assert profile.runner == "local"
    assert profile.ledger == "sqlite_wal"
    assert profile.approval_mode == "strict"
    assert not profile.remote_runner
    assert not profile.distributed_scheduler
    assert not profile.skill_evolution


def test_environment_selects_lite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(EDITION_ENV, "lite")

    assert resolve_edition().edition == "lite"


@pytest.mark.parametrize("edition", ["workstation", "hub"])
def test_unimplemented_editions_fail_closed(edition: str) -> None:
    with pytest.raises(EditionUnavailableError, match="unavailable"):
        resolve_edition(edition)
