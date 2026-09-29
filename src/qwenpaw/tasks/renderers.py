# -*- coding: utf-8 -*-
"""Generation-pinned, fail-closed rendering for immutable artifacts."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from uuid import UUID

from ..kernel.models import (
    ArtifactPreviewDescriptor,
    ArtifactRef,
    ArtifactRenderDisposition,
    ArtifactRenderRequest,
    ArtifactRenderResult,
)
from ..kernel.ports import (
    ArtifactRenderer,
    CapabilityLease,
    CapabilityResolver,
    ExecutableCapabilityLease,
)

logger = logging.getLogger(__name__)

SAFE_INLINE_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "text/markdown",
        "text/plain",
    },
)


class ArtifactRendererUnavailableError(LookupError):
    """Raised when no healthy renderer can produce the requested view."""

    def __init__(
        self,
        media_type: str,
        failures: tuple[str, ...] = (),
    ) -> None:
        self.media_type = media_type
        self.failures = failures
        suffix = f"; failed: {', '.join(failures)}" if failures else ""
        super().__init__(f"no renderer accepted '{media_type}'{suffix}")


class ArtifactRendererContractError(ValueError):
    """Raised when a renderer returns an unsafe or inconsistent result."""


class ArtifactPreviewTooLargeError(ValueError):
    """Raised before loading an artifact that exceeds the preview budget."""


class SafeArtifactRenderer:
    """Built-in raw download and UTF-8 text preview renderer."""

    priority = 10_000

    def __init__(self, renderer_id: str) -> None:
        self.renderer_id = renderer_id

    async def health_check(self) -> bool:
        """Report that the stateless renderer is ready."""
        return True

    def supports(
        self,
        artifact: ArtifactRef,
        disposition: ArtifactRenderDisposition,
    ) -> bool:
        """Allow all downloads and only inert text previews."""
        return (
            disposition is ArtifactRenderDisposition.ATTACHMENT
            or artifact.media_type in SAFE_INLINE_MEDIA_TYPES
        )

    async def render(
        self,
        request: ArtifactRenderRequest,
    ) -> ArtifactRenderResult:
        """Preserve verified bytes after validating inline UTF-8 text."""
        if request.disposition is ArtifactRenderDisposition.INLINE:
            request.content.decode("utf-8")
        return ArtifactRenderResult(
            renderer_id=self.renderer_id,
            content=request.content,
            media_type=request.artifact.media_type,
            filename=request.filename,
            disposition=request.disposition,
            source_content_hash=request.artifact.content_hash,
        )


class ArtifactRenderService:
    """Resolve Artifact Renderers from one immutable generation per call."""

    def __init__(self, resolver: CapabilityResolver) -> None:
        self._resolver = resolver

    @staticmethod
    def _renderers(
        lease: CapabilityLease,
    ) -> tuple[ArtifactRenderer, ...]:
        if not isinstance(lease, ExecutableCapabilityLease):
            return ()
        renderers: list[ArtifactRenderer] = []
        for descriptor in lease.descriptors("artifact.renderer"):
            implementation = lease.implementation(
                descriptor.capability_id,
            )
            if not isinstance(implementation, ArtifactRenderer):
                logger.warning(
                    "artifact renderer '%s' does not satisfy the port",
                    descriptor.capability_id,
                )
                continue
            if implementation.renderer_id != descriptor.capability_id:
                logger.warning(
                    "artifact renderer identity mismatch: %s",
                    descriptor.capability_id,
                )
                continue
            if not isinstance(implementation.priority, int):
                logger.warning(
                    "artifact renderer '%s' has an invalid priority",
                    descriptor.capability_id,
                )
                continue
            renderers.append(implementation)
        return tuple(
            sorted(
                renderers,
                key=lambda item: (item.priority, item.renderer_id),
            ),
        )

    @staticmethod
    def _first_supported(
        renderers: Sequence[ArtifactRenderer],
        artifact: ArtifactRef,
        disposition: ArtifactRenderDisposition,
    ) -> ArtifactRenderer | None:
        for renderer in renderers:
            try:
                if renderer.supports(artifact, disposition):
                    return renderer
            except Exception:  # noqa: BLE001
                logger.warning(
                    "artifact renderer '%s' support check raised",
                    renderer.renderer_id,
                    exc_info=True,
                )
        return None

    async def describe_previews(
        self,
        artifacts: Sequence[ArtifactRef],
        *,
        max_output_bytes: int,
    ) -> dict[UUID, ArtifactPreviewDescriptor]:
        """Describe current-generation inline support without reading bytes."""
        lease = await self._resolver.pin()
        try:
            renderers = self._renderers(lease)
            descriptions: dict[UUID, ArtifactPreviewDescriptor] = {}
            for artifact in artifacts:
                if artifact.size_bytes > max_output_bytes:
                    descriptions[
                        artifact.artifact_id
                    ] = ArtifactPreviewDescriptor(
                        available=False,
                        registry_generation=lease.generation,
                        reason="too_large",
                    )
                    continue
                renderer = self._first_supported(
                    renderers,
                    artifact,
                    ArtifactRenderDisposition.INLINE,
                )
                descriptions[artifact.artifact_id] = ArtifactPreviewDescriptor(
                    available=renderer is not None,
                    registry_generation=lease.generation,
                    renderer_id=(
                        renderer.renderer_id if renderer is not None else None
                    ),
                    reason=("" if renderer is not None else "unsupported"),
                )
            return descriptions
        finally:
            await lease.close()

    @staticmethod
    def _validate_result(
        request: ArtifactRenderRequest,
        renderer: ArtifactRenderer,
        result: ArtifactRenderResult,
    ) -> None:
        if result.renderer_id != renderer.renderer_id:
            raise ArtifactRendererContractError(
                "renderer result identity does not match capability",
            )
        if result.source_content_hash != request.artifact.content_hash:
            raise ArtifactRendererContractError(
                "renderer result does not reference the source digest",
            )
        if result.disposition is not request.disposition:
            raise ArtifactRendererContractError(
                "renderer changed the requested disposition",
            )
        if result.filename != request.filename:
            raise ArtifactRendererContractError(
                "renderer changed the safe artifact filename",
            )
        if len(result.content) > request.max_output_bytes:
            raise ArtifactRendererContractError(
                "renderer output exceeds the request budget",
            )
        if (
            request.disposition is ArtifactRenderDisposition.INLINE
            and result.media_type not in SAFE_INLINE_MEDIA_TYPES
        ):
            raise ArtifactRendererContractError(
                "renderer returned an unsafe inline media type",
            )
        if request.disposition is ArtifactRenderDisposition.ATTACHMENT and (
            result.content != request.content
            or result.media_type != request.artifact.media_type
        ):
            raise ArtifactRendererContractError(
                "attachment rendering must preserve source bytes and type",
            )

    async def render(
        self,
        artifact: ArtifactRef,
        content: bytes,
        *,
        disposition: ArtifactRenderDisposition,
        filename: str,
        max_output_bytes: int,
    ) -> tuple[ArtifactRenderResult, int]:
        """Render through one lease, falling back after provider failure."""
        if (
            disposition is ArtifactRenderDisposition.INLINE
            and artifact.size_bytes > max_output_bytes
        ):
            raise ArtifactPreviewTooLargeError(
                "artifact exceeds the inline preview budget",
            )
        request = ArtifactRenderRequest(
            artifact=artifact,
            content=content,
            disposition=disposition,
            filename=filename,
            max_output_bytes=max_output_bytes,
        )
        lease = await self._resolver.pin()
        failures: list[str] = []
        try:
            for renderer in self._renderers(lease):
                try:
                    if not renderer.supports(artifact, disposition):
                        continue
                    result = await renderer.render(request)
                    self._validate_result(request, renderer, result)
                    return result, lease.generation
                except Exception:  # noqa: BLE001
                    failures.append(renderer.renderer_id)
                    logger.warning(
                        "artifact renderer '%s' failed; trying fallback",
                        renderer.renderer_id,
                        exc_info=True,
                    )
        finally:
            await lease.close()
        raise ArtifactRendererUnavailableError(
            artifact.media_type,
            tuple(failures),
        )


__all__ = [
    "ArtifactPreviewTooLargeError",
    "ArtifactRendererContractError",
    "ArtifactRendererUnavailableError",
    "ArtifactRenderService",
    "SAFE_INLINE_MEDIA_TYPES",
    "SafeArtifactRenderer",
]
