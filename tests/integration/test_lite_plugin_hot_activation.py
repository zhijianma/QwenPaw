# -*- coding: utf-8 -*-
"""Focused hot-activation test through the production plugin loader."""

import json
import sys
from pathlib import Path

import pytest

from qwenpaw.plugins.architecture import PluginManifest
from qwenpaw.plugins.generations import ActivationError
from qwenpaw.plugins.contributions import ContributionValidationError
from qwenpaw.plugins.loader import PluginLoader
from qwenpaw.capabilities import GenerationRegistry
from qwenpaw.capabilities.promotions import (
    FilesystemCapabilityPromotionJournal,
    LiteCapabilityPromotionScenarioRunner,
)
from qwenpaw.kernel.ports import (
    ArtifactRenderer,
    DeliveryAdapter,
    ProposalSensor,
    RuntimeStrategy,
    TaskPlanner,
    TaskRunner,
)


def _write_engine_plugin(
    source: Path,
    *,
    version: str,
    marker: str,
    healthy: bool = True,
) -> None:
    source.mkdir(parents=True)
    (source / "plugin.json").write_text(
        json.dumps(
            {
                "schema_version": "qwenpaw.plugin.v2",
                "id": "atomic-engine",
                "name": "Atomic Engine",
                "version": version,
                "restart_policy": "hot",
                "contributions": [
                    {
                        "id": "engine",
                        "slot": "engine",
                        "entrypoint": "provider:create",
                    },
                ],
            },
        ),
        encoding="utf-8",
    )
    (source / "provider.py").write_text(
        "class Engine:\n"
        f"    marker = {marker!r}\n"
        "    def health_check(self):\n"
        f"        return {healthy!r}\n"
        "def create():\n"
        "    return Engine()\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_contextual_slots_load_and_unload_without_restart(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[2]
    source = root / "examples" / "plugins" / "task-insights"
    registry = GenerationRegistry(
        promotion_journal=FilesystemCapabilityPromotionJournal(tmp_path),
        promotion_scenario_runner=LiteCapabilityPromotionScenarioRunner(),
    )
    loader = PluginLoader(
        [source.parent],
        capability_registry=registry,
    )
    manifest = PluginManifest.from_dict(
        json.loads((source / "plugin.json").read_text(encoding="utf-8")),
    )

    record = await loader.load_plugin(manifest, source)
    lease = await loader.capability_registry.pin()

    assert record.enabled
    assert lease.generation == 2
    assert lease.resolve("task-insights.insight-planner").slot == "planner"
    assert lease.resolve("task-insights.insight-strategy").slot == "strategy"
    assert lease.resolve("task-insights.summary-runner").slot == "runner"
    assert lease.resolve("task-insights.review-sensor").slot == "sensor"
    assert isinstance(
        lease.implementation("task-insights.insight-planner"),
        TaskPlanner,
    )
    assert isinstance(
        lease.implementation("task-insights.insight-strategy"),
        RuntimeStrategy,
    )
    assert isinstance(
        lease.implementation("task-insights.summary-runner"),
        TaskRunner,
    )
    assert isinstance(
        lease.implementation("task-insights.review-sensor"),
        ProposalSensor,
    )
    assert isinstance(
        lease.implementation("task-insights.summary-renderer"),
        ArtifactRenderer,
    )
    assert (
        lease.resolve("task-insights.summary-renderer").slot
        == "artifact.renderer"
    )
    assert lease.resolve("task-insights.toolbar").slot == "ui.task.toolbar"
    assert lease.resolve("task-insights.tab").slot == "ui.task.tab"
    assert lease.resolve("task-insights.inspector").slot == "ui.task.inspector"
    assert (
        lease.resolve("task-insights.artifact-preview").slot
        == "ui.artifact.preview"
    )
    release = registry.stable_release("task-insights")
    assert release is not None
    promotion_events = await registry.promotion_events(
        provider_id="task-insights",
    )
    assert promotion_events
    evidence_bundles = await registry.promotion_evidence(
        promotion_events[-1].candidate.candidate_id,
    )
    assert any(
        evidence.check_id
        == (
            "scenario.artifact-renderer.roundtrip."
            "task-insights.summary-renderer"
        )
        and evidence.outcome.value == "passed"
        for evidence_bundle in evidence_bundles
        for evidence in evidence_bundle.evidence
    )

    await loader.unload_plugin("task-insights")
    current = await loader.capability_registry.pin()
    assert current.generation == 3
    assert current.resolve("task-insights.insight-planner") is None
    assert current.resolve("task-insights.insight-strategy") is None
    assert current.resolve("task-insights.summary-runner") is None
    assert current.resolve("task-insights.review-sensor") is None
    assert current.resolve("task-insights.summary-renderer") is None
    assert lease.resolve("task-insights.summary-runner") is not None
    assert lease.resolve("task-insights.insight-planner") is not None
    assert lease.resolve("task-insights.insight-strategy") is not None
    await lease.close()
    await current.close()


@pytest.mark.asyncio
async def test_delivery_adapter_loads_and_unloads_without_restart() -> None:
    root = Path(__file__).parents[2]
    source = root / "examples" / "plugins" / "delivery-provider"
    loader = PluginLoader([source.parent])
    manifest = PluginManifest.from_dict(
        json.loads((source / "plugin.json").read_text(encoding="utf-8")),
    )

    record = await loader.load_plugin(manifest, source)
    lease = await loader.capability_registry.pin()

    contribution_id = "delivery-provider.local-jsonl"
    assert record.enabled
    assert lease.generation == 2
    assert lease.resolve(contribution_id).slot == "delivery.adapter"
    assert isinstance(
        lease.implementation(contribution_id),
        DeliveryAdapter,
    )

    await loader.unload_plugin("delivery-provider")
    current = await loader.capability_registry.pin()
    assert current.generation == 3
    assert current.resolve(contribution_id) is None
    assert lease.resolve(contribution_id) is not None
    await lease.close()
    await current.close()


@pytest.mark.asyncio
async def test_failed_activation_removes_staged_modules(
    tmp_path: Path,
) -> None:
    source = tmp_path / "rollback-plugin"
    source.mkdir()
    (source / "healthy.py").write_text(
        "class Contribution:\n"
        "    def health_check(self):\n"
        "        return True\n"
        "def create():\n"
        "    return Contribution()\n",
        encoding="utf-8",
    )
    (source / "unhealthy.py").write_text(
        "class Contribution:\n"
        "    def health_check(self):\n"
        "        return False\n"
        "def create():\n"
        "    return Contribution()\n",
        encoding="utf-8",
    )
    manifest = PluginManifest.from_dict(
        {
            "schema_version": "qwenpaw.plugin.v2",
            "id": "rollback-plugin",
            "version": "1.0.0",
            "contributions": [
                {
                    "id": "healthy",
                    "slot": "engine",
                    "entrypoint": "healthy:create",
                },
                {
                    "id": "unhealthy",
                    "slot": "memory",
                    "entrypoint": "unhealthy:create",
                },
            ],
        },
    )
    loader = PluginLoader([tmp_path])

    with pytest.raises(ActivationError, match="health check failed"):
        await loader.load_plugin(manifest, source)

    assert loader.capability_registry.generation == 1
    assert loader.get_loaded_plugin("rollback-plugin") is None
    assert not any(
        name.startswith("plugin_rollback_plugin_contribution_")
        for name in sys.modules
    )


@pytest.mark.asyncio
async def test_force_replace_publishes_one_atomic_generation(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "installed"
    old_source = tmp_path / "source-v1"
    new_source = tmp_path / "source-v2"
    _write_engine_plugin(old_source, version="1.0.0", marker="old")
    _write_engine_plugin(new_source, version="2.0.0", marker="new")
    loader = PluginLoader([install_dir])

    await loader.load_plugin_from_path(old_source, install_dir=install_dir)
    old_lease = await loader.capability_registry.pin()
    updated = await loader.load_plugin_from_path(
        new_source,
        install_dir=install_dir,
        force=True,
    )
    current = await loader.capability_registry.pin()

    assert updated.manifest.version == "2.0.0"
    assert current.generation == 3
    assert old_lease.generation == 2
    assert old_lease.implementation("atomic-engine.engine").marker == "old"
    assert current.implementation("atomic-engine.engine").marker == "new"
    await old_lease.close()
    await current.close()


@pytest.mark.asyncio
async def test_failed_force_replace_restores_runtime_and_files(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "installed"
    old_source = tmp_path / "source-v1"
    bad_source = tmp_path / "source-v2"
    _write_engine_plugin(old_source, version="1.0.0", marker="old")
    _write_engine_plugin(
        bad_source,
        version="2.0.0",
        marker="bad",
        healthy=False,
    )
    loader = PluginLoader([install_dir])
    await loader.load_plugin_from_path(old_source, install_dir=install_dir)

    with pytest.raises(ActivationError, match="health check failed"):
        await loader.load_plugin_from_path(
            bad_source,
            install_dir=install_dir,
            force=True,
        )

    current = await loader.capability_registry.pin()
    record = loader.get_loaded_plugin("atomic-engine")
    installed_manifest = json.loads(
        (install_dir / "atomic-engine" / "plugin.json").read_text(
            encoding="utf-8",
        ),
    )

    assert record is not None
    assert record.manifest.version == "1.0.0"
    assert installed_manifest["version"] == "1.0.0"
    assert current.implementation("atomic-engine.engine").marker == "old"
    await current.close()


@pytest.mark.asyncio
async def test_force_replace_can_atomically_remove_all_contributions(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "installed"
    old_source = tmp_path / "source-v1"
    empty_source = tmp_path / "source-v2"
    _write_engine_plugin(old_source, version="1.0.0", marker="old")
    empty_source.mkdir()
    (empty_source / "plugin.json").write_text(
        json.dumps(
            {
                "schema_version": "qwenpaw.plugin.v2",
                "id": "atomic-engine",
                "name": "Atomic Engine",
                "version": "2.0.0",
                "restart_policy": "hot",
                "entry": {"frontend": "frontend.js"},
                "contributions": [],
            },
        ),
        encoding="utf-8",
    )
    (empty_source / "frontend.js").write_text(
        "export default {};\n",
        encoding="utf-8",
    )
    loader = PluginLoader([install_dir])
    await loader.load_plugin_from_path(old_source, install_dir=install_dir)

    await loader.load_plugin_from_path(
        empty_source,
        install_dir=install_dir,
        force=True,
    )
    current = await loader.capability_registry.pin()

    assert current.generation == 3
    assert current.resolve("atomic-engine.engine") is None
    await current.close()


@pytest.mark.asyncio
async def test_invalid_manifest_never_unloads_current_plugin(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "installed"
    old_source = tmp_path / "source-v1"
    bad_source = tmp_path / "source-v2"
    _write_engine_plugin(old_source, version="1.0.0", marker="old")
    _write_engine_plugin(bad_source, version="2.0.0", marker="bad")
    manifest_path = bad_source / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["contributions"][0]["config_schema"] = {
        "type": "not-a-json-schema-type",
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    loader = PluginLoader([install_dir])
    old_record = await loader.load_plugin_from_path(
        old_source,
        install_dir=install_dir,
    )

    with pytest.raises(ContributionValidationError):
        await loader.load_plugin_from_path(
            bad_source,
            install_dir=install_dir,
            force=True,
        )

    current = await loader.capability_registry.pin()
    installed_manifest = json.loads(
        (install_dir / "atomic-engine" / "plugin.json").read_text(
            encoding="utf-8",
        ),
    )
    assert loader.get_loaded_plugin("atomic-engine") is old_record
    assert current.generation == 2
    assert installed_manifest["version"] == "1.0.0"
    assert current.implementation("atomic-engine.engine").marker == "old"
    await current.close()
