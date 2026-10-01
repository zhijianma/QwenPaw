# -*- coding: utf-8 -*-
"""Environment contracts for local third-party Harness providers."""

from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path
from uuid import UUID, uuid5

from ..harnesses.capabilities import HarnessRuntimeCapabilities
from ..kernel import (
    EnvironmentContract,
    EnvironmentDependency,
    EnvironmentDependencyKind,
    EnvironmentEvidenceLevel,
    EnvironmentFilesystemMode,
    EnvironmentIsolation,
    EnvironmentMount,
    EnvironmentMountAccess,
    EnvironmentNetworkMode,
    EnvironmentRecord,
    EnvironmentResolution,
    EnvironmentResolutionStatus,
    EnvironmentStore,
    EnvironmentVariableMode,
)
from ..utils.io_utils import run_sync_io
from .environments import EnvironmentContractUnsatisfiedError, _os_family

HARNESS_ENVIRONMENT_RESOLVER_ID = "qwenpaw.system.environment.harness-resolver"
_CODEX_SANDBOX_MODES = {
    "read-only",
    "workspace-write",
    "danger-full-access",
}
_QODER_PERMISSION_MODES = {
    "default",
    "acceptEdits",
    "plan",
    "auto",
    "bypassPermissions",
    "yolo",
}


def _capabilities(settings: dict[str, object]) -> HarnessRuntimeCapabilities:
    value = settings.get("_runtime_capabilities")
    if isinstance(value, HarnessRuntimeCapabilities):
        return value
    return HarnessRuntimeCapabilities()


def _provider_posture(
    backend: str,
    settings: dict[str, object],
) -> tuple[
    EnvironmentIsolation,
    EnvironmentFilesystemMode,
    EnvironmentMountAccess,
    tuple[str, ...],
]:
    if backend == "codex":
        sandbox = str(settings.get("sandbox") or "workspace-write")
        if sandbox == "danger-full-access":
            return (
                EnvironmentIsolation.HOST,
                EnvironmentFilesystemMode.HOST,
                EnvironmentMountAccess.READ_WRITE,
                ("provider.codex.sandbox.danger-full-access",),
            )
        access = (
            EnvironmentMountAccess.READ_ONLY
            if sandbox == "read-only"
            else EnvironmentMountAccess.READ_WRITE
        )
        return (
            EnvironmentIsolation.SANDBOX,
            EnvironmentFilesystemMode.ALLOWLIST,
            access,
            (f"provider.codex.sandbox.{sandbox}",),
        )
    if backend != "qoder":
        return (
            EnvironmentIsolation.HOST,
            EnvironmentFilesystemMode.HOST,
            EnvironmentMountAccess.READ_WRITE,
            (f"provider.{backend}.unsupported",),
        )
    permission_mode = str(settings.get("permission_mode") or "default")
    return (
        EnvironmentIsolation.HOST,
        EnvironmentFilesystemMode.HOST,
        EnvironmentMountAccess.READ_WRITE,
        (f"provider.qoder.permission.{permission_mode}",),
    )


def harness_environment_contract(
    backend: str,
    cwd: Path,
    settings: dict[str, object],
) -> EnvironmentContract:
    """Translate public Harness settings without retaining secret values."""
    isolation, filesystem, access, native = _provider_posture(
        backend,
        settings,
    )
    capabilities = _capabilities(settings)
    mounts: list[EnvironmentMount] = []
    dependencies: list[EnvironmentDependency] = []
    dependency_names: set[str] = set()
    environment_variables: set[str] = set()
    credential_refs: set[str] = set()
    for index, skill in enumerate(capabilities.skills, start=1):
        mounts.append(
            EnvironmentMount(
                source=str(skill.directory.resolve(strict=False)),
                target=f"skill-{index}",
                access=EnvironmentMountAccess.READ_ONLY,
            ),
        )
    for index, server in enumerate(capabilities.mcp_servers, start=1):
        environment_variables.update(server.env)
        if server.env or server.headers:
            credential_refs.add(f"mcp:{server.name}")
        if server.transport == "stdio":
            if server.command and server.command not in dependency_names:
                dependency_names.add(server.command)
                dependencies.append(
                    EnvironmentDependency(
                        kind=EnvironmentDependencyKind.EXECUTABLE,
                        name=server.command,
                    ),
                )
            if server.cwd is not None:
                mounts.append(
                    EnvironmentMount(
                        source=str(server.cwd.resolve(strict=False)),
                        target=f"mcp-cwd-{index}",
                        access=EnvironmentMountAccess.READ_ONLY,
                    ),
                )
    return EnvironmentContract(
        contract_id="qwenpaw.system.environment.harness",
        isolation=isolation,
        filesystem_mode=filesystem,
        workspace=EnvironmentMount(
            source=str(cwd.resolve(strict=False)),
            target="workspace",
            access=access,
            executable=True,
        ),
        mounts=tuple(mounts),
        network_mode=EnvironmentNetworkMode.INHERIT,
        environment_variables=tuple(sorted(environment_variables)),
        environment_mode=(
            EnvironmentVariableMode.INJECT
            if environment_variables
            else EnvironmentVariableMode.INHERIT
        ),
        credential_refs=tuple(sorted(credential_refs)),
        dependencies=tuple(dependencies),
        native_constraint_names=native,
    )


def _path_violations(contract: EnvironmentContract) -> list[str]:
    violations: list[str] = []
    for index, mount in enumerate((contract.workspace, *contract.mounts)):
        path = Path(mount.source)
        prefix = "workspace" if index == 0 else "mount"
        if not path.exists():
            if mount.required:
                violations.append(f"environment.{prefix}.missing")
        elif not os.access(path, os.R_OK):
            violations.append(f"environment.{prefix}.unreadable")
        elif mount.executable and not os.access(path, os.X_OK):
            violations.append(f"environment.{prefix}.unexecutable")
        elif (
            mount.access is EnvironmentMountAccess.READ_WRITE
            and not os.access(path, os.W_OK)
        ):
            violations.append(f"environment.{prefix}.unwritable")
    return violations


def _dependency_violations(contract: EnvironmentContract) -> list[str]:
    return [
        "environment.dependency.missing"
        for dependency in contract.dependencies
        if dependency.required and shutil.which(dependency.name) is None
    ]


class HarnessEnvironmentResolver:
    """Resolve host facts and label provider policy as declaration only."""

    resolver_id = HARNESS_ENVIRONMENT_RESOLVER_ID

    @staticmethod
    def _resolve_sync(
        backend: str,
        contract: EnvironmentContract,
        invocation_id: UUID,
        settings: dict[str, object],
    ) -> EnvironmentResolution:
        violations = _path_violations(contract)
        violations.extend(_dependency_violations(contract))
        if backend not in {"codex", "qoder"}:
            violations.append("environment.harness.backend.unsupported")
        if backend == "codex":
            sandbox = str(settings.get("sandbox") or "workspace-write")
            if sandbox not in _CODEX_SANDBOX_MODES:
                violations.append("environment.harness.sandbox.unknown")
        if backend == "qoder":
            permission = str(
                settings.get("permission_mode") or "default",
            )
            if permission not in _QODER_PERMISSION_MODES:
                violations.append("environment.harness.permission.unknown")
        capabilities = _capabilities(settings)
        if any(
            server.transport == "stdio" and not server.command
            for server in capabilities.mcp_servers
        ):
            violations.append("environment.harness.mcp_command.missing")
        unique_violations = tuple(dict.fromkeys(violations))
        return EnvironmentResolution(
            resolution_id=uuid5(
                invocation_id,
                HARNESS_ENVIRONMENT_RESOLVER_ID,
            ),
            invocation_id=invocation_id,
            contract_id=contract.contract_id,
            contract_version=contract.version,
            resolver_id=HARNESS_ENVIRONMENT_RESOLVER_ID,
            status=(
                EnvironmentResolutionStatus.UNSATISFIED
                if unique_violations
                else EnvironmentResolutionStatus.SATISFIED
            ),
            evidence_level=EnvironmentEvidenceLevel.PROVIDER_DECLARED,
            os_family=_os_family(),
            architecture=platform.machine().strip().lower() or "unknown",
            workspace_root=contract.workspace.source,
            enforced_constraints=(
                "workspace.host-verified",
                "dependencies.host-verified",
            ),
            declared_constraints=contract.native_constraint_names,
            violations=unique_violations,
        )

    async def resolve(
        self,
        backend: str,
        cwd: Path,
        settings: dict[str, object],
        *,
        invocation_id: UUID,
    ) -> tuple[EnvironmentContract, EnvironmentResolution]:
        """Resolve one local Harness launch without blocking the loop."""
        contract = harness_environment_contract(backend, cwd, settings)
        resolution = await run_sync_io(
            self._resolve_sync,
            backend,
            contract,
            invocation_id,
            settings,
        )
        return contract, resolution


class RuntimeHarnessEnvironmentManager:
    """Persist one immutable Harness environment per controlled invocation."""

    def __init__(
        self,
        store: EnvironmentStore,
        resolver: HarnessEnvironmentResolver | None = None,
    ) -> None:
        self._store = store
        self._resolver = resolver or HarnessEnvironmentResolver()

    async def resolve(
        self,
        backend: str,
        cwd: Path,
        settings: dict[str, object],
        *,
        invocation_id: UUID,
        conversation_id: str | None,
    ) -> EnvironmentResolution:
        """Persist declared and resolved facts before provider dispatch."""
        contract, resolution = await self._resolver.resolve(
            backend,
            cwd,
            settings,
            invocation_id=invocation_id,
        )
        await self._store.record(
            EnvironmentRecord(contract=contract, resolution=resolution),
            conversation_id=conversation_id,
        )
        if resolution.status is EnvironmentResolutionStatus.UNSATISFIED:
            raise EnvironmentContractUnsatisfiedError(resolution)
        return resolution


__all__ = [
    "HARNESS_ENVIRONMENT_RESOLVER_ID",
    "HarnessEnvironmentResolver",
    "RuntimeHarnessEnvironmentManager",
    "harness_environment_contract",
]
