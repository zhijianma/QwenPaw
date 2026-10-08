# -*- coding: utf-8 -*-
"""Compile and persist Lite invocation capability lock manifests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Sequence

from ..kernel import (
    CapabilityLease,
    CapabilityLockManifest,
    CapabilityRelease,
    InvocationScope,
)
from ..utils.io_utils import (
    get_path_lock,
    read_json_async,
    run_sync_io,
    write_json_atomic_async,
)


class CapabilityLockConflictError(RuntimeError):
    """Raised when one Invocation already has different release evidence."""


class CapabilityLockCompiler:
    """Resolve selected releases from one already-pinned generation."""

    @staticmethod
    def compile(
        scope: InvocationScope,
        lease: CapabilityLease,
    ) -> CapabilityLockManifest:
        """Create deterministic lock evidence without implementation data."""
        if lease.registry_epoch_id != scope.registry_epoch_id:
            raise ValueError("capability lease registry epoch mismatch")
        if lease.generation != scope.registry_generation:
            raise ValueError("capability lease generation mismatch")
        releases = []
        for capability_id in scope.capability_ids:
            descriptor = lease.resolve(capability_id)
            if descriptor is None:
                raise LookupError(
                    f"selected capability '{capability_id}' is unavailable",
                )
            descriptor_payload = descriptor.model_dump(mode="json")
            descriptor_encoded = json.dumps(
                descriptor_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            descriptor_digest = hashlib.sha256(
                descriptor_encoded,
            ).hexdigest()
            releases.append(
                CapabilityRelease(
                    capability_id=descriptor.capability_id,
                    slot=descriptor.slot,
                    provider_id=descriptor.provider_id,
                    provider_kind=descriptor.provider_kind,
                    version=descriptor.version,
                    restart_policy=descriptor.restart_policy,
                    descriptor_hash=f"sha256:{descriptor_digest}",
                ),
            )
        return CapabilityLockManifest.create(
            invocation_id=scope.invocation_id,
            correlation_id=scope.correlation_id or scope.invocation_id,
            agent_id=scope.agent_id,
            conversation_id=scope.conversation_id,
            registry_epoch_id=scope.registry_epoch_id,
            registry_generation=scope.registry_generation,
            releases=tuple(releases),
        )


class FilesystemCapabilityLockStore:
    """Owner-only append-once Lite store for capability lock evidence."""

    def __init__(self, workspace_dir: Path) -> None:
        self._root = (
            Path(workspace_dir) / ".qwenpaw" / "lite" / "capability-locks"
        )

    @staticmethod
    def _owner_key(owner: str) -> str:
        return hashlib.sha256(owner.encode("utf-8")).hexdigest()

    def _path(self, manifest: CapabilityLockManifest) -> Path:
        owner = manifest.conversation_id or (
            f"invocation:{manifest.invocation_id}"
        )
        return (
            self._root
            / self._owner_key(owner)
            / str(manifest.invocation_id)
            / "lock.json"
        )

    async def append(self, manifest: CapabilityLockManifest) -> None:
        """Persist one Invocation lock exactly once."""
        path = self._path(manifest)
        async with get_path_lock(path):
            try:
                payload = await read_json_async(path)
            except FileNotFoundError:
                payload = None
            if payload is not None:
                existing = CapabilityLockManifest.model_validate(payload)
                existing_evidence = existing.model_dump(
                    mode="json",
                    exclude={"created_at"},
                )
                incoming_evidence = manifest.model_dump(
                    mode="json",
                    exclude={"created_at"},
                )
                if existing_evidence != incoming_evidence:
                    raise CapabilityLockConflictError(
                        "Invocation already has different capability lock",
                    )
                return
            await write_json_atomic_async(
                path,
                manifest.model_dump(mode="json"),
                sort_keys=True,
            )

    def _list_sync(
        self,
        conversation_id: str,
        limit: int,
    ) -> list[CapabilityLockManifest]:
        root = self._root / self._owner_key(conversation_id)
        manifests = [
            CapabilityLockManifest.model_validate(
                json.loads(path.read_text(encoding="utf-8")),
            )
            for path in root.glob("*/lock.json")
        ]
        manifests.sort(key=lambda item: item.created_at, reverse=True)
        return manifests[:limit]

    async def list_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 100,
    ) -> Sequence[CapabilityLockManifest]:
        """Return newest Invocation locks for one ChatSpec identity."""
        if not conversation_id.strip():
            raise ValueError("conversation_id cannot be empty")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        return await run_sync_io(
            self._list_sync,
            conversation_id,
            limit,
        )


def lite_capability_lock_store(
    workspace_dir: Path,
) -> FilesystemCapabilityLockStore:
    """Return the Lite lock store for one workspace."""
    return FilesystemCapabilityLockStore(workspace_dir)


__all__ = [
    "CapabilityLockCompiler",
    "CapabilityLockConflictError",
    "FilesystemCapabilityLockStore",
    "lite_capability_lock_store",
]
