# -*- coding: utf-8 -*-
"""Tests for safe legacy plugin migration planning."""

from qwenpaw.plugins.architecture import (
    PluginManifest,
    PluginMigrationDiagnostic,
)
from qwenpaw.plugins.migration import build_plugin_migration_plan


def _diagnostic(api_name: str, target_slot: str):
    return PluginMigrationDiagnostic(
        api_name=api_name,
        target_slot=target_slot,
        message="Legacy registration detected.",
        recovery=f"Declare a {target_slot} provider.",
        manifest_fragment={
            "schema_version": "qwenpaw.plugin.v2",
            "contributions": [],
        },
    )


def test_plan_deduplicates_provider_slots_and_never_auto_applies():
    manifest = PluginManifest.from_dict(
        {
            "id": "legacy-runtime",
            "version": "1.0.0",
        },
    )

    plan = build_plugin_migration_plan(
        manifest,
        (
            _diagnostic("register_slash_command", "command.provider"),
            _diagnostic("register_slash_command", "command.provider"),
            _diagnostic("register_runtime_hook", "hook.provider"),
        ),
    )

    assert plan["status"] == "manual_changes_required"
    assert plan["safe_to_apply"] is False
    assert plan["manifest_patch"]["contributions_to_add"] == [
        {
            "id": "legacy-command-provider",
            "slot": "command.provider",
            "entrypoint": "<module>:<provider_factory>",
        },
        {
            "id": "legacy-hook-provider",
            "slot": "hook.provider",
            "entrypoint": "<module>:<provider_factory>",
        },
    ]


def test_plan_does_not_duplicate_an_existing_contribution():
    manifest = PluginManifest.from_dict(
        {
            "id": "mixed-runtime",
            "version": "1.0.0",
            "schema_version": "qwenpaw.plugin.v2",
            "contributions": [
                {
                    "id": "commands",
                    "slot": "command.provider",
                    "entrypoint": "provider:create",
                },
            ],
        },
    )

    plan = build_plugin_migration_plan(
        manifest,
        (_diagnostic("register_slash_command", "command.provider"),),
    )

    assert not plan["manifest_patch"]["contributions_to_add"]
    assert plan["actions"][0]["state"] == "remove_legacy_registration"


def test_plan_reports_clean_plugin_without_claiming_apply_safety():
    manifest = PluginManifest.from_dict(
        {
            "id": "native-runtime",
            "version": "1.0.0",
        },
    )

    plan = build_plugin_migration_plan(manifest, ())

    assert plan["status"] == "no_migration_needed"
    assert not plan["actions"]
    assert plan["safe_to_apply"] is False
