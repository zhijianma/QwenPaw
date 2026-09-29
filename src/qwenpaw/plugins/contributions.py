# -*- coding: utf-8 -*-
"""Validation helpers for typed multi-slot plugin contributions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for

from ..capabilities.contracts import (
    CapabilityImplementationError,
    PUBLIC_IMPLEMENTATION_CONTRACTS,
    validate_capability_implementation,
)
from ..capabilities.registry import ActivationError
from ..kernel.models import PluginContribution
from ..kernel.slots import CONTRIBUTION_SLOTS, SLOT_CONTRACTS
from .architecture import PluginManifest

SYSTEM_ONLY_SLOTS = frozenset(
    slot
    for slot, contract in SLOT_CONTRACTS.items()
    if contract.stability == "system"
)
SUPPORTED_SLOTS = CONTRIBUTION_SLOTS - SYSTEM_ONLY_SLOTS


@dataclass(frozen=True)
class ContributionDiagnostic:
    """Actionable manifest diagnostic safe for CLI and HTTP display."""

    field: str
    code: str
    message: str
    recovery: str


class ContributionValidationError(ValueError):
    """Raised with all actionable contribution diagnostics."""

    def __init__(
        self,
        diagnostics: tuple[ContributionDiagnostic, ...],
    ) -> None:
        self.diagnostics = diagnostics
        super().__init__("; ".join(item.message for item in diagnostics))

    def response_detail(self) -> dict[str, object]:
        """Return a stable, transport-neutral diagnostic projection."""
        return {
            "code": "invalid_plugin_contributions",
            "diagnostics": [
                {
                    "field": item.field,
                    "code": item.code,
                    "message": item.message,
                    "recovery": item.recovery,
                }
                for item in self.diagnostics
            ],
        }


class ContributionImplementationError(ActivationError):
    """Raised before publication when a staged implementation is invalid."""


_PUBLIC_IMPLEMENTATION_CONTRACTS = PUBLIC_IMPLEMENTATION_CONTRACTS


def validate_contributions(
    manifest: PluginManifest,
) -> tuple[PluginContribution, ...]:
    """Validate all slots together and return an immutable declaration."""
    diagnostics = []
    for index, contribution in enumerate(manifest.contributions):
        if contribution.slot not in SUPPORTED_SLOTS:
            system_only = contribution.slot in SYSTEM_ONLY_SLOTS
            diagnostics.append(
                ContributionDiagnostic(
                    field=f"contributions[{index}].slot",
                    code=(
                        "system_slot" if system_only else "unsupported_slot"
                    ),
                    message=(
                        f"Slot '{contribution.slot}' is reserved for the "
                        "QwenPaw runtime core"
                        if system_only
                        else f"Unsupported slot '{contribution.slot}'"
                    ),
                    recovery=(
                        "Use a public extension slot instead: "
                        if system_only
                        else "Choose one of: "
                    )
                    + (f"{', '.join(sorted(SUPPORTED_SLOTS))}"),
                ),
            )
        if contribution.slot.startswith("ui."):
            entrypoint = contribution.entrypoint
            path = PurePosixPath(entrypoint)
            if (
                not entrypoint.endswith(".js")
                or path.is_absolute()
                or ".." in path.parts
                or "\\" in entrypoint
            ):
                diagnostics.append(
                    ContributionDiagnostic(
                        field=f"contributions[{index}].entrypoint",
                        code="invalid_ui_entrypoint",
                        message=(
                            f"UI entrypoint '{entrypoint}' must be a "
                            "plugin-relative JavaScript path"
                        ),
                        recovery=(
                            "Use a relative path such as "
                            "frontend/index.js without '..' segments"
                        ),
                    ),
                )
        elif ":" not in contribution.entrypoint:
            diagnostics.append(
                ContributionDiagnostic(
                    field=f"contributions[{index}].entrypoint",
                    code="invalid_entrypoint",
                    message=(
                        f"Backend entrypoint '{contribution.entrypoint}' "
                        "must use module:attribute syntax"
                    ),
                    recovery="Use a value such as example.runner:create",
                ),
            )
        if contribution.config_schema is not None:
            try:
                validator = validator_for(contribution.config_schema)
                validator.check_schema(contribution.config_schema)
            except SchemaError:
                diagnostics.append(
                    ContributionDiagnostic(
                        field=f"contributions[{index}].config_schema",
                        code="invalid_config_schema",
                        message=(
                            "Contribution config_schema is not a valid "
                            "JSON Schema"
                        ),
                        recovery=(
                            "Use a valid JSON Schema object for the "
                            "provider configuration"
                        ),
                    ),
                )
    if diagnostics:
        raise ContributionValidationError(tuple(diagnostics))
    return tuple(manifest.contributions)


def validate_contribution_implementation(
    provider_id: str,
    declaration: PluginContribution,
    implementation: object,
) -> None:
    """Validate one staged plugin implementation before publication."""
    try:
        validate_capability_implementation(
            provider_id,
            declaration,
            implementation,
        )
    except CapabilityImplementationError as exc:
        raise ContributionImplementationError(str(exc)) from exc


__all__ = [
    "ContributionDiagnostic",
    "ContributionImplementationError",
    "ContributionValidationError",
    "SUPPORTED_SLOTS",
    "SYSTEM_ONLY_SLOTS",
    "validate_contribution_implementation",
    "validate_contributions",
]
