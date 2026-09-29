# -*- coding: utf-8 -*-
"""Safe Artifact Renderer implemented only with the public plugin SDK."""

from qwenpaw.plugins.sdk import (
    ArtifactRef,
    ArtifactRenderDisposition,
    ArtifactRenderRequest,
    ArtifactRenderResult,
)


class TaskInsightRenderer:
    """Add a presentation heading to Task Insight Markdown previews."""

    renderer_id = "task-insights.summary-renderer"
    priority = 200

    async def health_check(self) -> bool:
        """Report that the stateless renderer is ready."""
        return True

    def supports(
        self,
        artifact: ArtifactRef,
        disposition: ArtifactRenderDisposition,
    ) -> bool:
        """Handle only inline views of this plugin's artifact kind."""
        return (
            disposition is ArtifactRenderDisposition.INLINE
            and artifact.kind == "task.summary"
            and artifact.media_type == "text/markdown"
        )

    async def render(
        self,
        request: ArtifactRenderRequest,
    ) -> ArtifactRenderResult:
        """Return a bounded Markdown projection tied to the source hash."""
        source = request.content.decode("utf-8")
        content = f"# Task Insights Preview\n\n{source}".encode("utf-8")
        return ArtifactRenderResult(
            renderer_id=self.renderer_id,
            content=content,
            media_type="text/markdown",
            filename=request.filename,
            disposition=request.disposition,
            source_content_hash=request.artifact.content_hash,
        )


def create_renderer() -> TaskInsightRenderer:
    """Create the renderer without importing application internals."""
    return TaskInsightRenderer()
