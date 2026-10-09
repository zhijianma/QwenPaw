# -*- coding: utf-8 -*-
"""Shared behavioral contract for system and plugin Artifact Renderers."""

import hashlib
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from qwenpaw.kernel.models import (
    ArtifactRef,
    ArtifactRenderDisposition,
)
from qwenpaw.kernel.ports import ArtifactRenderer
from qwenpaw.plugins.architecture import PluginManifest
from qwenpaw.plugins.generations import GenerationRegistry
from qwenpaw.tasks.renderers import ArtifactRenderService
from qwenpaw.tasks.system_contributions import (
    SYSTEM_CAPABILITY_BUNDLE,
    SYSTEM_SAFE_ARTIFACT_RENDERER_ID,
    system_contribution_factory,
)


def _artifact(content: bytes) -> ArtifactRef:
    digest = hashlib.sha256(content).hexdigest()
    return ArtifactRef(
        kind="task.summary",
        uri=f"qwenpaw-artifact://sha256/{digest}",
        media_type="text/markdown",
        content_hash=f"sha256:{digest}",
        size_bytes=len(content),
    )


async def _system_registry(tmp_path: Path) -> GenerationRegistry:
    workspace = SimpleNamespace(
        agent_id="agent-contract",
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


async def _plugin_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> GenerationRegistry:
    registry = await _system_registry(tmp_path)
    plugin_root = (
        Path(__file__).parents[3] / "examples" / "plugins" / "task-insights"
    )
    monkeypatch.syspath_prepend(str(plugin_root))
    module = importlib.import_module("task_insights.renderer")
    manifest_data = json.loads(
        (plugin_root / "plugin.json").read_text(encoding="utf-8"),
    )
    manifest_data["contributions"] = [
        item
        for item in manifest_data["contributions"]
        if item["slot"] == "artifact.renderer"
    ]
    await registry.activate(
        PluginManifest.from_dict(manifest_data),
        lambda _declaration: module.create_renderer(),
    )
    return registry


@pytest.mark.asyncio
async def test_system_and_plugin_renderers_share_behavioral_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system_registry = await _system_registry(tmp_path / "system")
    plugin_registry = await _plugin_registry(
        tmp_path / "plugin",
        monkeypatch,
    )
    cases = (
        (system_registry, SYSTEM_SAFE_ARTIFACT_RENDERER_ID),
        (plugin_registry, "task-insights.summary-renderer"),
    )
    content = b"# Contract result\n"
    artifact = _artifact(content)

    for registry, renderer_id in cases:
        lease = await registry.pin()
        try:
            descriptor = lease.resolve(renderer_id)
            renderer = lease.implementation(renderer_id)
            assert descriptor is not None
            assert descriptor.slot == "artifact.renderer"
            assert isinstance(renderer, ArtifactRenderer)
            assert await renderer.health_check() is True
        finally:
            await lease.close()

        service = ArtifactRenderService(registry)
        descriptions = await service.describe_previews(
            (artifact,),
            max_output_bytes=1024,
        )
        result, generation = await service.render(
            artifact,
            content,
            disposition=ArtifactRenderDisposition.INLINE,
            filename="contract.md",
            max_output_bytes=1024,
        )

        description = descriptions[artifact.artifact_id]
        assert description.available is True
        assert description.renderer_id == renderer_id
        assert description.registry_generation == generation
        assert result.renderer_id == renderer_id
        assert result.source_content_hash == artifact.content_hash
        assert result.filename == "contract.md"
        assert result.disposition is ArtifactRenderDisposition.INLINE
        assert result.media_type == "text/markdown"
        assert len(result.content) <= 1024


@pytest.mark.asyncio
async def test_plugin_renderer_preserves_system_attachment_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = await _plugin_registry(tmp_path, monkeypatch)
    service = ArtifactRenderService(registry)
    content = b"# Immutable source\n"
    artifact = _artifact(content)

    result, generation = await service.render(
        artifact,
        content,
        disposition=ArtifactRenderDisposition.ATTACHMENT,
        filename="source.md",
        max_output_bytes=1024,
    )

    assert generation == registry.generation
    assert result.renderer_id == SYSTEM_SAFE_ARTIFACT_RENDERER_ID
    assert result.content == content
    assert result.media_type == artifact.media_type
    assert result.source_content_hash == artifact.content_hash
