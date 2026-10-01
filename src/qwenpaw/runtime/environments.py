# -*- coding: utf-8 -*-
"""Fail-closed Lite resolution for backend-neutral environment contracts."""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import sys
from pathlib import Path
from uuid import UUID

from ..kernel import (
    EnvironmentContract,
    EnvironmentDependencyKind,
    EnvironmentIsolation,
    EnvironmentMount,
    EnvironmentMountAccess,
    EnvironmentNetworkMode,
    EnvironmentRecord,
    EnvironmentResolution,
    EnvironmentResolutionStatus,
)
from ..utils.io_utils import (
    get_path_lock,
    read_json_async,
    run_sync_io,
    write_json_atomic_async,
)

LITE_ENVIRONMENT_RESOLVER_ID = "qwenpaw.system.environment.lite-resolver"


class EnvironmentContractConflictError(RuntimeError):
    """Raised when immutable environment evidence already differs."""


class EnvironmentContractUnsatisfiedError(RuntimeError):
    """Raised before execution when Lite cannot enforce a requirement."""

    def __init__(self, resolution: EnvironmentResolution) -> None:
        self.resolution = resolution
        super().__init__(
            "environment contract is unsatisfied: "
            f"{', '.join(resolution.violations)}",
        )


def _os_family() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    if os.name == "nt":
        return "windows"
    raise RuntimeError(f"unsupported host platform: {sys.platform}")


def default_lite_environment_contract(
    workspace_dir: str | Path,
) -> EnvironmentContract:
    """Return the explicit requirements of the existing local Chat path."""
    workspace = str(Path(workspace_dir).resolve(strict=False))
    return EnvironmentContract(
        workspace=EnvironmentMount(
            source=workspace,
            target="workspace",
            access=EnvironmentMountAccess.READ_WRITE,
            executable=True,
        ),
    )


class LiteEnvironmentResolver:
    """Verify only constraints the local Lite edition can prove."""

    resolver_id = LITE_ENVIRONMENT_RESOLVER_ID

    @staticmethod
    def _mount_path(source: str, workspace_dir: Path) -> Path:
        path = Path(source).expanduser()
        if not path.is_absolute():
            path = workspace_dir / path
        return path.resolve(strict=False)

    def _mount_violations(
        self,
        contract: EnvironmentContract,
        root: Path,
    ) -> list[str]:
        violations: list[str] = []
        mounts = (contract.workspace, *contract.mounts)
        for index, mount in enumerate(mounts):
            path = self._mount_path(mount.source, root)
            prefix = "workspace" if index == 0 else "mount"
            if not path.exists():
                if mount.required:
                    violations.append(f"environment.{prefix}.missing")
                continue
            if not os.access(path, os.R_OK):
                violations.append(f"environment.{prefix}.unreadable")
            if mount.executable and not os.access(path, os.X_OK):
                violations.append(f"environment.{prefix}.unexecutable")
            if (
                mount.access is EnvironmentMountAccess.READ_WRITE
                and not os.access(path, os.W_OK)
            ):
                violations.append(f"environment.{prefix}.unwritable")
            if index == 0 and path != root:
                violations.append("environment.workspace.mismatch")
        return violations

    def _dependency_violations(
        self,
        contract: EnvironmentContract,
        root: Path,
    ) -> list[str]:
        violations: list[str] = []
        for dependency in contract.dependencies:
            if dependency.kind is EnvironmentDependencyKind.EXECUTABLE:
                available = shutil.which(dependency.name) is not None
            else:
                available = self._mount_path(dependency.name, root).exists()
            if dependency.required and not available:
                violations.append("environment.dependency.missing")
            if dependency.version_constraint is not None:
                violations.append("environment.dependency.version.unsupported")
        return violations

    @staticmethod
    def _capability_resolution(
        contract: EnvironmentContract,
    ) -> tuple[list[str], list[str]]:
        enforced: list[str] = []
        checks = (
            (
                contract.isolation is not EnvironmentIsolation.HOST,
                "environment.isolation.unsupported",
                "isolation.host",
            ),
            (
                contract.runtime_image is not None,
                "environment.image.unsupported",
                None,
            ),
            (
                contract.network_mode is not EnvironmentNetworkMode.INHERIT,
                "environment.network.unsupported",
                "network.inherit",
            ),
            (
                bool(contract.credential_refs),
                "environment.credentials.unsupported",
                None,
            ),
            (
                contract.resources.constrained,
                "environment.resources.unsupported",
                None,
            ),
            (
                contract.timeout_seconds is not None,
                "environment.timeout.unsupported",
                None,
            ),
            (
                contract.max_concurrency is not None,
                "environment.concurrency.unsupported",
                None,
            ),
            (
                contract.snapshot_required,
                "environment.snapshot.unsupported",
                None,
            ),
            (
                contract.cleanup_policy != "retain",
                "environment.cleanup.unsupported",
                None,
            ),
        )
        violations = [code for unsupported, code, _ in checks if unsupported]
        enforced.extend(
            proof
            for unsupported, _, proof in checks
            if not unsupported and proof is not None
        )
        return enforced, violations

    def _resolve_sync(
        self,
        contract: EnvironmentContract,
        invocation_id: UUID,
        workspace_dir: str,
    ) -> EnvironmentResolution:
        root = Path(workspace_dir).expanduser().resolve(strict=False)
        os_family = _os_family()
        architecture = platform.machine().strip().lower() or "unknown"
        enforced = ["workspace", "mounts", "dependencies"]
        violations: list[str] = []

        if contract.os_families and os_family not in contract.os_families:
            violations.append("environment.os.unsupported")
        normalized_architectures = {
            item.strip().lower() for item in contract.architectures
        }
        if normalized_architectures and architecture not in (
            normalized_architectures
        ):
            violations.append("environment.architecture.unsupported")

        violations.extend(self._mount_violations(contract, root))
        violations.extend(self._dependency_violations(contract, root))
        capability_proofs, capability_violations = self._capability_resolution(
            contract,
        )
        enforced.extend(capability_proofs)
        violations.extend(capability_violations)

        unique_violations = tuple(dict.fromkeys(violations))
        return EnvironmentResolution(
            invocation_id=invocation_id,
            contract_id=contract.contract_id,
            contract_version=contract.version,
            resolver_id=self.resolver_id,
            status=(
                EnvironmentResolutionStatus.UNSATISFIED
                if unique_violations
                else EnvironmentResolutionStatus.SATISFIED
            ),
            os_family=os_family,
            architecture=architecture,
            workspace_root=str(root),
            enforced_constraints=tuple(enforced),
            violations=unique_violations,
        )

    async def resolve(
        self,
        contract: EnvironmentContract,
        *,
        invocation_id: UUID,
        workspace_dir: str,
    ) -> EnvironmentResolution:
        """Resolve the contract without blocking the event loop on I/O."""
        return await run_sync_io(
            self._resolve_sync,
            contract,
            invocation_id,
            workspace_dir,
        )


class FilesystemEnvironmentStore:
    """Owner-only Lite store for one resolution per invocation."""

    def __init__(self, workspace_dir: str | Path) -> None:
        self._root = Path(workspace_dir) / ".qwenpaw" / "lite" / "environments"

    @staticmethod
    def _owner_key(owner: str) -> str:
        return hashlib.sha256(owner.encode("utf-8")).hexdigest()

    def _record_path(
        self,
        record: EnvironmentRecord,
        conversation_id: str | None,
    ) -> Path:
        invocation_id = record.resolution.invocation_id
        owner = conversation_id or f"invocation:{invocation_id}"
        return (
            self._root
            / self._owner_key(owner)
            / str(invocation_id)
            / "environment.json"
        )

    async def record(
        self,
        record: EnvironmentRecord,
        *,
        conversation_id: str | None,
    ) -> None:
        """Persist immutable evidence and reject identity reuse."""
        path = self._record_path(record, conversation_id)
        async with get_path_lock(path):
            try:
                existing_payload = await read_json_async(path)
            except FileNotFoundError:
                existing_payload = None
            if existing_payload is not None:
                existing = EnvironmentRecord.model_validate(existing_payload)
                if existing != record:
                    raise EnvironmentContractConflictError(
                        "invocation already has different environment "
                        "evidence",
                    )
                return
            await write_json_atomic_async(
                path,
                record.model_dump(mode="json"),
                sort_keys=True,
            )


__all__ = [
    "EnvironmentContractConflictError",
    "EnvironmentContractUnsatisfiedError",
    "FilesystemEnvironmentStore",
    "LITE_ENVIRONMENT_RESOLVER_ID",
    "LiteEnvironmentResolver",
    "default_lite_environment_contract",
]
