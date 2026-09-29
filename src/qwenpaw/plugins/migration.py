# -*- coding: utf-8 -*-
"""Safe migration planning for legacy plugin registration APIs."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .architecture import PluginManifest, PluginMigrationDiagnostic


def _unique_contribution_id(
    target_slot: str,
    existing_ids: set[str],
) -> str:
    """Return a deterministic contribution ID that does not collide."""
    base = f"legacy-{target_slot.replace('.', '-')}"
    candidate = base
    suffix = 2
    while candidate in existing_ids:
        candidate = f"{base}-{suffix}"
        suffix += 1
    existing_ids.add(candidate)
    return candidate


def build_plugin_migration_plan(
    manifest: PluginManifest,
    diagnostics: Sequence[PluginMigrationDiagnostic],
) -> dict[str, Any]:
    """Build a non-mutating v2 Contribution migration plan.

    Legacy registration callables are not equivalent to provider factories.
    The planner therefore creates a deterministic manifest patch while
    keeping factory entrypoints explicit placeholders. It never rewrites a
    third-party plugin or claims that a placeholder is installable.
    """
    existing_slots = {item.slot for item in manifest.contributions}
    existing_ids = {item.contribution_id for item in manifest.contributions}
    planned_slots: set[str] = set()
    additions: list[dict[str, str]] = []
    actions: list[dict[str, str]] = []

    for diagnostic in diagnostics:
        if diagnostic.target_slot in existing_slots:
            actions.append(
                {
                    "api_name": diagnostic.api_name,
                    "target_slot": diagnostic.target_slot,
                    "state": "remove_legacy_registration",
                    "recovery": (
                        "The target Contribution is already declared. "
                        "Move the legacy behavior into that provider and "
                        "remove the registration call."
                    ),
                },
            )
            continue

        if diagnostic.target_slot in planned_slots:
            continue
        planned_slots.add(diagnostic.target_slot)
        contribution_id = _unique_contribution_id(
            diagnostic.target_slot,
            existing_ids,
        )
        addition = {
            "id": contribution_id,
            "slot": diagnostic.target_slot,
            "entrypoint": "<module>:<provider_factory>",
        }
        additions.append(addition)
        actions.append(
            {
                "api_name": diagnostic.api_name,
                "target_slot": diagnostic.target_slot,
                "state": "provider_scaffold_required",
                "recovery": diagnostic.recovery,
            },
        )

    if not diagnostics:
        status = "no_migration_needed"
    else:
        status = "manual_changes_required"

    blockers = []
    if additions:
        blockers.append(
            "Replace every provider factory placeholder with a module-level "
            "factory implementing the public Slot protocol.",
        )
    if actions:
        blockers.append(
            "Remove legacy registration calls only after the v2 provider "
            "passes validation and hot-activation tests.",
        )

    return {
        "plugin_id": manifest.id,
        "status": status,
        "target_schema_version": "qwenpaw.plugin.v2",
        "manifest_patch": {
            "schema_version": "qwenpaw.plugin.v2",
            "contributions_to_add": additions,
        },
        "actions": actions,
        "blockers": blockers,
        "safe_to_apply": False,
    }


__all__ = ["build_plugin_migration_plan"]
