# -*- coding: utf-8 -*-
"""Tests for edition deployment adapters over the shared Kernel."""

from pathlib import Path

import pytest

from qwenpaw.editions import (
    DEPLOYMENT_REQUIREMENTS,
    EDITION_PROFILES,
    EditionDeploymentAdapter,
    EditionDeploymentUnavailable,
    build_deployment_adapter,
    deployment_requirements,
    describe_edition,
)
from qwenpaw.plugins.generations import GenerationRegistry


class _Workspace:
    def __init__(self, workspace_dir: Path) -> None:
        self.workspace_dir = workspace_dir


def test_every_profile_declares_deployment_adapter_requirements() -> None:
    assert set(DEPLOYMENT_REQUIREMENTS) == set(EDITION_PROFILES)
    for profile in EDITION_PROFILES.values():
        requirements = deployment_requirements(profile)
        assert requirements
        assert all("." in capability for capability in requirements)


@pytest.mark.asyncio
async def test_lite_adapter_composes_existing_runtime_ports(
    tmp_path: Path,
) -> None:
    registry = GenerationRegistry()
    adapter = build_deployment_adapter(describe_edition("lite"), registry)
    assert isinstance(adapter, EditionDeploymentAdapter)

    runtime = adapter.compose(_Workspace(tmp_path))
    task = await runtime.task_service.create_task(
        objective="Verify the Lite deployment adapter",
        agent_id="default",
    )
    artifact_store = runtime.artifact_store(tmp_path / "project")
    artifact = await artifact_store.put(
        kind="test.report",
        media_type="text/plain",
        content=b"deployment-ready",
    )

    assert runtime.profile.edition == "lite"
    assert await runtime.task_service.get_task(task.task_id) == task
    assert await artifact_store.read(artifact) == b"deployment-ready"
    assert (tmp_path / ".qwenpaw" / "lite" / "tasks.db").is_file()
    assert artifact.uri.startswith("qwenpaw-artifact://sha256/")


@pytest.mark.parametrize("edition", ["workstation", "hub"])
def test_unimplemented_editions_fail_with_exact_adapter_gaps(
    edition: str,
) -> None:
    profile = describe_edition(edition)

    with pytest.raises(EditionDeploymentUnavailable) as raised:
        build_deployment_adapter(profile, GenerationRegistry())

    assert raised.value.edition == edition
    assert raised.value.missing_adapters == deployment_requirements(profile)
    assert edition in str(raised.value)
