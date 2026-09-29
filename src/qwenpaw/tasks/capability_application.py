# -*- coding: utf-8 -*-
"""Transport-neutral capability catalog queries for Tasks."""

from __future__ import annotations

from dataclasses import dataclass

from ..kernel.models import CapabilityDescriptor
from ..kernel.ports import CapabilityResolver


@dataclass(frozen=True, slots=True)
class TaskCapabilityCatalog:
    """Descriptors read from one immutable registry generation."""

    registry_generation: int
    items: tuple[CapabilityDescriptor, ...]


class TaskCapabilityApplicationService:
    """Read Task capabilities without exposing generation leases."""

    def __init__(self, resolver: CapabilityResolver) -> None:
        self._resolver = resolver

    async def list(self, *, slot: str) -> TaskCapabilityCatalog:
        """Return one current-generation capability catalog."""
        lease = await self._resolver.pin()
        try:
            return TaskCapabilityCatalog(
                registry_generation=lease.generation,
                items=tuple(lease.descriptors(slot)),
            )
        finally:
            await lease.close()


__all__ = [
    "TaskCapabilityApplicationService",
    "TaskCapabilityCatalog",
]
