# -*- coding: utf-8 -*-
"""Plugin compatibility adapters for the OS capability registry."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable

from ..capabilities.registry import (
    ActivatedContribution,
    ActivationError,
    GenerationLease,
    GenerationRegistry as CoreGenerationRegistry,
    RegistrySnapshot,
)
from ..kernel.models import (
    CapabilityBundle,
    CapabilityContribution,
    CapabilityProviderKind,
    PluginContribution,
)
from .architecture import PluginManifest
from .contributions import (
    validate_contribution_implementation,
    validate_contributions,
)

PluginContributionFactory = Callable[
    [PluginContribution],
    object | Awaitable[object],
]


async def activate_plugin_bundle(
    registry: CoreGenerationRegistry,
    manifest: PluginManifest,
    factory: PluginContributionFactory,
) -> RegistrySnapshot:
    """Validate a plugin manifest and publish its common capability bundle."""
    declarations = validate_contributions(manifest)
    declaration_by_id = {item.contribution_id: item for item in declarations}
    bundle = CapabilityBundle(
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
                metadata=item.metadata,
            )
            for item in declarations
        ),
    )

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

    return await registry.activate_bundle(bundle, adapter)


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
    "RegistrySnapshot",
    "activate_plugin_bundle",
]
