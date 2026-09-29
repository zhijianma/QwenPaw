# -*- coding: utf-8 -*-
"""Deployment adapter boundary shared by every product edition."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Protocol, runtime_checkable

from ..kernel.ports import ArtifactStore, CapabilityResolver
from ..tasks.artifacts import FilesystemArtifactStore
from ..tasks.bootstrap import task_service_for_workspace
from ..tasks.service import TaskService
from .models import EditionProfile

DEPLOYMENT_REQUIREMENTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "lite": (
            "runner.local",
            "ledger.sqlite_wal",
            "artifact.filesystem",
            "scheduler.in_process",
        ),
        "workstation": (
            "runner.local_pool",
            "scheduler.local_durable",
            "harness.adapter",
            "resource.local_budget",
        ),
        "hub": (
            "runner.distributed",
            "ledger.tenant_database",
            "artifact.object_store",
            "scheduler.distributed",
            "policy.tenant_rbac",
        ),
    },
)


class EditionDeploymentUnavailable(RuntimeError):
    """Report exact adapter gaps for a declared unavailable edition."""

    def __init__(
        self,
        edition: str,
        missing_adapters: tuple[str, ...],
    ) -> None:
        self.edition = edition
        self.missing_adapters = missing_adapters
        super().__init__(
            f"edition '{edition}' is missing deployment adapters: "
            f"{', '.join(missing_adapters)}",
        )


@dataclass(frozen=True)
class EditionRuntimeBindings:
    """Concrete runtime ports selected by one edition adapter."""

    profile: EditionProfile
    task_service: TaskService
    capability_resolver: CapabilityResolver

    def artifact_store(self, project_dir: Path) -> ArtifactStore:
        """Return the edition's content store scoped to one project."""
        return FilesystemArtifactStore(
            Path(project_dir) / ".qwenpaw" / "lite" / "artifacts",
        )


@runtime_checkable
class EditionDeploymentAdapter(Protocol):
    """Application-layer composition boundary over the shared Kernel."""

    @property
    def profile(self) -> EditionProfile:
        """Return the immutable product profile represented by this adapter."""

    def compose(self, workspace: Any) -> EditionRuntimeBindings:
        """Bind Kernel ports for one workspace without copying domain code."""


@dataclass(frozen=True)
class LiteDeploymentAdapter:
    """Runnable local-first deployment adapter for the Lite edition."""

    capability_resolver: CapabilityResolver
    profile: EditionProfile

    def compose(self, workspace: Any) -> EditionRuntimeBindings:
        """Compose existing Lite adapters around the shared Task service."""
        generation = int(
            getattr(self.capability_resolver, "generation", 1),
        )
        return EditionRuntimeBindings(
            profile=self.profile,
            task_service=task_service_for_workspace(
                workspace,
                registry_generation=generation,
            ),
            capability_resolver=self.capability_resolver,
        )


def deployment_requirements(profile: EditionProfile) -> tuple[str, ...]:
    """Return named adapter requirements for diagnostics and planning."""
    return DEPLOYMENT_REQUIREMENTS[profile.edition]


def build_deployment_adapter(
    profile: EditionProfile,
    capability_resolver: CapabilityResolver,
) -> EditionDeploymentAdapter:
    """Build the implemented adapter or fail with structured missing ports."""
    if profile.edition == "lite":
        return LiteDeploymentAdapter(
            capability_resolver=capability_resolver,
            profile=profile,
        )
    raise EditionDeploymentUnavailable(
        profile.edition,
        deployment_requirements(profile),
    )
