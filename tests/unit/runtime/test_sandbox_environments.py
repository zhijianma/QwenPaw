# -*- coding: utf-8 -*-
"""Tests for action-scoped legacy Sandbox environment evidence."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from qwenpaw.kernel import (
    CapabilitySelection,
    EnvironmentResolutionStatus,
    InvocationScope,
)
from qwenpaw.runtime.environments import FilesystemEnvironmentStore
from qwenpaw.runtime.sandbox_environments import (
    RuntimeSandboxEnvironmentManager,
    SandboxEnvironmentResolver,
    sandbox_environment_contract,
)
from qwenpaw.sandbox import MountSpec, SandboxConfig, SandboxMode


def _config(tmp_path: Path, **overrides) -> SandboxConfig:
    values = {
        "mode": SandboxMode.SEATBELT,
        "workspace_dir": str(tmp_path),
        "mounts": [MountSpec(path=str(tmp_path), writable=True)],
        "deny_paths": [str(tmp_path / "secret")],
        "network_allow": [],
        "env_vars": {"SERVICE_TOKEN": "must-not-be-persisted"},
        "timeout_seconds": 15,
    }
    values.update(overrides)
    return SandboxConfig(**values)


def _backend(config: SandboxConfig, enforced: set[str]):
    return SimpleNamespace(
        config=config,
        _enforced_fields=lambda: frozenset(enforced),
    )


def test_sandbox_contract_never_contains_environment_values(
    tmp_path: Path,
) -> None:
    contract = sandbox_environment_contract(_config(tmp_path))
    payload = json.dumps(contract.model_dump(mode="json"))

    assert contract.environment_variables == ("SERVICE_TOKEN",)
    assert "must-not-be-persisted" not in payload


@pytest.mark.asyncio
async def test_sandbox_resolver_records_only_proven_constraints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(
        "qwenpaw.runtime.sandbox_environments.create_sandbox",
        lambda _: _backend(
            config,
            {"mounts", "deny_paths", "network_allow"},
        ),
    )

    contract, resolution = await SandboxEnvironmentResolver().resolve(
        config,
        invocation_id=uuid4(),
        action_id=uuid4(),
    )

    assert resolution.status is EnvironmentResolutionStatus.SATISFIED
    assert resolution.action_id is not None
    assert contract.timeout_seconds == 15
    assert "network_allow" in resolution.enforced_constraints


@pytest.mark.asyncio
async def test_sandbox_resolver_fails_closed_for_ignored_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(
        tmp_path,
        max_memory_mb=256,
        max_processes=4,
    )
    monkeypatch.setattr(
        "qwenpaw.runtime.sandbox_environments.create_sandbox",
        lambda _: _backend(
            config,
            {"mounts", "deny_paths", "network_allow"},
        ),
    )

    _, resolution = await SandboxEnvironmentResolver().resolve(
        config,
        invocation_id=uuid4(),
        action_id=uuid4(),
    )

    assert set(resolution.violations) == {
        "environment.memory_limit.unenforced",
        "environment.process_limit.unenforced",
    }


@pytest.mark.asyncio
async def test_manager_persists_action_scoped_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(
        "qwenpaw.runtime.sandbox_environments.create_sandbox",
        lambda _: _backend(
            config,
            {"mounts", "deny_paths", "network_allow"},
        ),
    )
    scope = InvocationScope(
        agent_id="default",
        chat_id="chat-1",
        session_id="transport",
        root_agent_id="default",
        root_session_id="transport",
        workspace_dir=str(tmp_path),
        registry_generation=1,
        selection=CapabilitySelection(),
    )
    action_id = uuid4()
    manager = RuntimeSandboxEnvironmentManager(
        scope,
        FilesystemEnvironmentStore(tmp_path),
    )

    reference, resolution = await manager.resolve(
        config,
        action_id=action_id,
    )
    replay_reference, replay_resolution = await manager.resolve(
        config,
        action_id=action_id,
    )

    assert reference.resolution_id == resolution.resolution_id
    assert replay_reference == reference
    assert replay_resolution.resolution_id == resolution.resolution_id
    [path] = list(
        (tmp_path / ".qwenpaw" / "lite" / "environments").glob(
            f"*/*/actions/{action_id}/environment.json",
        ),
    )
    assert "must-not-be-persisted" not in path.read_text(encoding="utf-8")
