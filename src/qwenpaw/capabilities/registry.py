# -*- coding: utf-8 -*-
"""Immutable capability generations with atomic provider activation."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from ..kernel.models import (
    CapabilityBundle,
    CapabilityContribution,
    CapabilityDescriptor,
)
from ..kernel.ports import CapabilityLease
from ..kernel.releases import CapabilityReleaseTag
from .contracts import (
    CapabilityImplementationError,
    validate_capability_implementation,
)

ContributionFactory = Callable[
    [CapabilityContribution],
    object | Awaitable[object],
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActivatedContribution:
    """One validated descriptor paired with its implementation."""

    descriptor: CapabilityDescriptor
    implementation: object


@dataclass(frozen=True)
class RegistrySnapshot:
    """Immutable registry state pinned by an active invocation."""

    generation: int
    capabilities: Mapping[str, ActivatedContribution]


class ActivationError(RuntimeError):
    """Raised when staging or health checks reject an activation."""


class ReleaseRollbackError(RuntimeError):
    """Raised when a stable provider release cannot be rolled back."""


@dataclass(frozen=True)
class _ProviderRollbackPoint:
    """Last promoted provider state retained for a fenced rollback."""

    promoted_release_hash: str
    contributions: Mapping[str, ActivatedContribution]


class GenerationLease(CapabilityLease):
    """Reference-counted lease over one immutable snapshot."""

    def __init__(
        self,
        registry: "GenerationRegistry",
        snapshot: RegistrySnapshot,
    ) -> None:
        self._registry = registry
        self._snapshot = snapshot
        self._closed = False

    @property
    def generation(self) -> int:
        """Return the pinned generation number."""
        return self._snapshot.generation

    def resolve(self, capability_id: str) -> CapabilityDescriptor | None:
        """Resolve a descriptor without exposing its implementation."""
        contribution = self._snapshot.capabilities.get(capability_id)
        return contribution.descriptor if contribution else None

    def implementation(self, capability_id: str) -> object | None:
        """Resolve the staged implementation for runtime dispatch."""
        contribution = self._snapshot.capabilities.get(capability_id)
        return contribution.implementation if contribution else None

    def descriptors(
        self,
        slot: str | None = None,
    ) -> tuple[CapabilityDescriptor, ...]:
        """List pinned descriptors in stable capability ID order."""
        descriptors = (
            contribution.descriptor
            for contribution in self._snapshot.capabilities.values()
        )
        return tuple(
            sorted(
                (
                    descriptor
                    for descriptor in descriptors
                    if slot is None or descriptor.slot == slot
                ),
                key=lambda descriptor: descriptor.capability_id,
            ),
        )

    async def close(self) -> None:
        """Release this lease exactly once."""
        if self._closed:
            return
        self._closed = True
        await self._registry.release(self.generation)


class GenerationRegistry:
    """Publish complete provider updates as one atomic generation."""

    def __init__(self) -> None:
        initial = RegistrySnapshot(
            generation=1,
            capabilities=MappingProxyType({}),
        )
        self._current = initial
        self._snapshots = {initial.generation: initial}
        self._leases = {initial.generation: 0}
        self._lock = asyncio.Lock()
        self._ensure_lock = asyncio.Lock()
        self._provider_cleanup: dict[str, list[Callable[[], None]]] = {}
        self._stable_releases: dict[str, CapabilityReleaseTag] = {}
        self._rollback_points: dict[str, _ProviderRollbackPoint] = {}

    @property
    def generation(self) -> int:
        """Return the currently published generation."""
        return self._current.generation

    def current_descriptor(
        self,
        capability_id: str,
    ) -> CapabilityDescriptor | None:
        """Return one descriptor from the current immutable snapshot."""
        contribution = self._current.capabilities.get(capability_id)
        return contribution.descriptor if contribution else None

    def stable_release(
        self,
        provider_id: str,
    ) -> CapabilityReleaseTag | None:
        """Return the current content-addressed stable provider tag."""
        return self._stable_releases.get(provider_id)

    async def pin(
        self,
        generation: int | None = None,
    ) -> GenerationLease:
        """Pin the current or one retained immutable generation."""
        async with self._lock:
            if generation is None:
                snapshot = self._current
            else:
                snapshot = self._snapshots.get(generation)
                if snapshot is None:
                    raise LookupError(
                        f"registry generation {generation} is unavailable",
                    )
            self._leases[snapshot.generation] += 1
        return GenerationLease(self, snapshot)

    async def ensure_bundle(
        self,
        bundle: CapabilityBundle,
        factory: ContributionFactory,
    ) -> RegistrySnapshot:
        """Publish a bundle only when its exact descriptors are absent."""
        async with self._ensure_lock:
            expected = {
                f"{bundle.provider_id}.{item.contribution_id}": item
                for item in bundle.contributions
            }
            current_provider_ids = {
                capability_id
                for capability_id, contribution in (
                    self._current.capabilities.items()
                )
                if contribution.descriptor.provider_id == bundle.provider_id
            }
            current_matches = current_provider_ids == set(expected) and all(
                (descriptor := self.current_descriptor(capability_id))
                is not None
                and descriptor.version == bundle.version
                and descriptor.slot == declaration.slot
                for capability_id, declaration in expected.items()
            )
            if current_matches:
                return self._current
            return await self.activate_bundle(bundle, factory)

    async def activate_bundle(
        self,
        bundle: CapabilityBundle,
        factory: ContributionFactory,
    ) -> RegistrySnapshot:
        """Stage and atomically publish a system or plugin bundle."""
        staged: dict[str, ActivatedContribution] = {}
        try:
            for declaration in bundle.contributions:
                implementation = factory(declaration)
                if inspect.isawaitable(implementation):
                    implementation = await implementation
                health_check = getattr(implementation, "health_check", None)
                if health_check is not None:
                    healthy = health_check()
                    if inspect.isawaitable(healthy):
                        healthy = await healthy
                    if healthy is not True:
                        raise ActivationError(
                            f"health check failed for "
                            f"{declaration.contribution_id}",
                        )
                validate_capability_implementation(
                    bundle.provider_id,
                    declaration,
                    implementation,
                )
                descriptor = CapabilityDescriptor(
                    capability_id=(
                        f"{bundle.provider_id}."
                        f"{declaration.contribution_id}"
                    ),
                    slot=declaration.slot,
                    provider_id=bundle.provider_id,
                    provider_kind=bundle.provider_kind,
                    version=bundle.version,
                    input_schema=declaration.input_schema,
                    output_schema=declaration.output_schema,
                    config_schema=declaration.config_schema,
                    restart_policy=bundle.restart_policy,
                    metadata=declaration.metadata,
                )
                staged[descriptor.capability_id] = ActivatedContribution(
                    descriptor=descriptor,
                    implementation=implementation,
                )
        except Exception as exc:
            if isinstance(exc, ActivationError):
                raise
            if isinstance(exc, CapabilityImplementationError):
                raise ActivationError(str(exc)) from exc
            raise ActivationError(
                f"failed to stage provider '{bundle.provider_id}'",
            ) from exc

        async with self._lock:
            previous = {
                capability_id: contribution
                for capability_id, contribution in (
                    self._current.capabilities.items()
                )
                if contribution.descriptor.provider_id == bundle.provider_id
            }
            capabilities = {
                capability_id: contribution
                for capability_id, contribution in (
                    self._current.capabilities.items()
                )
                if contribution.descriptor.provider_id != bundle.provider_id
            }
            capabilities.update(staged)
            generation = self._current.generation + 1
            snapshot = RegistrySnapshot(
                generation=generation,
                capabilities=MappingProxyType(capabilities),
            )
            descriptors = tuple(
                contribution.descriptor
                for contribution in staged.values()
            )
            release = CapabilityReleaseTag.create(
                provider_id=bundle.provider_id,
                provider_kind=bundle.provider_kind,
                version=bundle.version,
                promoted_generation=generation,
                descriptors=descriptors,
            )
            self._rollback_points[bundle.provider_id] = (
                _ProviderRollbackPoint(
                    promoted_release_hash=release.release_hash,
                    contributions=MappingProxyType(previous),
                )
            )
            self._stable_releases[bundle.provider_id] = release
            self._current = snapshot
            self._snapshots[generation] = snapshot
            self._leases[generation] = 0
            self._reclaim_unleased()
            return snapshot

    async def rollback_provider(
        self,
        provider_id: str,
        *,
        expected_release_hash: str,
    ) -> RegistrySnapshot:
        """Rollback one provider without reverting unrelated promotions."""
        async with self._lock:
            current_release = self._stable_releases.get(provider_id)
            point = self._rollback_points.get(provider_id)
            if current_release is None or point is None:
                raise ReleaseRollbackError(
                    f"provider '{provider_id}' has no rollback point",
                )
            if current_release.release_hash != expected_release_hash:
                raise ReleaseRollbackError(
                    f"provider '{provider_id}' stable release changed",
                )
            if point.promoted_release_hash != expected_release_hash:
                raise ReleaseRollbackError(
                    f"provider '{provider_id}' rollback point is stale",
                )

            capabilities = {
                capability_id: contribution
                for capability_id, contribution in (
                    self._current.capabilities.items()
                )
                if contribution.descriptor.provider_id != provider_id
            }
            capabilities.update(point.contributions)
            generation = self._current.generation + 1
            snapshot = RegistrySnapshot(
                generation=generation,
                capabilities=MappingProxyType(capabilities),
            )
            previous_descriptors = tuple(
                contribution.descriptor
                for contribution in point.contributions.values()
            )
            if previous_descriptors:
                descriptor = previous_descriptors[0]
                self._stable_releases[provider_id] = (
                    CapabilityReleaseTag.create(
                        provider_id=provider_id,
                        provider_kind=descriptor.provider_kind,
                        version=descriptor.version,
                        promoted_generation=generation,
                        descriptors=previous_descriptors,
                    )
                )
            else:
                self._stable_releases.pop(provider_id, None)
            self._rollback_points.pop(provider_id, None)
            self._current = snapshot
            self._snapshots[generation] = snapshot
            self._leases[generation] = 0
            self._reclaim_unleased()
            return snapshot

    async def release(self, generation: int) -> None:
        """Release one lease and reclaim obsolete unreferenced snapshots."""
        async with self._lock:
            count = self._leases.get(generation)
            if count is None or count < 1:
                return
            self._leases[generation] = count - 1
            self._reclaim_unleased()

    async def deactivate_provider(
        self,
        provider_id: str,
    ) -> RegistrySnapshot:
        """Atomically remove one provider from all future leases."""
        async with self._lock:
            capabilities = {
                capability_id: contribution
                for capability_id, contribution in (
                    self._current.capabilities.items()
                )
                if contribution.descriptor.provider_id != provider_id
            }
            if len(capabilities) == len(self._current.capabilities):
                return self._current
            generation = self._current.generation + 1
            snapshot = RegistrySnapshot(
                generation=generation,
                capabilities=MappingProxyType(capabilities),
            )
            self._current = snapshot
            self._stable_releases.pop(provider_id, None)
            self._rollback_points.pop(provider_id, None)
            self._snapshots[generation] = snapshot
            self._leases[generation] = 0
            self._reclaim_unleased()
            return snapshot

    def defer_provider_cleanup(
        self,
        provider_id: str,
        callback: Callable[[], None],
    ) -> None:
        """Run cleanup after no retained generation uses the provider."""
        if not self._provider_is_retained(provider_id):
            callback()
            return
        self._provider_cleanup.setdefault(provider_id, []).append(callback)

    def _reclaim_unleased(self) -> None:
        current = self._current.generation
        reclaimable = [
            generation
            for generation, count in self._leases.items()
            if generation != current and count == 0
        ]
        for generation in reclaimable:
            self._leases.pop(generation, None)
            self._snapshots.pop(generation, None)
        self._run_ready_provider_cleanup()

    def _provider_is_retained(self, provider_id: str) -> bool:
        return any(
            contribution.descriptor.provider_id == provider_id
            for snapshot in self._snapshots.values()
            for contribution in snapshot.capabilities.values()
        )

    def _run_ready_provider_cleanup(self) -> None:
        ready = [
            provider_id
            for provider_id in self._provider_cleanup
            if not self._provider_is_retained(provider_id)
        ]
        for provider_id in ready:
            callbacks = self._provider_cleanup.pop(provider_id, [])
            for callback in callbacks:
                try:
                    callback()
                except Exception:  # pylint: disable=broad-except
                    logger.exception(
                        "provider cleanup failed: %s",
                        provider_id,
                    )

    def retained_generations(self) -> tuple[int, ...]:
        """Return retained generations for structured diagnostics."""
        return tuple(sorted(self._snapshots))


__all__ = [
    "ActivatedContribution",
    "ActivationError",
    "ContributionFactory",
    "GenerationLease",
    "GenerationRegistry",
    "ReleaseRollbackError",
    "RegistrySnapshot",
]
