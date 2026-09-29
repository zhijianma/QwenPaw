# -*- coding: utf-8 -*-
"""Product edition profiles composed over the shared kernel."""

from .catalog import EDITION_PROFILES, describe_edition
from .deployment import (
    DEPLOYMENT_REQUIREMENTS,
    EditionDeploymentAdapter,
    EditionDeploymentUnavailable,
    EditionRuntimeBindings,
    build_deployment_adapter,
    deployment_requirements,
)
from .models import EditionProfile
from .resolver import resolve_edition

__all__ = [
    "EDITION_PROFILES",
    "DEPLOYMENT_REQUIREMENTS",
    "EditionDeploymentAdapter",
    "EditionDeploymentUnavailable",
    "EditionProfile",
    "EditionRuntimeBindings",
    "build_deployment_adapter",
    "deployment_requirements",
    "describe_edition",
    "resolve_edition",
]
