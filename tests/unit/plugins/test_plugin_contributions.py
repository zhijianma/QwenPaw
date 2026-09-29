# -*- coding: utf-8 -*-
"""Contract tests for multi-slot plugin manifests."""

import pytest

from qwenpaw.kernel import DeliveryStatus
from qwenpaw.kernel.delivery import DeliveryReceipt
from qwenpaw.kernel.slots import SLOT_CONTRACTS
from qwenpaw.capabilities.contracts import PUBLIC_IMPLEMENTATION_CONTRACTS
from qwenpaw.plugins.architecture import PluginManifest
from qwenpaw.plugins.contributions import (
    ContributionValidationError,
    ContributionImplementationError,
    validate_contribution_implementation,
    validate_contributions,
)
from qwenpaw.scheduling import SQLiteSchedulerStore


def test_v2_manifest_accepts_multiple_typed_slots() -> None:
    manifest = PluginManifest.from_dict(
        {
            "schema_version": "qwenpaw.plugin.v2",
            "id": "task-insights",
            "version": "1.0.0",
            "contributions": [
                {
                    "id": "local-runner",
                    "slot": "runner",
                    "entrypoint": "task_insights.runner:create",
                },
                {
                    "id": "task-panel",
                    "slot": "ui.task.inspector",
                    "entrypoint": "frontend/index.js",
                },
            ],
        },
    )

    contributions = validate_contributions(manifest)

    assert [item.contribution_id for item in contributions] == [
        "local-runner",
        "task-panel",
    ]


def test_v1_manifest_remains_valid_without_contributions() -> None:
    manifest = PluginManifest.from_dict(
        {"id": "legacy-tool", "version": "1.0.0", "type": "tool"},
    )

    assert manifest.schema_version == "qwenpaw.plugin.v1"
    assert not validate_contributions(manifest)


def test_manifest_reports_exact_unsupported_slot_and_recovery() -> None:
    manifest = PluginManifest.from_dict(
        {
            "schema_version": "qwenpaw.plugin.v2",
            "id": "invalid-plugin",
            "version": "1.0.0",
            "contributions": [
                {
                    "id": "unknown",
                    "slot": "ui.unknown",
                    "entrypoint": "frontend/index.js",
                },
            ],
        },
    )

    with pytest.raises(ContributionValidationError) as raised:
        validate_contributions(manifest)

    diagnostic = raised.value.diagnostics[0]
    assert diagnostic.field == "contributions[0].slot"
    assert diagnostic.code == "unsupported_slot"
    assert "ui.task.inspector" in diagnostic.recovery


def test_manifest_rejects_system_agent_factory_slot() -> None:
    manifest = PluginManifest.from_dict(
        {
            "schema_version": "qwenpaw.plugin.v2",
            "id": "unsafe-agent-factory",
            "version": "1.0.0",
            "contributions": [
                {
                    "id": "factory",
                    "slot": "agent.factory",
                    "entrypoint": "unsafe_factory:create",
                },
            ],
        },
    )

    with pytest.raises(ContributionValidationError) as raised:
        validate_contributions(manifest)

    diagnostic = raised.value.diagnostics[0]
    assert diagnostic.field == "contributions[0].slot"
    assert diagnostic.code == "system_slot"
    assert "reserved for the QwenPaw runtime core" in diagnostic.message
    assert "agent.mode.provider" in diagnostic.recovery


def test_backend_entrypoint_requires_module_and_attribute() -> None:
    manifest = PluginManifest.from_dict(
        {
            "schema_version": "qwenpaw.plugin.v2",
            "id": "invalid-runner",
            "version": "1.0.0",
            "contributions": [
                {
                    "id": "runner",
                    "slot": "runner",
                    "entrypoint": "runner.py",
                },
            ],
        },
    )

    with pytest.raises(ContributionValidationError) as raised:
        validate_contributions(manifest)

    assert raised.value.diagnostics[0].field.endswith("entrypoint")


@pytest.mark.parametrize(
    "entrypoint",
    ("frontend", "../outside.js", "/absolute.js", "frontend\\index.js"),
)
def test_ui_entrypoint_requires_safe_relative_javascript_path(
    entrypoint: str,
) -> None:
    manifest = PluginManifest.from_dict(
        {
            "schema_version": "qwenpaw.plugin.v2",
            "id": "invalid-ui",
            "version": "1.0.0",
            "contributions": [
                {
                    "id": "inspector",
                    "slot": "ui.task.inspector",
                    "entrypoint": entrypoint,
                },
            ],
        },
    )

    with pytest.raises(ContributionValidationError) as raised:
        validate_contributions(manifest)

    diagnostic = raised.value.diagnostics[0]
    assert diagnostic.code == "invalid_ui_entrypoint"
    assert "frontend/index.js" in diagnostic.recovery


def test_manifest_rejects_invalid_provider_config_schema() -> None:
    manifest = PluginManifest.from_dict(
        {
            "schema_version": "qwenpaw.plugin.v2",
            "id": "invalid-config",
            "version": "1.0.0",
            "contributions": [
                {
                    "id": "provider",
                    "slot": "tool.provider",
                    "entrypoint": "invalid_config:create",
                    "config_schema": {"type": "not-a-json-schema-type"},
                },
            ],
        },
    )

    with pytest.raises(ContributionValidationError) as raised:
        validate_contributions(manifest)

    diagnostic = raised.value.diagnostics[0]
    assert diagnostic.field == "contributions[0].config_schema"
    assert diagnostic.code == "invalid_config_schema"
    assert "JSON Schema" in diagnostic.recovery
    assert raised.value.response_detail() == {
        "code": "invalid_plugin_contributions",
        "diagnostics": [
            {
                "field": "contributions[0].config_schema",
                "code": "invalid_config_schema",
                "message": (
                    "Contribution config_schema is not a valid JSON Schema"
                ),
                "recovery": (
                    "Use a valid JSON Schema object for the provider "
                    "configuration"
                ),
            },
        ],
    }


def test_every_public_backend_slot_has_an_activation_contract() -> None:
    public_backend_slots = {
        slot
        for slot, contract in SLOT_CONTRACTS.items()
        if contract.stability == "public" and not slot.startswith("ui.")
    }

    assert set(PUBLIC_IMPLEMENTATION_CONTRACTS) == public_backend_slots


def test_scheduler_contribution_requires_public_port(tmp_path) -> None:
    declaration = PluginManifest.from_dict(
        {
            "schema_version": "qwenpaw.plugin.v2",
            "id": "scheduler-plugin",
            "version": "1.0.0",
            "contributions": [
                {
                    "id": "durable",
                    "slot": "scheduler",
                    "entrypoint": "scheduler_plugin:create",
                },
            ],
        },
    ).contributions[0]

    with pytest.raises(ContributionImplementationError, match="contract"):
        validate_contribution_implementation(
            "scheduler-plugin",
            declaration,
            object(),
        )

    validate_contribution_implementation(
        "scheduler-plugin",
        declaration,
        SQLiteSchedulerStore(tmp_path / "scheduler.db"),
    )


def test_delivery_contribution_requires_public_adapter_identity() -> None:
    declaration = PluginManifest.from_dict(
        {
            "schema_version": "qwenpaw.plugin.v2",
            "id": "delivery-plugin",
            "version": "1.0.0",
            "contributions": [
                {
                    "id": "channel",
                    "slot": "delivery.adapter",
                    "entrypoint": "delivery_plugin:create",
                },
            ],
        },
    ).contributions[0]

    class Adapter:
        adapter_id = "delivery-plugin.channel"

        def supports(self, _request):
            return True

        async def deliver(self, request, *, attempt):
            return DeliveryReceipt(
                delivery_id=request.delivery_id,
                adapter_id=self.adapter_id,
                status=DeliveryStatus.DELIVERED,
                attempt=attempt,
            )

    with pytest.raises(ContributionImplementationError, match="contract"):
        validate_contribution_implementation(
            "delivery-plugin",
            declaration,
            object(),
        )

    validate_contribution_implementation(
        "delivery-plugin",
        declaration,
        Adapter(),
    )
