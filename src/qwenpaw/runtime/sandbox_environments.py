# -*- coding: utf-8 -*-
"""Translate legacy per-tool sandbox policy into environment evidence."""

from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from ..kernel import (
    EnvironmentContract,
    EnvironmentDependency,
    EnvironmentDependencyKind,
    EnvironmentFilesystemMode,
    EnvironmentIsolation,
    EnvironmentMount,
    EnvironmentMountAccess,
    EnvironmentNetworkMode,
    EnvironmentPortRule,
    EnvironmentRecord,
    EnvironmentRef,
    EnvironmentResolution,
    EnvironmentResolutionStatus,
    EnvironmentResourceLimits,
    EnvironmentStore,
    EnvironmentVariableMode,
    InvocationScope,
)
from ..sandbox import SandboxConfig, SandboxMode, create_sandbox
from ..utils.io_utils import run_sync_io
from .environments import _os_family

SANDBOX_ENVIRONMENT_RESOLVER_ID = "qwenpaw.system.environment.sandbox-resolver"


def sandbox_environment_contract(config: SandboxConfig) -> EnvironmentContract:
    """Create a value-only contract without persisting environment values."""
    workspace = Path(config.workspace_dir).expanduser().resolve(strict=False)
    mounts: list[EnvironmentMount] = []
    for index, mount in enumerate(config.mounts, start=1):
        source = Path(mount.path).expanduser().resolve(strict=False)
        if source == workspace:
            continue
        mounts.append(
            EnvironmentMount(
                source=str(source),
                target=f"mount-{index}",
                access=(
                    EnvironmentMountAccess.READ_WRITE
                    if mount.writable
                    else EnvironmentMountAccess.READ_ONLY
                ),
                executable=mount.executable,
            ),
        )
    if config.network_allow == ["*"]:
        network_mode = EnvironmentNetworkMode.INHERIT
        allowed_hosts: tuple[str, ...] = ()
    elif config.network_allow:
        network_mode = EnvironmentNetworkMode.RESTRICTED
        allowed_hosts = tuple(config.network_allow)
    else:
        network_mode = EnvironmentNetworkMode.DENY
        allowed_hosts = ()
    environment_variables = tuple(sorted(config.env_vars))
    environment_mode = (
        EnvironmentVariableMode.ALLOWLIST
        if config.env_mode == "allowlist"
        else (
            EnvironmentVariableMode.INJECT
            if environment_variables
            else EnvironmentVariableMode.INHERIT
        )
    )
    dependencies = (
        (
            EnvironmentDependency(
                kind=EnvironmentDependencyKind.EXECUTABLE,
                name=config.shell_executable,
            ),
        )
        if config.shell_executable
        else ()
    )
    return EnvironmentContract(
        contract_id="qwenpaw.system.environment.sandbox",
        isolation=(
            EnvironmentIsolation.HOST
            if config.mode is SandboxMode.NONE
            else EnvironmentIsolation.SANDBOX
        ),
        filesystem_mode=(
            EnvironmentFilesystemMode.READ_ALL
            if config.allow_read_all
            else EnvironmentFilesystemMode.ALLOWLIST
        ),
        workspace=EnvironmentMount(
            source=str(workspace),
            target="workspace",
            access=EnvironmentMountAccess.READ_WRITE,
            executable=True,
        ),
        mounts=tuple(mounts),
        denied_paths=tuple(config.deny_paths),
        network_mode=network_mode,
        allowed_hosts=allowed_hosts,
        network_ports=tuple(
            EnvironmentPortRule(
                port=rule.port,
                direction=rule.direction,
                allow=rule.allow,
            )
            for rule in (config.network_ports or ())
        ),
        environment_mode=environment_mode,
        environment_variables=environment_variables,
        dependencies=dependencies,
        native_constraint_names=tuple(sorted(config.platform_hints)),
        resources=EnvironmentResourceLimits(
            memory_mb=config.max_memory_mb,
            max_processes=config.max_processes,
        ),
        timeout_seconds=config.timeout_seconds,
    )


def _backend_enforced_fields(backend: Any) -> frozenset[str]:
    method = getattr(backend, "_enforced_fields", None)
    if not callable(method):
        return frozenset()
    return frozenset(str(item) for item in method())


def _dependency_available(dependency: EnvironmentDependency) -> bool:
    if dependency.kind is EnvironmentDependencyKind.EXECUTABLE:
        return shutil.which(dependency.name) is not None
    return Path(dependency.name).expanduser().exists()


def _resolve_mounts(
    contract: EnvironmentContract,
    mode: SandboxMode,
    enforced_fields: frozenset[str],
) -> tuple[list[str], list[str]]:
    enforced: list[str] = []
    violations: list[str] = []
    for index, mount in enumerate((contract.workspace, *contract.mounts)):
        path = Path(mount.source)
        prefix = "workspace" if index == 0 else "mount"
        if not path.exists():
            violations.append(f"environment.{prefix}.missing")
        elif not os.access(path, os.R_OK):
            violations.append(f"environment.{prefix}.unreadable")
        elif (
            mount.access is EnvironmentMountAccess.READ_WRITE
            and not os.access(path, os.W_OK)
        ):
            violations.append(f"environment.{prefix}.unwritable")
        elif "mounts" in enforced_fields:
            enforced.append(f"filesystem.{prefix}")
        elif mode is not SandboxMode.NONE:
            violations.append(f"environment.{prefix}.unenforced")
    return enforced, violations


def _resolve_declared_fields(
    contract: EnvironmentContract,
    enforced_fields: frozenset[str],
) -> tuple[list[str], list[str]]:
    checks = (
        (
            bool(contract.denied_paths),
            "deny_paths",
            "environment.denied_paths.unenforced",
        ),
        (
            contract.network_mode is not EnvironmentNetworkMode.INHERIT,
            "network_allow",
            "environment.network.unenforced",
        ),
        (
            bool(contract.network_ports),
            "network_ports",
            "environment.network_ports.unenforced",
        ),
        (
            contract.resources.max_processes is not None,
            "max_processes",
            "environment.process_limit.unenforced",
        ),
        (
            contract.resources.memory_mb is not None,
            "max_memory_mb",
            "environment.memory_limit.unenforced",
        ),
        (
            contract.environment_mode is EnvironmentVariableMode.ALLOWLIST,
            "env_mode",
            "environment.variable_allowlist.unenforced",
        ),
        (
            bool(contract.native_constraint_names),
            "platform_hints",
            "environment.native_constraints.unenforced",
        ),
    )
    enforced = [
        field
        for requested, field, _ in checks
        if requested and field in enforced_fields
    ]
    violations = [
        violation
        for requested, field, violation in checks
        if requested and field not in enforced_fields
    ]
    return enforced, violations


def _resolve_dependencies(
    contract: EnvironmentContract,
    enforced_fields: frozenset[str],
) -> tuple[list[str], list[str]]:
    enforced: list[str] = []
    violations: list[str] = []
    for dependency in contract.dependencies:
        if not _dependency_available(dependency):
            violations.append("environment.dependency.missing")
        elif "shell_executable" in enforced_fields:
            enforced.append("dependency.shell")
        else:
            violations.append("environment.dependency.unenforced")
    return enforced, violations


class SandboxEnvironmentResolver:
    """Describe what the selected legacy sandbox backend really enforces."""

    resolver_id = SANDBOX_ENVIRONMENT_RESOLVER_ID

    @staticmethod
    def _resolve_sync(
        config: SandboxConfig,
        contract: EnvironmentContract,
        invocation_id: UUID,
        action_id: UUID,
    ) -> EnvironmentResolution:
        backend = create_sandbox(config)
        effective_config = getattr(backend, "config", config)
        enforced_fields = _backend_enforced_fields(backend)
        violations: list[str] = []
        enforced = ["timeout"]

        if effective_config.mode is SandboxMode.NONE:
            enforced.append("isolation.host")
            if contract.isolation is EnvironmentIsolation.SANDBOX:
                violations.append("environment.isolation.unenforced")
            if contract.filesystem_mode is EnvironmentFilesystemMode.ALLOWLIST:
                violations.append("environment.filesystem.unenforced")
        else:
            enforced.append("isolation.sandbox")
            enforced.append("filesystem.visibility")

        mount_proofs, mount_violations = _resolve_mounts(
            contract,
            effective_config.mode,
            enforced_fields,
        )
        enforced.extend(mount_proofs)
        violations.extend(mount_violations)
        field_proofs, field_violations = _resolve_declared_fields(
            contract,
            enforced_fields,
        )
        enforced.extend(field_proofs)
        violations.extend(field_violations)

        if contract.environment_variables:
            enforced.append("environment.inject")
        if contract.network_mode is EnvironmentNetworkMode.INHERIT:
            enforced.append("network.inherit")
        dependency_proofs, dependency_violations = _resolve_dependencies(
            contract,
            enforced_fields,
        )
        enforced.extend(dependency_proofs)
        violations.extend(dependency_violations)

        unique_violations = tuple(dict.fromkeys(violations))
        return EnvironmentResolution(
            resolution_id=uuid5(
                action_id,
                SANDBOX_ENVIRONMENT_RESOLVER_ID,
            ),
            invocation_id=invocation_id,
            action_id=action_id,
            contract_id=contract.contract_id,
            contract_version=contract.version,
            resolver_id=SANDBOX_ENVIRONMENT_RESOLVER_ID,
            status=(
                EnvironmentResolutionStatus.UNSATISFIED
                if unique_violations
                else EnvironmentResolutionStatus.SATISFIED
            ),
            os_family=_os_family(),
            architecture=platform.machine().strip().lower() or "unknown",
            workspace_root=str(
                Path(effective_config.workspace_dir).resolve(strict=False),
            ),
            enforced_constraints=tuple(dict.fromkeys(enforced)),
            violations=unique_violations,
        )

    async def resolve(
        self,
        config: SandboxConfig,
        *,
        invocation_id: UUID,
        action_id: UUID,
    ) -> tuple[EnvironmentContract, EnvironmentResolution]:
        """Translate and resolve a per-action sandbox without blocking."""
        contract = sandbox_environment_contract(config)
        resolution = await run_sync_io(
            self._resolve_sync,
            config,
            contract,
            invocation_id,
            action_id,
        )
        return contract, resolution


class RuntimeSandboxEnvironmentManager:
    """Resolve and persist one sandbox environment before an Action runs."""

    def __init__(
        self,
        scope: InvocationScope,
        store: EnvironmentStore,
        resolver: SandboxEnvironmentResolver | None = None,
    ) -> None:
        self._scope = scope
        self._store = store
        self._resolver = resolver or SandboxEnvironmentResolver()

    async def resolve(
        self,
        config: SandboxConfig,
        *,
        action_id: UUID,
    ) -> tuple[EnvironmentRef, EnvironmentResolution]:
        """Return a durable action-scoped reference and its status."""
        contract, resolution = await self._resolver.resolve(
            config,
            invocation_id=self._scope.invocation_id,
            action_id=action_id,
        )
        await self._store.record(
            EnvironmentRecord(contract=contract, resolution=resolution),
            conversation_id=self._scope.conversation_id,
        )
        return (
            EnvironmentRef(
                resolution_id=resolution.resolution_id,
                contract_id=resolution.contract_id,
                contract_version=resolution.contract_version,
                resolver_id=resolution.resolver_id,
            ),
            resolution,
        )


__all__ = [
    "SANDBOX_ENVIRONMENT_RESOLVER_ID",
    "RuntimeSandboxEnvironmentManager",
    "SandboxEnvironmentResolver",
    "sandbox_environment_contract",
]
