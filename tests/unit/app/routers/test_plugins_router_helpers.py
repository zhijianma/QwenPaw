# -*- coding: utf-8 -*-
"""Tests for plugin router helpers: safe zip extraction, plugin dir
discovery, disk-based plugin listing, and plugin UI file serving.

These cover the path-traversal guards and the pre-loader fallback
paths that previously had no test coverage.
"""

# pylint: disable=protected-access,redefined-outer-name,unused-argument,use-implicit-booleaness-not-comparison  # noqa: E501
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from qwenpaw.app.routers.plugins import (
    InstallPluginRequest,
    _find_plugin_dir,
    _list_plugins_from_disk,
    _safe_extract_zip,
    get_capability_promotion_evidence_artifact,
    install_plugin,
    list_capability_promotion_evidence,
    list_capability_promotions,
    list_capability_releases,
    list_plugins,
    router as plugins_router,
    uninstall_plugin,
)
from qwenpaw.app.routers.frontend_plugin import list_frontend_plugins
from qwenpaw.capabilities import GenerationRegistry
from qwenpaw.capabilities.promotions import (
    FilesystemCapabilityPromotionEvidenceStore,
    FilesystemCapabilityPromotionJournal,
)
from qwenpaw.kernel.models import (
    CapabilityBundle,
    CapabilityContribution,
    CapabilityProviderKind,
)
from qwenpaw.plugins.architecture import (
    PluginManifest,
    PluginMigrationDiagnostic,
    PluginRecord,
)
from qwenpaw.plugins.contributions import (
    ContributionDiagnostic,
    ContributionValidationError,
)
from qwenpaw.plugins.loader import (
    PluginDeactivationAuthorizationRequired,
)


def _zip_bytes(entries: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buffer.getvalue()


class _ReleaseFactory:
    def __init__(self, factory_id: str) -> None:
        self.factory_id = factory_id

    async def build(self, context, app_services):
        return context, app_services


# ---------------------------------------------------------------------------
# _safe_extract_zip
# ---------------------------------------------------------------------------


class TestSafeExtractZip:
    def test_normal_members_extract(self, tmp_path):
        dest = tmp_path / "out"
        dest.mkdir()
        data = _zip_bytes({"a.txt": "A", "sub/b.txt": "B"})
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            _safe_extract_zip(zf, dest)
        assert (dest / "a.txt").read_text(encoding="utf-8") == "A"
        assert (dest / "sub" / "b.txt").read_text(encoding="utf-8") == "B"

    def test_zip_slip_member_rejected(self, tmp_path):
        dest = tmp_path / "out"
        dest.mkdir()
        data = _zip_bytes({"../escape.txt": "evil"})
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            with pytest.raises(ValueError, match="Zip Slip"):
                _safe_extract_zip(zf, dest)
        assert not (tmp_path / "escape.txt").exists()

    def test_absolute_member_rejected(self, tmp_path):
        dest = tmp_path / "out"
        dest.mkdir()
        data = _zip_bytes({"/etc/passwd": "evil"})
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            with pytest.raises(ValueError, match="Zip Slip"):
                _safe_extract_zip(zf, dest)


# ---------------------------------------------------------------------------
# _find_plugin_dir
# ---------------------------------------------------------------------------


class TestFindPluginDir:
    def test_plugin_json_at_base(self, tmp_path):
        (tmp_path / "plugin.json").write_text("{}", encoding="utf-8")
        assert _find_plugin_dir(tmp_path) == tmp_path

    def test_plugin_json_in_single_subdir(self, tmp_path):
        inner = tmp_path / "my-plugin"
        inner.mkdir()
        (inner / "plugin.json").write_text("{}", encoding="utf-8")
        assert _find_plugin_dir(tmp_path) == inner

    def test_no_plugin_json_raises(self, tmp_path):
        (tmp_path / "other.txt").write_text("x", encoding="utf-8")
        with pytest.raises(ValueError):
            _find_plugin_dir(tmp_path)


# ---------------------------------------------------------------------------
# _list_plugins_from_disk
# ---------------------------------------------------------------------------


def _write_plugin(plugin_dir: Path, manifest: dict):
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )


class TestListPluginsFromDisk:
    def test_missing_plugins_dir_returns_empty(
        self,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.setattr(
            "qwenpaw.config.utils.get_plugins_dir",
            lambda: tmp_path / "nonexistent",
        )
        assert _list_plugins_from_disk() == []

    def test_lists_valid_plugins(self, tmp_path, monkeypatch):
        plugins_dir = tmp_path / "plugins"
        monkeypatch.setattr(
            "qwenpaw.config.utils.get_plugins_dir",
            lambda: plugins_dir,
        )
        _write_plugin(
            plugins_dir / "my-plugin",
            {
                "id": "my-plugin",
                "name": "My Plugin",
                "version": "1.2.3",
                "description": "desc",
                "author": "me",
            },
        )
        result = _list_plugins_from_disk()
        assert len(result) == 1
        entry = result[0]
        assert entry["id"] == "my-plugin"
        assert entry["version"] == "1.2.3"
        assert entry["enabled"] is True
        assert entry["loaded"] is False
        assert entry["schema_version"] == "qwenpaw.plugin.v1"
        assert not entry["ui_contributions"]
        assert entry["migration_plan"] is None

    def test_skips_disabled_and_hidden_dirs(self, tmp_path, monkeypatch):
        plugins_dir = tmp_path / "plugins"
        monkeypatch.setattr(
            "qwenpaw.config.utils.get_plugins_dir",
            lambda: plugins_dir,
        )
        _write_plugin(plugins_dir / "good", {"id": "good", "version": "1.0"})
        _write_plugin(
            plugins_dir / "off.disabled",
            {"id": "off", "version": "1.0"},
        )
        _write_plugin(
            plugins_dir / ".hidden",
            {"id": "hidden", "version": "1.0"},
        )
        result = _list_plugins_from_disk()
        assert [entry["id"] for entry in result] == ["good"]

    def test_skips_dir_without_manifest(self, tmp_path, monkeypatch):
        plugins_dir = tmp_path / "plugins"
        monkeypatch.setattr(
            "qwenpaw.config.utils.get_plugins_dir",
            lambda: plugins_dir,
        )
        (plugins_dir / "no-manifest").mkdir(parents=True)
        assert _list_plugins_from_disk() == []

    def test_malformed_manifest_skipped(self, tmp_path, monkeypatch):
        plugins_dir = tmp_path / "plugins"
        monkeypatch.setattr(
            "qwenpaw.config.utils.get_plugins_dir",
            lambda: plugins_dir,
        )
        bad = plugins_dir / "bad"
        bad.mkdir(parents=True)
        (bad / "plugin.json").write_text("{not json", encoding="utf-8")
        assert _list_plugins_from_disk() == []

    def test_non_dir_entries_ignored(self, tmp_path, monkeypatch):
        plugins_dir = tmp_path / "plugins"
        monkeypatch.setattr(
            "qwenpaw.config.utils.get_plugins_dir",
            lambda: plugins_dir,
        )
        plugins_dir.mkdir()
        (plugins_dir / "stray.txt").write_text("x", encoding="utf-8")
        assert _list_plugins_from_disk() == []


# ---------------------------------------------------------------------------
# serve_plugin_ui_file (via TestClient)
# ---------------------------------------------------------------------------


@pytest.fixture
def ui_client(tmp_path, monkeypatch):
    """Client with no plugin loader (disk fallback path)."""
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "ui-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps({"id": "ui-plugin"}),
        encoding="utf-8",
    )
    (plugin_dir / "ui").mkdir()
    (plugin_dir / "ui" / "app.js").write_text("console.log(1)")
    (plugin_dir / "ui" / "style.css").write_text("body{}")
    (plugin_dir / "ui" / "chunk-abcdefgh.js").write_text("// hashed")

    monkeypatch.setattr(
        "qwenpaw.config.utils.get_plugins_dir",
        lambda: plugins_dir,
    )
    app = FastAPI()
    app.state.plugin_loader = None  # force disk fallback
    app.include_router(plugins_router, prefix="/api")
    return TestClient(app)


class TestServePluginUiFile:
    def test_serves_js_with_javascript_type(self, ui_client):
        response = ui_client.get(
            "/api/plugins/ui-plugin/files/ui/app.js",
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith(
            "application/javascript",
        )
        assert response.headers["cache-control"] == "no-cache"

    def test_hashed_asset_gets_immutable_cache(self, ui_client):
        response = ui_client.get(
            "/api/plugins/ui-plugin/files/ui/chunk-abcdefgh.js",
        )
        assert response.status_code == 200
        assert "immutable" in response.headers["cache-control"]

    def test_serves_css(self, ui_client):
        response = ui_client.get(
            "/api/plugins/ui-plugin/files/ui/style.css",
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/css")

    def test_missing_plugin_returns_404(self, ui_client):
        response = ui_client.get("/api/plugins/ghost/files/ui/app.js")
        assert response.status_code == 404

    def test_path_traversal_denied(self, ui_client, tmp_path, monkeypatch):
        """Call the handler directly: the HTTP layer normalizes '..'
        segments before routing, so traversal is probed at the function."""
        import asyncio

        from qwenpaw.app.routers.plugins import serve_plugin_ui_file

        plugins_dir = tmp_path / "plugins"
        monkeypatch.setattr(
            "qwenpaw.config.utils.get_plugins_dir",
            lambda: plugins_dir,
        )
        request = SimpleNamespace(app=ui_client.app)
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                serve_plugin_ui_file(
                    "ui-plugin",
                    "../../other.txt",
                    request,
                ),
            )
        assert exc_info.value.status_code in (403, 404)

    def test_missing_file_returns_404(self, ui_client):
        response = ui_client.get(
            "/api/plugins/ui-plugin/files/ui/ghost.js",
        )
        assert response.status_code == 404

    def test_loader_mode_unknown_plugin_404(self, tmp_path):
        app = FastAPI()
        loader = SimpleNamespace()
        loader.get_loaded_plugin = lambda plugin_id: None
        app.state.plugin_loader = loader
        app.include_router(plugins_router, prefix="/api")
        client = TestClient(app)
        response = client.get("/api/plugins/ghost/files/ui/app.js")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_install_returns_structured_contribution_diagnostics():
    diagnostic = ContributionDiagnostic(
        field="contributions[0].config_schema",
        code="invalid_config_schema",
        message="The configuration schema is invalid.",
        recovery="Provide a valid JSON Schema object.",
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(plugin_loader=object()),
        ),
    )
    error = ContributionValidationError((diagnostic,))

    with patch(
        "qwenpaw.app.routers.plugins.install_plugin_source",
        new=AsyncMock(side_effect=error),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await install_plugin(
                InstallPluginRequest(source="/tmp/example"),
                request,
            )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == error.response_detail()


@pytest.mark.asyncio
async def test_uninstall_returns_exact_release_challenge():
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(plugin_loader=object()),
        ),
    )
    challenge = PluginDeactivationAuthorizationRequired(
        "capability-plugin",
        "sha256:release-1",
        ("capability-plugin.runner",),
    )

    with patch(
        "qwenpaw.app.routers.plugins.uninstall_plugin_source",
        new=AsyncMock(side_effect=challenge),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await uninstall_plugin(
                "capability-plugin",
                request,
                None,
            )

    assert exc_info.value.status_code == 428
    assert exc_info.value.detail == challenge.response_detail()


@pytest.mark.asyncio
async def test_uninstall_forwards_confirmed_release_hash():
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(plugin_loader=object()),
        ),
    )

    with patch(
        "qwenpaw.app.routers.plugins.uninstall_plugin_source",
        new=AsyncMock(),
    ) as uninstall_source:
        await uninstall_plugin(
            "capability-plugin",
            request,
            "sha256:release-1",
        )

    uninstall_source.assert_awaited_once_with(
        "capability-plugin",
        app=request.app,
        confirmed_release_hash="sha256:release-1",
    )


@pytest.mark.asyncio
async def test_list_plugins_exposes_migration_diagnostics(tmp_path):
    diagnostic = PluginMigrationDiagnostic(
        api_name="register_tool",
        target_slot="tool.provider",
        message="Legacy tool registration detected.",
        recovery="Declare a tool.provider contribution.",
        manifest_fragment={
            "schema_version": "qwenpaw.plugin.v2",
            "contributions": [],
        },
    )
    record = PluginRecord(
        manifest=PluginManifest.from_dict(
            {
                "id": "legacy-tool",
                "version": "1.0.0",
            },
        ),
        source_path=tmp_path,
        enabled=True,
        migration_diagnostics=[diagnostic],
    )
    loader = SimpleNamespace(
        get_all_loaded_plugins=lambda: {"legacy-tool": record},
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(plugin_loader=loader),
        ),
    )

    result = await list_plugins(request)

    assert result[0]["migration_diagnostics"] == [
        diagnostic.model_dump(mode="json"),
    ]
    assert result[0]["schema_version"] == "qwenpaw.plugin.v1"
    assert not result[0]["ui_contributions"]
    assert result[0]["migration_plan"] == {
        "plugin_id": "legacy-tool",
        "status": "manual_changes_required",
        "target_schema_version": "qwenpaw.plugin.v2",
        "manifest_patch": {
            "schema_version": "qwenpaw.plugin.v2",
            "contributions_to_add": [
                {
                    "id": "legacy-tool-provider",
                    "slot": "tool.provider",
                    "entrypoint": "<module>:<provider_factory>",
                },
            ],
        },
        "actions": [
            {
                "api_name": "register_tool",
                "target_slot": "tool.provider",
                "state": "provider_scaffold_required",
                "recovery": "Declare a tool.provider contribution.",
            },
        ],
        "blockers": [
            "Replace every provider factory placeholder with a module-level "
            "factory implementing the public Slot protocol.",
            "Remove legacy registration calls only after the v2 provider "
            "passes validation and hot-activation tests.",
        ],
        "safe_to_apply": False,
    }


@pytest.mark.asyncio
async def test_frontend_plugin_list_exposes_ui_contribution_contract(
    tmp_path,
):
    record = PluginRecord(
        manifest=PluginManifest.from_dict(
            {
                "schema_version": "qwenpaw.plugin.v2",
                "id": "task-insights",
                "version": "1.0.0",
                "contributions": [
                    {
                        "id": "inspector",
                        "slot": "ui.task.inspector",
                        "entrypoint": "frontend/inspector.js",
                    },
                ],
            },
        ),
        source_path=tmp_path,
        enabled=True,
    )
    loader = SimpleNamespace(
        get_all_loaded_plugins=lambda: {"task-insights": record},
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(plugin_loader=loader),
        ),
    )

    result = await list_frontend_plugins(request)

    assert result[0]["schema_version"] == "qwenpaw.plugin.v2"
    assert result[0]["frontend_entry"] == "frontend/inspector.js"
    assert result[0]["ui_contributions"] == [
        {
            "id": "inspector",
            "slot": "ui.task.inspector",
            "entrypoint": "frontend/inspector.js",
        },
    ]


@pytest.mark.asyncio
async def test_list_capability_releases_is_stable_and_content_safe():
    registry = GenerationRegistry()
    for provider_id in ("provider.zeta", "provider.alpha"):
        await registry.activate_bundle(
            CapabilityBundle(
                provider_id=provider_id,
                provider_kind=CapabilityProviderKind.SYSTEM,
                version="1.0.0",
                contributions=(
                    CapabilityContribution(
                        contribution_id="factory",
                        slot="agent.factory",
                        entrypoint="tests:factory",
                    ),
                ),
            ),
            lambda _declaration, owner=provider_id: _ReleaseFactory(
                f"{owner}.factory",
            ),
        )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                plugin_loader=SimpleNamespace(
                    capability_registry=registry,
                ),
            ),
        ),
    )

    result = await list_capability_releases(request)

    assert result["registry_generation"] == 3
    assert result["registry_epoch_id"] == str(registry.registry_epoch_id)
    assert result["channel"] == "stable"
    assert [item["provider_id"] for item in result["items"]] == [
        "provider.alpha",
        "provider.zeta",
    ]
    assert all("implementation" not in item for item in result["items"])
    assert all(
        item["release_hash"].startswith("sha256:")
        for item in result["items"]
    )


@pytest.mark.asyncio
async def test_list_capability_releases_waits_for_registry():
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(plugin_loader=None),
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await list_capability_releases(request)

    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_list_capability_releases_uses_os_registry_during_startup():
    registry = GenerationRegistry()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                plugin_loader=None,
                workspace_registry=SimpleNamespace(
                    capability_registry=registry,
                ),
            ),
        ),
    )

    result = await list_capability_releases(request)

    assert result == {
        "registry_epoch_id": str(registry.registry_epoch_id),
        "registry_generation": 1,
        "channel": "stable",
        "items": [],
    }


@pytest.mark.asyncio
async def test_list_capability_promotions_reads_durable_wal(tmp_path):
    journal = FilesystemCapabilityPromotionJournal(tmp_path)
    evidence_store = FilesystemCapabilityPromotionEvidenceStore(tmp_path)
    registry = GenerationRegistry(
        promotion_journal=journal,
        promotion_evidence_store=evidence_store,
    )
    provider_id = "provider.audit"
    await registry.activate_bundle(
        CapabilityBundle(
            provider_id=provider_id,
            provider_kind=CapabilityProviderKind.SYSTEM,
            version="1.0.0",
            contributions=(
                CapabilityContribution(
                    contribution_id="factory",
                    slot="agent.factory",
                    entrypoint="tests:factory",
                ),
            ),
        ),
        lambda _declaration: _ReleaseFactory(
            f"{provider_id}.factory",
        ),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                plugin_loader=SimpleNamespace(
                    capability_registry=registry,
                ),
            ),
        ),
    )

    result = await list_capability_promotions(
        request,
        provider_id=provider_id,
        limit=10,
    )

    assert result["registry_generation"] == 2
    assert result["registry_epoch_id"] == str(registry.registry_epoch_id)
    assert [item["phase"] for item in result["items"]] == [
        "committed",
        "prepared",
    ]
    assert all(
        item["candidate"]["provider_id"] == provider_id
        for item in result["items"]
    )
    candidate_id = result["items"][0]["candidate"]["candidate_id"]
    evidence_result = await list_capability_promotion_evidence(
        request,
        candidate_id=UUID(candidate_id),
        limit=10,
    )
    [bundle] = evidence_result["items"]
    [artifact] = evidence_result["artifacts"]
    assert bundle["candidate_id"] == candidate_id
    assert {item["check_id"] for item in bundle["evidence"]} == {
        "contract.schema",
        "contract.implementation",
        "contract.health",
    }
    assert artifact["kind"] == "capability.promotion-evidence"
    assert artifact["metadata"]["candidate_id"] == candidate_id
    assert artifact["content_hash"].startswith("sha256:")
    response = await get_capability_promotion_evidence_artifact(
        request,
        UUID(bundle["bundle_id"]),
    )
    assert response.status_code == 200
    assert len(response.body) == artifact["size_bytes"]
    assert response.headers["etag"] == f'"{artifact["content_hash"]}"'
    assert json.loads(response.body)["bundle_id"] == bundle["bundle_id"]


@pytest.mark.asyncio
async def test_missing_promotion_evidence_artifact_returns_404():
    registry = GenerationRegistry()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                plugin_loader=SimpleNamespace(
                    capability_registry=registry,
                ),
            ),
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await get_capability_promotion_evidence_artifact(
            request,
            UUID("00000000-0000-0000-0000-000000000001"),
        )

    assert exc_info.value.status_code == 404
