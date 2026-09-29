# -*- coding: utf-8 -*-
"""Declared product profiles independent of runtime availability."""

from types import MappingProxyType
from typing import Mapping

from .hub import HUB_PROFILE
from .lite import LITE_PROFILE
from .models import EditionProfile
from .workstation import WORKSTATION_PROFILE

EDITION_PROFILES: Mapping[str, EditionProfile] = MappingProxyType(
    {
        "lite": LITE_PROFILE,
        "workstation": WORKSTATION_PROFILE,
        "hub": HUB_PROFILE,
    },
)


def describe_edition(value: str) -> EditionProfile:
    """Return a declared profile without implying runtime availability."""
    edition = value.strip().lower()
    try:
        return EDITION_PROFILES[edition]
    except KeyError as exc:
        raise ValueError(f"unknown edition '{edition}'") from exc
