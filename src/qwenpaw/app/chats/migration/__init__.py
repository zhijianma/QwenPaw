# -*- coding: utf-8 -*-
"""Legacy Chat inventory and migration orchestration."""

from .inventory import LegacyInventoryReport, inventory_legacy_workspace
from .normalize import (
    LegacyNormalizationError,
    NormalizedTranscript,
    normalize_legacy_snapshot,
)
from .orchestrator import (
    LegacyMigrationCoordinator,
    LegacyMigrationResult,
    LegacyMigrationStateError,
)
from .snapshot import (
    LegacySnapshotError,
    LegacySnapshotManifest,
    create_legacy_snapshot,
    validate_legacy_snapshot,
)

__all__ = [
    "LegacyInventoryReport",
    "LegacySnapshotError",
    "LegacySnapshotManifest",
    "LegacyNormalizationError",
    "LegacyMigrationCoordinator",
    "LegacyMigrationResult",
    "LegacyMigrationStateError",
    "NormalizedTranscript",
    "create_legacy_snapshot",
    "inventory_legacy_workspace",
    "normalize_legacy_snapshot",
    "validate_legacy_snapshot",
]
