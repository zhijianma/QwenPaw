# -*- coding: utf-8 -*-
"""Task-owned Artifact queries and generation-pinned rendering."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from ..kernel.models import (
    ArtifactPreviewDescriptor,
    ArtifactRef,
    ArtifactRenderDisposition,
    ArtifactRenderResult,
)
from ..kernel.ports import ArtifactStore, CapabilityResolver
from .application import TaskApplicationService
from .artifacts import artifact_filename
from .renderers import ArtifactPreviewTooLargeError, ArtifactRenderService


class TaskArtifactNotFoundError(LookupError):
    """Raised when an Artifact is not owned by the requested Task."""


class ArtifactContentNotFoundError(FileNotFoundError):
    """Raised when every compatible content store misses an Artifact."""


@dataclass(frozen=True, slots=True)
class TaskArtifactProjection:
    """One immutable Artifact with current preview availability."""

    artifact: ArtifactRef
    preview: ArtifactPreviewDescriptor

    def to_public_dict(self) -> dict[str, Any]:
        """Serialize the compatible Workbench Artifact shape."""
        item = self.artifact.model_dump(mode="json")
        item["preview"] = self.preview.model_dump(mode="json")
        return item


@dataclass(frozen=True, slots=True)
class RenderedTaskArtifact:
    """Safe rendered bytes and the generation that selected a Renderer."""

    result: ArtifactRenderResult
    registry_generation: int


class TaskArtifactApplicationService:
    """Authorize, locate, verify, and render Task-owned Artifacts."""

    def __init__(
        self,
        tasks: TaskApplicationService,
        resolver: CapabilityResolver,
        *,
        artifact_store_factory: Callable[[Path], ArtifactStore],
        legacy_workspace_dir: Path,
        max_preview_bytes: int,
    ) -> None:
        if max_preview_bytes <= 0:
            raise ValueError("Artifact preview budget must be positive")
        self._tasks = tasks
        self._renderer = ArtifactRenderService(resolver)
        self._artifact_store_factory = artifact_store_factory
        self._legacy_workspace_dir = Path(legacy_workspace_dir)
        self._max_preview_bytes = max_preview_bytes

    async def describe(
        self,
        artifacts: Sequence[ArtifactRef],
    ) -> tuple[TaskArtifactProjection, ...]:
        """Attach current-generation preview availability."""
        descriptions = await self._renderer.describe_previews(
            artifacts,
            max_output_bytes=self._max_preview_bytes,
        )
        return tuple(
            TaskArtifactProjection(
                artifact=artifact,
                preview=descriptions[artifact.artifact_id],
            )
            for artifact in artifacts
        )

    async def render(
        self,
        task_id: UUID,
        artifact_id: UUID,
        *,
        disposition: ArtifactRenderDisposition,
    ) -> RenderedTaskArtifact:
        """Render one verified Artifact after enforcing Task ownership."""
        workbench = await self._tasks.workbench(task_id)
        artifact = next(
            (
                record.artifact
                for record in workbench.results.artifacts
                if record.artifact.artifact_id == artifact_id
            ),
            None,
        )
        if artifact is None:
            raise TaskArtifactNotFoundError(str(artifact_id))
        if (
            disposition is ArtifactRenderDisposition.INLINE
            and artifact.size_bytes > self._max_preview_bytes
        ):
            raise ArtifactPreviewTooLargeError(
                "artifact exceeds the inline preview budget",
            )

        project_dir = workbench.task.metadata.get("workspace_dir")
        if not isinstance(project_dir, str) or not project_dir:
            raise ArtifactContentNotFoundError(str(artifact_id))
        content = await self._read(
            artifact,
            (Path(project_dir), self._legacy_workspace_dir),
        )
        budget = (
            self._max_preview_bytes
            if disposition is ArtifactRenderDisposition.INLINE
            else max(len(content), 1)
        )
        rendered, generation = await self._renderer.render(
            artifact,
            content,
            disposition=disposition,
            filename=artifact_filename(
                artifact.metadata,
                artifact.artifact_id,
            ),
            max_output_bytes=budget,
        )
        return RenderedTaskArtifact(
            result=rendered,
            registry_generation=generation,
        )

    async def _read(
        self,
        artifact: ArtifactRef,
        project_dirs: Sequence[Path],
    ) -> bytes:
        for project_dir in dict.fromkeys(project_dirs):
            store = self._artifact_store_factory(project_dir)
            try:
                return await store.read(artifact)
            except FileNotFoundError:
                continue
        raise ArtifactContentNotFoundError(str(artifact.artifact_id))


__all__ = [
    "ArtifactContentNotFoundError",
    "RenderedTaskArtifact",
    "TaskArtifactApplicationService",
    "TaskArtifactNotFoundError",
    "TaskArtifactProjection",
]
