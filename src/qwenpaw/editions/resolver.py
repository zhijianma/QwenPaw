# -*- coding: utf-8 -*-
"""Resolve product profiles without importing edition implementations."""

import os

from .catalog import EDITION_PROFILES
from .models import EditionProfile

EDITION_ENV = "QWENPAW_EDITION"
AVAILABLE_EDITIONS = frozenset({"lite"})


class EditionUnavailableError(ValueError):
    """Raised when this distribution does not implement an edition."""


def resolve_edition(value: str | None = None) -> EditionProfile:
    """Resolve an explicit or environment edition; Lite is the default."""
    edition = (value or os.environ.get(EDITION_ENV) or "lite").strip().lower()
    if edition not in EDITION_PROFILES:
        raise EditionUnavailableError(f"unknown edition '{edition}'")
    if edition not in AVAILABLE_EDITIONS:
        raise EditionUnavailableError(
            f"edition '{edition}' is recognized but unavailable in this "
            "Lite slice",
        )
    return EDITION_PROFILES[edition]
