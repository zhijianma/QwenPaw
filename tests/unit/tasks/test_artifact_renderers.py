# -*- coding: utf-8 -*-
"""Tests for generation-pinned Artifact Renderer Contributions."""

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from qwenpaw.kernel.models import (
    ArtifactRef,
    ArtifactRenderDisposition,
    ArtifactRenderResult,
    CapabilityBundle,
    CapabilityContribution,
    CapabilityProviderKind,
)
from qwenpaw.plugins.generations import GenerationRegistry
from qwenpaw.tasks.renderers import (
    ArtifactRendererUnavailableError,
    ArtifactRenderService,
)
from qwenpaw.tasks.system_contributions import (
    SYSTEM_CAPABILITY_BUNDLE,
    SYSTEM_SAFE_ARTIFACT_RENDERER_ID,
    system_contribution_factory,
)


def _artifact(
    content: bytes,
    media_type: str = "text/markdown",
) -> ArtifactRef:
    digest = hashlib.sha256(content).hexdigest()
    return ArtifactRef(
        kind="test.output",
        uri=f"qwenpaw-artifact://sha256/{digest}",
        media_type=media_type,
        content_hash=f"sha256:{digest}",
        size_bytes=len(content),
    )


async def _system_registry(tmp_path: Path) -> GenerationRegistry:
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
    )

    async def resolve_workspace(_agent_id: str):
        return workspace

    registry = GenerationRegistry()
    await registry.activate_bundle(
        SYSTEM_CAPABILITY_BUNDLE,
        system_contribution_factory(resolve_workspace),
    )
    return registry


@pytest.mark.asyncio
async def test_system_renderer_describes_and_renders_safe_text(
    tmp_path: Path,
) -> None:
    registry = await _system_registry(tmp_path)
    service = ArtifactRenderService(registry)
    content = b"# Result\n"
    artifact = _artifact(content)

    descriptions = await service.describe_previews(
        (artifact,),
        max_output_bytes=1024,
    )
    rendered, generation = await service.render(
        artifact,
        content,
        disposition=ArtifactRenderDisposition.INLINE,
        filename="result.md",
        max_output_bytes=1024,
    )

    preview = descriptions[artifact.artifact_id]
    assert preview.available is True
    assert preview.renderer_id == SYSTEM_SAFE_ARTIFACT_RENDERER_ID
    assert preview.registry_generation == generation
    assert rendered.content == content
    assert rendered.source_content_hash == artifact.content_hash


@pytest.mark.asyncio
async def test_description_rejects_large_and_unsupported_inline_artifacts(
    tmp_path: Path,
) -> None:
    registry = await _system_registry(tmp_path)
    service = ArtifactRenderService(registry)
    large = _artifact(b"large")
    binary = _artifact(b"binary", "application/octet-stream")

    descriptions = await service.describe_previews(
        (large, binary),
        max_output_bytes=2,
    )

    assert descriptions[large.artifact_id].reason == "too_large"
    assert descriptions[binary.artifact_id].reason == "too_large"
    unsupported = await service.describe_previews(
        (binary,),
        max_output_bytes=1024,
    )
    assert unsupported[binary.artifact_id].reason == "unsupported"


class _PluginRenderer:
    priority = 100
    renderer_id = "example.renderers.preview"

    def __init__(
        self,
        version: str,
        *,
        entered: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
        fail: bool = False,
    ) -> None:
        self.version = version
        self._entered = entered
        self._release = release
        self._fail = fail

    async def health_check(self) -> bool:
        return True

    def supports(self, _artifact, _disposition) -> bool:
        return True

    async def render(self, request):
        if self._entered is not None:
            self._entered.set()
        if self._release is not None:
            await self._release.wait()
        if self._fail:
            raise RuntimeError("renderer failed")
        content = f"plugin-{self.version}".encode()
        return ArtifactRenderResult(
            renderer_id=self.renderer_id,
            content=content,
            media_type="text/plain",
            filename=request.filename,
            disposition=request.disposition,
            source_content_hash=request.artifact.content_hash,
        )


def _plugin_bundle(version: str) -> CapabilityBundle:
    return CapabilityBundle(
        provider_id="example.renderers",
        provider_kind=CapabilityProviderKind.PLUGIN,
        version=version,
        contributions=(
            CapabilityContribution(
                contribution_id="preview",
                slot="artifact.renderer",
                entrypoint="example:renderer",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_renderer_failure_falls_back_to_system_renderer(
    tmp_path: Path,
) -> None:
    registry = await _system_registry(tmp_path)
    await registry.activate_bundle(
        _plugin_bundle("1.0.0"),
        lambda _: _PluginRenderer("1", fail=True),
    )
    artifact = _artifact(b"source")

    rendered, _ = await ArtifactRenderService(registry).render(
        artifact,
        b"source",
        disposition=ArtifactRenderDisposition.INLINE,
        filename="result.md",
        max_output_bytes=1024,
    )

    assert rendered.renderer_id == SYSTEM_SAFE_ARTIFACT_RENDERER_ID
    assert rendered.content == b"source"


@pytest.mark.asyncio
async def test_attachment_renderer_cannot_change_source_bytes(
    tmp_path: Path,
) -> None:
    registry = await _system_registry(tmp_path)
    await registry.activate_bundle(
        _plugin_bundle("1.0.0"),
        lambda _: _PluginRenderer("mutated"),
    )
    artifact = _artifact(b"source", "application/octet-stream")

    rendered, _ = await ArtifactRenderService(registry).render(
        artifact,
        b"source",
        disposition=ArtifactRenderDisposition.ATTACHMENT,
        filename="result.bin",
        max_output_bytes=1024,
    )

    assert rendered.renderer_id == SYSTEM_SAFE_ARTIFACT_RENDERER_ID
    assert rendered.content == b"source"
    assert rendered.media_type == "application/octet-stream"


@pytest.mark.asyncio
async def test_hot_replacement_does_not_change_inflight_render(
    tmp_path: Path,
) -> None:
    registry = await _system_registry(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()
    await registry.activate_bundle(
        _plugin_bundle("1.0.0"),
        lambda _: _PluginRenderer(
            "1",
            entered=entered,
            release=release,
        ),
    )
    artifact = _artifact(b"source", "application/x-example")
    service = ArtifactRenderService(registry)
    old_render = asyncio.create_task(
        service.render(
            artifact,
            b"source",
            disposition=ArtifactRenderDisposition.INLINE,
            filename="result.example",
            max_output_bytes=1024,
        ),
    )
    await entered.wait()

    await registry.activate_bundle(
        _plugin_bundle("2.0.0"),
        lambda _: _PluginRenderer("2"),
    )
    release.set()
    old_result, old_generation = await old_render
    new_result, new_generation = await service.render(
        artifact,
        b"source",
        disposition=ArtifactRenderDisposition.INLINE,
        filename="result.example",
        max_output_bytes=1024,
    )

    assert old_result.content == b"plugin-1"
    assert new_result.content == b"plugin-2"
    assert new_generation > old_generation


@pytest.mark.asyncio
async def test_unsafe_plugin_output_fails_closed_without_fallback(
    tmp_path: Path,
) -> None:
    registry = await _system_registry(tmp_path)

    class UnsafeRenderer(_PluginRenderer):
        async def render(self, request):
            return ArtifactRenderResult(
                renderer_id=self.renderer_id,
                content=b"<script>bad()</script>",
                media_type="text/html",
                filename=request.filename,
                disposition=request.disposition,
                source_content_hash=request.artifact.content_hash,
            )

    await registry.activate_bundle(
        _plugin_bundle("1.0.0"),
        lambda _: UnsafeRenderer("1"),
    )
    artifact = _artifact(b"source", "application/x-example")

    with pytest.raises(ArtifactRendererUnavailableError) as caught:
        await ArtifactRenderService(registry).render(
            artifact,
            b"source",
            disposition=ArtifactRenderDisposition.INLINE,
            filename="result.example",
            max_output_bytes=1024,
        )

    assert caught.value.failures == ("example.renderers.preview",)
