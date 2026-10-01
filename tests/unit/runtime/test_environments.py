# -*- coding: utf-8 -*-
"""Tests for Lite environment contract resolution and evidence."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from uuid import uuid4

import pytest

from qwenpaw.kernel import (
    EnvironmentContract,
    EnvironmentDependency,
    EnvironmentDependencyKind,
    EnvironmentIsolation,
    EnvironmentMount,
    EnvironmentMountAccess,
    EnvironmentNetworkMode,
    EnvironmentRecord,
    EnvironmentResolutionStatus,
    EnvironmentResourceLimits,
)
from qwenpaw.runtime.environments import (
    EnvironmentContractConflictError,
    FilesystemEnvironmentStore,
    LiteEnvironmentResolver,
    default_lite_environment_contract,
)


@pytest.mark.asyncio
async def test_default_lite_environment_is_satisfied_and_private(
    tmp_path: Path,
) -> None:
    invocation_id = uuid4()
    contract = default_lite_environment_contract(tmp_path)
    resolver = LiteEnvironmentResolver()
    resolution = await resolver.resolve(
        contract,
        invocation_id=invocation_id,
        workspace_dir=str(tmp_path),
    )

    assert resolution.status is EnvironmentResolutionStatus.SATISFIED
    assert resolution.invocation_id == invocation_id
    assert resolution.violations == ()
    assert "workspace" in resolution.enforced_constraints

    store = FilesystemEnvironmentStore(tmp_path)
    record = EnvironmentRecord(contract=contract, resolution=resolution)
    await store.record(record, conversation_id="chat-1")
    [path] = list(
        (tmp_path / ".qwenpaw" / "lite" / "environments").glob(
            "*/*/environment.json",
        ),
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert EnvironmentRecord.model_validate(json.loads(path.read_text())) == (
        record
    )


@pytest.mark.asyncio
async def test_lite_environment_rejects_unenforced_requirements(
    tmp_path: Path,
) -> None:
    contract = EnvironmentContract(
        contract_id="example.environment.strict",
        workspace=EnvironmentMount(
            source=str(tmp_path),
            target="workspace",
            access=EnvironmentMountAccess.READ_WRITE,
        ),
        isolation=EnvironmentIsolation.CONTAINER,
        runtime_image="example/runtime:1.0",
        network_mode=EnvironmentNetworkMode.RESTRICTED,
        allowed_hosts=("example.com",),
        credential_refs=("service-token",),
        resources=EnvironmentResourceLimits(memory_mb=256),
        timeout_seconds=30,
        max_concurrency=2,
        snapshot_required=True,
        cleanup_policy="delete_temporary",
    )

    resolution = await LiteEnvironmentResolver().resolve(
        contract,
        invocation_id=uuid4(),
        workspace_dir=str(tmp_path),
    )

    assert resolution.status is EnvironmentResolutionStatus.UNSATISFIED
    assert set(resolution.violations) == {
        "environment.cleanup.unsupported",
        "environment.concurrency.unsupported",
        "environment.credentials.unsupported",
        "environment.image.unsupported",
        "environment.isolation.unsupported",
        "environment.network.unsupported",
        "environment.resources.unsupported",
        "environment.snapshot.unsupported",
        "environment.timeout.unsupported",
    }


@pytest.mark.asyncio
async def test_lite_environment_checks_required_dependency(
    tmp_path: Path,
) -> None:
    contract = EnvironmentContract(
        workspace=EnvironmentMount(
            source=str(tmp_path),
            target="workspace",
            access=EnvironmentMountAccess.READ_WRITE,
        ),
        dependencies=(
            EnvironmentDependency(
                kind=EnvironmentDependencyKind.EXECUTABLE,
                name="qwenpaw-definitely-missing-executable",
            ),
        ),
    )

    resolution = await LiteEnvironmentResolver().resolve(
        contract,
        invocation_id=uuid4(),
        workspace_dir=str(tmp_path),
    )

    assert resolution.status is EnvironmentResolutionStatus.UNSATISFIED
    assert resolution.violations == ("environment.dependency.missing",)


@pytest.mark.asyncio
async def test_lite_environment_rejects_unverified_dependency_version(
    tmp_path: Path,
) -> None:
    contract = EnvironmentContract(
        workspace=EnvironmentMount(
            source=str(tmp_path),
            target="workspace",
            access=EnvironmentMountAccess.READ_WRITE,
        ),
        dependencies=(
            EnvironmentDependency(
                kind=EnvironmentDependencyKind.PATH,
                name=".",
                version_constraint=">=1.0",
            ),
        ),
    )

    resolution = await LiteEnvironmentResolver().resolve(
        contract,
        invocation_id=uuid4(),
        workspace_dir=str(tmp_path),
    )

    assert resolution.violations == (
        "environment.dependency.version.unsupported",
    )


@pytest.mark.asyncio
async def test_environment_store_rejects_conflicting_evidence(
    tmp_path: Path,
) -> None:
    invocation_id = uuid4()
    contract = default_lite_environment_contract(tmp_path)
    resolver = LiteEnvironmentResolver()
    resolution = await resolver.resolve(
        contract,
        invocation_id=invocation_id,
        workspace_dir=str(tmp_path),
    )
    store = FilesystemEnvironmentStore(tmp_path)
    await store.record(
        EnvironmentRecord(contract=contract, resolution=resolution),
        conversation_id="chat-1",
    )
    conflicting = resolution.model_copy(
        update={"architecture": "different-architecture"},
    )

    with pytest.raises(EnvironmentContractConflictError):
        await store.record(
            EnvironmentRecord(contract=contract, resolution=conflicting),
            conversation_id="chat-1",
        )
