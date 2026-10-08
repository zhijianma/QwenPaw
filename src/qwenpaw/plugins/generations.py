# -*- coding: utf-8 -*-
"""Plugin compatibility adapters for the OS capability registry."""

from __future__ import annotations

import hashlib
import inspect
import os
import stat
from collections.abc import Awaitable, Callable
from pathlib import Path

from ..capabilities.registry import (
    ActivatedContribution,
    ActivationError,
    GenerationLease,
    GenerationRegistry as CoreGenerationRegistry,
    ProviderDeactivationError,
    ReleaseRollbackError,
    RegistrySnapshot,
)
from ..kernel.models import (
    CapabilityBundle,
    CapabilityContribution,
    CapabilityProviderKind,
    PluginContribution,
)
from ..kernel.releases import CapabilityPromotionCandidate
from .architecture import PluginManifest
from .contributions import (
    validate_contribution_implementation,
    validate_contributions,
)

PluginContributionFactory = Callable[
    [PluginContribution],
    object | Awaitable[object],
]

_IMPLEMENTATION_HASH_KEY = "qwenpaw.implementation_hash"
_IMPLEMENTATION_HASH_MAX_FILES = 5_000
_IMPLEMENTATION_HASH_MAX_BYTES = 64 * 1024 * 1024
_PAWPORT_MARKER = ".qwenpaw-pawport.json"
_UNKNOWN_IMPLEMENTATION_HASH = f"sha256:{'0' * 64}"


def plugin_implementation_hash(source_path: Path) -> str:
    """Hash a plugin tree without following links or executing code."""
    root = source_path.resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"Plugin source is not a directory: {root}")
    hasher = hashlib.sha256()
    files = 0
    total_bytes = 0
    for current, directory_names, file_names in os.walk(root):
        directory_names.sort()
        file_names.sort()
        current_path = Path(current)
        for name in directory_names:
            path = current_path / name
            if stat.S_ISLNK(path.lstat().st_mode):
                raise ValueError(f"Plugin source contains a link: {path}")
        for name in file_names:
            if name == _PAWPORT_MARKER:
                continue
            path = current_path / name
            before = path.lstat()
            if stat.S_ISLNK(before.st_mode):
                raise ValueError(f"Plugin source contains a link: {path}")
            if not stat.S_ISREG(before.st_mode):
                raise ValueError(
                    f"Plugin source contains a non-regular file: {path}",
                )
            files += 1
            total_bytes += before.st_size
            if files > _IMPLEMENTATION_HASH_MAX_FILES:
                raise ValueError("Plugin source exceeds the file limit")
            if total_bytes > _IMPLEMENTATION_HASH_MAX_BYTES:
                raise ValueError("Plugin source exceeds the byte limit")
            data = path.read_bytes()
            after = path.lstat()
            if (
                before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
            ):
                raise ValueError(
                    f"Plugin source changed while hashing: {path}",
                )
            relative = path.relative_to(root).as_posix().encode("utf-8")
            hasher.update(len(relative).to_bytes(4, "big"))
            hasher.update(relative)
            hasher.update(len(data).to_bytes(8, "big"))
            hasher.update(data)
    return f"sha256:{hasher.hexdigest()}"


async def activate_plugin_bundle(
    registry: CoreGenerationRegistry,
    manifest: PluginManifest,
    factory: PluginContributionFactory,
    *,
    implementation_hash: str = _UNKNOWN_IMPLEMENTATION_HASH,
    operator_authorized: bool = False,
) -> RegistrySnapshot:
    """Validate a plugin manifest and publish its common capability bundle."""
    bundle = plugin_capability_bundle(manifest, implementation_hash)
    declarations = validate_contributions(manifest)
    declaration_by_id = {item.contribution_id: item for item in declarations}

    async def adapter(declaration: CapabilityContribution) -> object:
        plugin_declaration = declaration_by_id[declaration.contribution_id]
        implementation = factory(plugin_declaration)
        if inspect.isawaitable(implementation):
            implementation = await implementation
        validate_contribution_implementation(
            manifest.id,
            plugin_declaration,
            implementation,
        )
        return implementation

    return await registry.activate_bundle(
        bundle,
        adapter,
        operator_authorized=operator_authorized,
    )


def plugin_capability_bundle(
    manifest: PluginManifest,
    implementation_hash: str,
) -> CapabilityBundle:
    """Build the exact public bundle used for candidate authorization."""
    declarations = validate_contributions(manifest)
    return CapabilityBundle(
        provider_id=manifest.id,
        provider_kind=CapabilityProviderKind.PLUGIN,
        version=manifest.version,
        restart_policy=manifest.restart_policy,
        contributions=tuple(
            CapabilityContribution(
                contribution_id=item.contribution_id,
                slot=item.slot,
                entrypoint=item.entrypoint,
                config_schema=item.config_schema,
                metadata={
                    **item.metadata,
                    _IMPLEMENTATION_HASH_KEY: implementation_hash,
                },
            )
            for item in declarations
        ),
    )


def plugin_promotion_candidate(
    manifest: PluginManifest,
    implementation_hash: str,
) -> CapabilityPromotionCandidate:
    """Create the candidate identity before plugin code can execute."""
    bundle = plugin_capability_bundle(manifest, implementation_hash)
    payload = bundle.model_dump(mode="json")
    if not bundle.contributions:
        payload["implementation_hash"] = implementation_hash
    return CapabilityPromotionCandidate.create(
        provider_id=bundle.provider_id,
        provider_kind=bundle.provider_kind,
        version=bundle.version,
        bundle_payload=payload,
    )


class GenerationRegistry(CoreGenerationRegistry):
    """Backward-compatible registry exposing plugin-specific helpers."""

    async def activate(
        self,
        manifest: PluginManifest,
        factory: PluginContributionFactory,
    ) -> RegistrySnapshot:
        """Adapt a plugin manifest into the provider-neutral catalog."""
        return await activate_plugin_bundle(self, manifest, factory)

    async def deactivate(self, plugin_id: str) -> RegistrySnapshot:
        """Compatibility alias for removing one plugin provider."""
        return await self.deactivate_provider(plugin_id)


__all__ = [
    "ActivatedContribution",
    "ActivationError",
    "GenerationLease",
    "GenerationRegistry",
    "ProviderDeactivationError",
    "RegistrySnapshot",
    "ReleaseRollbackError",
    "activate_plugin_bundle",
    "plugin_capability_bundle",
    "plugin_implementation_hash",
    "plugin_promotion_candidate",
]
