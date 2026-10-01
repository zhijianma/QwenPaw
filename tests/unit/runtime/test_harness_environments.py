# -*- coding: utf-8 -*-
"""Tests for third-party Harness environment declarations."""

from __future__ import annotations

import stat
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import SecretStr

from qwenpaw.harnesses.capabilities import (
    HarnessMCPServerDefinition,
    HarnessRuntimeCapabilities,
    HarnessSkillDefinition,
)
from qwenpaw.kernel import (
    EnvironmentEvidenceLevel,
    EnvironmentFilesystemMode,
    EnvironmentIsolation,
    EnvironmentMountAccess,
    EnvironmentResolutionStatus,
)
from qwenpaw.runtime.environments import (
    EnvironmentContractUnsatisfiedError,
    FilesystemEnvironmentStore,
)
from qwenpaw.runtime.harness_environments import (
    HarnessEnvironmentResolver,
    RuntimeHarnessEnvironmentManager,
    harness_environment_contract,
)


def _capabilities(tmp_path: Path) -> HarnessRuntimeCapabilities:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    return HarnessRuntimeCapabilities(
        skills=[
            HarnessSkillDefinition(
                name="review",
                directory=skill_dir,
            ),
        ],
        mcp_servers=[
            HarnessMCPServerDefinition(
                name="local-tools",
                display_name="Local tools",
                transport="stdio",
                command=sys.executable,
                cwd=tmp_path,
                env={"API_TOKEN": SecretStr("not-persisted")},
                headers={"Authorization": SecretStr("also-secret")},
            ),
        ],
    )


@pytest.mark.asyncio
async def test_codex_contract_separates_host_proof_from_provider_declaration(
    tmp_path: Path,
) -> None:
    settings: dict[str, object] = {
        "sandbox": "workspace-write",
        "_runtime_capabilities": _capabilities(tmp_path),
    }

    contract, resolution = await HarnessEnvironmentResolver().resolve(
        "codex",
        tmp_path,
        settings,
        invocation_id=uuid4(),
    )

    assert contract.isolation is EnvironmentIsolation.SANDBOX
    assert contract.filesystem_mode is EnvironmentFilesystemMode.ALLOWLIST
    assert contract.workspace.access is EnvironmentMountAccess.READ_WRITE
    assert contract.environment_variables == ("API_TOKEN",)
    assert contract.credential_refs == ("mcp:local-tools",)
    assert resolution.status is EnvironmentResolutionStatus.SATISFIED
    assert (
        resolution.evidence_level is EnvironmentEvidenceLevel.PROVIDER_DECLARED
    )
    assert resolution.enforced_constraints == (
        "workspace.host-verified",
        "dependencies.host-verified",
    )
    assert resolution.declared_constraints == (
        "provider.codex.sandbox.workspace-write",
    )
    serialized = contract.model_dump_json()
    assert "not-persisted" not in serialized
    assert "also-secret" not in serialized


@pytest.mark.asyncio
async def test_qoder_permission_is_not_reported_as_sandbox(
    tmp_path: Path,
) -> None:
    contract, resolution = await HarnessEnvironmentResolver().resolve(
        "qoder",
        tmp_path,
        {"permission_mode": "acceptEdits"},
        invocation_id=uuid4(),
    )

    assert contract.isolation is EnvironmentIsolation.HOST
    assert contract.filesystem_mode is EnvironmentFilesystemMode.HOST
    assert resolution.declared_constraints == (
        "provider.qoder.permission.acceptEdits",
    )
    assert all(
        "sandbox" not in value for value in resolution.declared_constraints
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("settings", "violation"),
    [
        (
            {"sandbox": "imaginary"},
            "environment.harness.sandbox.unknown",
        ),
        (
            {
                "_runtime_capabilities": HarnessRuntimeCapabilities(
                    mcp_servers=[
                        HarnessMCPServerDefinition(
                            name="broken",
                            display_name="Broken",
                            transport="stdio",
                        ),
                    ],
                ),
            },
            "environment.harness.mcp_command.missing",
        ),
    ],
)
async def test_invalid_provider_declaration_is_unsatisfied(
    tmp_path: Path,
    settings: dict[str, object],
    violation: str,
) -> None:
    _, resolution = await HarnessEnvironmentResolver().resolve(
        "codex",
        tmp_path,
        settings,
        invocation_id=uuid4(),
    )

    assert resolution.status is EnvironmentResolutionStatus.UNSATISFIED
    assert violation in resolution.violations


@pytest.mark.asyncio
async def test_manager_persists_one_deterministic_owner_only_record(
    tmp_path: Path,
) -> None:
    invocation_id = uuid4()
    manager = RuntimeHarnessEnvironmentManager(
        FilesystemEnvironmentStore(tmp_path),
    )

    first = await manager.resolve(
        "codex",
        tmp_path,
        {"sandbox": "read-only"},
        invocation_id=invocation_id,
        conversation_id="chat-spec-1",
    )
    second = await manager.resolve(
        "codex",
        tmp_path,
        {"sandbox": "read-only"},
        invocation_id=invocation_id,
        conversation_id="chat-spec-1",
    )

    assert first.resolution_id == second.resolution_id
    records = list(
        (tmp_path / ".qwenpaw" / "lite" / "environments").rglob(
            "environment.json",
        ),
    )
    assert len(records) == 1
    assert stat.S_IMODE(records[0].stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_manager_records_then_rejects_unsatisfied_environment(
    tmp_path: Path,
) -> None:
    manager = RuntimeHarnessEnvironmentManager(
        FilesystemEnvironmentStore(tmp_path),
    )

    with pytest.raises(EnvironmentContractUnsatisfiedError) as raised:
        await manager.resolve(
            "codex",
            tmp_path,
            {"sandbox": "unknown"},
            invocation_id=uuid4(),
            conversation_id="chat-spec-1",
        )

    assert raised.value.resolution.status is (
        EnvironmentResolutionStatus.UNSATISFIED
    )
    assert list(
        (tmp_path / ".qwenpaw" / "lite" / "environments").rglob(
            "environment.json",
        ),
    )


def test_contract_deduplicates_shared_stdio_dependencies(
    tmp_path: Path,
) -> None:
    server = {
        "display_name": "Python",
        "transport": "stdio",
        "command": sys.executable,
    }
    capabilities = HarnessRuntimeCapabilities(
        mcp_servers=[
            HarnessMCPServerDefinition(name="one", **server),
            HarnessMCPServerDefinition(name="two", **server),
        ],
    )

    contract = harness_environment_contract(
        "codex",
        tmp_path,
        {"_runtime_capabilities": capabilities},
    )

    assert len(contract.dependencies) == 1
