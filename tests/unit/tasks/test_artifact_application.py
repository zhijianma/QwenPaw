# -*- coding: utf-8 -*-
"""Tests for transport-neutral Task Artifact application use cases."""

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from qwenpaw.kernel.models import (
    ArtifactRecord,
    ArtifactRenderDisposition,
    Task,
    TaskSource,
)
from qwenpaw.plugins.generations import GenerationRegistry
from qwenpaw.tasks.artifact_application import (
    ArtifactContentNotFoundError,
    TaskArtifactApplicationService,
    TaskArtifactNotFoundError,
)
from qwenpaw.tasks.artifacts import FilesystemArtifactStore
from qwenpaw.tasks.renderers import ArtifactPreviewTooLargeError
from qwenpaw.tasks.results import TaskResultProjection
from qwenpaw.tasks.system_contributions import (
    SYSTEM_CAPABILITY_BUNDLE,
    SYSTEM_SAFE_ARTIFACT_RENDERER_ID,
    system_contribution_factory,
)


class _TaskQueries:
    def __init__(self, workbench) -> None:
        self._workbench = workbench

    async def workbench(self, _task_id):
        return self._workbench


def _store(project_dir: Path) -> FilesystemArtifactStore:
    return FilesystemArtifactStore(
        project_dir / ".qwenpaw" / "lite" / "artifacts",
    )


async def _registry(workspace) -> GenerationRegistry:
    async def resolve_workspace(_agent_id: str):
        return workspace

    registry = GenerationRegistry()
    await registry.ensure_bundle(
        SYSTEM_CAPABILITY_BUNDLE,
        system_contribution_factory(resolve_workspace),
    )
    return registry


@pytest.mark.asyncio
async def test_artifact_application_reads_legacy_store_and_renders(
    tmp_path: Path,
) -> None:
    canonical_dir = tmp_path / "project"
    legacy_dir = tmp_path / "workspace"
    content = b"# Durable result\n"
    artifact = await _store(legacy_dir).put(
        kind="document.result",
        media_type="text/markdown",
        content=content,
        metadata={"name": "nested/result.md"},
    )
    task_id = uuid4()
    run_id = uuid4()
    task = Task(
        task_id=task_id,
        objective="Render a result",
        source=TaskSource.USER,
        agent_id="default",
        metadata={"workspace_dir": str(canonical_dir)},
    )
    results = TaskResultProjection(
        artifacts=(
            ArtifactRecord(
                artifact=artifact,
                task_id=task_id,
                run_id=run_id,
                event_id=uuid4(),
                producer="runner.tests",
            ),
        ),
        evidence=(),
        verifications=(),
    )
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=legacy_dir,
    )
    application = TaskArtifactApplicationService(
        _TaskQueries(SimpleNamespace(task=task, results=results)),
        await _registry(workspace),
        artifact_store_factory=_store,
        legacy_workspace_dir=legacy_dir,
        max_preview_bytes=1024,
    )

    projections = await application.describe((artifact,))
    rendered = await application.render(
        task_id,
        artifact.artifact_id,
        disposition=ArtifactRenderDisposition.INLINE,
    )

    assert projections[0].preview.available is True
    assert projections[0].preview.renderer_id == (
        SYSTEM_SAFE_ARTIFACT_RENDERER_ID
    )
    assert rendered.result.content == content
    assert rendered.result.filename == "result.md"
    assert rendered.registry_generation == projections[
        0
    ].preview.registry_generation


@pytest.mark.asyncio
async def test_artifact_application_fails_closed_before_content_read(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project"
    artifact = await _store(project_dir).put(
        kind="document.result",
        media_type="text/plain",
        content=b"content",
    )
    task_id = uuid4()
    task = Task(
        task_id=task_id,
        objective="Guard a result",
        source=TaskSource.USER,
        agent_id="default",
        metadata={"workspace_dir": str(project_dir)},
    )
    record = ArtifactRecord(
        artifact=artifact,
        task_id=task_id,
        run_id=uuid4(),
        event_id=uuid4(),
        producer="runner.tests",
    )
    results = TaskResultProjection(
        artifacts=(record,),
        evidence=(),
        verifications=(),
    )
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=project_dir,
    )
    application = TaskArtifactApplicationService(
        _TaskQueries(SimpleNamespace(task=task, results=results)),
        await _registry(workspace),
        artifact_store_factory=_store,
        legacy_workspace_dir=project_dir,
        max_preview_bytes=4,
    )

    with pytest.raises(ArtifactPreviewTooLargeError):
        await application.render(
            task_id,
            artifact.artifact_id,
            disposition=ArtifactRenderDisposition.INLINE,
        )
    with pytest.raises(TaskArtifactNotFoundError):
        await application.render(
            task_id,
            uuid4(),
            disposition=ArtifactRenderDisposition.ATTACHMENT,
        )

    missing = record.model_copy(
        update={
            "artifact": artifact.model_copy(
                update={
                    "artifact_id": uuid4(),
                    "content_hash": f"sha256:{'0' * 64}",
                },
            ),
        },
    )
    application = TaskArtifactApplicationService(
        _TaskQueries(
            SimpleNamespace(
                task=task,
                results=TaskResultProjection(
                    artifacts=(missing,),
                    evidence=(),
                    verifications=(),
                ),
            ),
        ),
        await _registry(workspace),
        artifact_store_factory=_store,
        legacy_workspace_dir=project_dir,
        max_preview_bytes=1024,
    )
    with pytest.raises(ArtifactContentNotFoundError):
        await application.render(
            task_id,
            missing.artifact.artifact_id,
            disposition=ArtifactRenderDisposition.ATTACHMENT,
        )
