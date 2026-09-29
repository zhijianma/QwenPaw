# -*- coding: utf-8 -*-
"""Runtime host for approval-gated sensor contributions."""

from __future__ import annotations

import json

from ..kernel.models import JsonObject, Proposal, SensorContext, Task
from ..kernel.ports import (
    CapabilityLease,
    CapabilityResolver,
    ContextualProposalSensor,
    ExecutableCapabilityLease,
    ProposalSensor,
)
from .proposals import persist_sensor_proposals
from .service import TaskService

MAX_SENSOR_PROPOSALS = 25
MAX_SENSOR_TRIGGER_BYTES = 32 * 1024


class SensorCapabilityUnavailableError(LookupError):
    """Raised when a pinned generation cannot supply a requested sensor."""

    def __init__(self, capability_id: str, reason: str) -> None:
        self.capability_id = capability_id
        self.reason = reason
        super().__init__(
            f"sensor capability '{capability_id}' is unavailable: {reason}",
        )


class SensorProposalLimitError(ValueError):
    """Raised before persistence when one sensor exceeds the batch limit."""


class SensorTriggerPayloadLimitError(ValueError):
    """Raised before polling when a trigger exceeds the public boundary."""


class SensorContributionHost:
    """Resolve one sensor generation and route proposals through approval."""

    def __init__(
        self,
        service: TaskService,
        capability_resolver: CapabilityResolver,
    ) -> None:
        self._service = service
        self._capability_resolver = capability_resolver

    async def poll(
        self,
        capability_id: str,
        *,
        agent_id: str,
        trigger_payload: JsonObject | None = None,
    ) -> tuple[Task, ...]:
        """Poll and persist a bounded proposal batch from one generation."""
        lease = await self._capability_resolver.pin()
        try:
            sensor = self._resolve_sensor(lease, capability_id)
            context = SensorContext(
                agent_id=agent_id,
                registry_generation=lease.generation,
                trigger_payload=trigger_payload or {},
            )
            encoded_trigger = json.dumps(
                context.trigger_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            if len(encoded_trigger) > MAX_SENSOR_TRIGGER_BYTES:
                raise SensorTriggerPayloadLimitError(
                    "sensor trigger payload exceeds 32 KiB",
                )
            proposals = tuple(
                await sensor.propose_context(context)
                if isinstance(sensor, ContextualProposalSensor)
                else await sensor.propose(),
            )
            if len(proposals) > MAX_SENSOR_PROPOSALS:
                raise SensorProposalLimitError(
                    f"sensor returned more than {MAX_SENSOR_PROPOSALS} "
                    "proposals",
                )
            if any(not isinstance(item, Proposal) for item in proposals):
                raise SensorCapabilityUnavailableError(
                    capability_id,
                    "implementation returned a non-Proposal value",
                )
            if any(item.source != capability_id for item in proposals):
                raise SensorCapabilityUnavailableError(
                    capability_id,
                    "proposal source does not match Sensor identity",
                )
            self._service.set_registry_generation(lease.generation)
            return await persist_sensor_proposals(
                self._service,
                agent_id=agent_id,
                sensor_id=capability_id,
                proposals=proposals,
                registry_generation=lease.generation,
            )
        finally:
            await lease.close()

    @staticmethod
    def _resolve_sensor(
        lease: CapabilityLease,
        capability_id: str,
    ) -> ProposalSensor:
        descriptor = lease.resolve(capability_id)
        if descriptor is None:
            raise SensorCapabilityUnavailableError(
                capability_id,
                "not present in the pinned registry generation",
            )
        if descriptor.slot != "sensor":
            raise SensorCapabilityUnavailableError(
                capability_id,
                f"declares slot '{descriptor.slot}' instead of 'sensor'",
            )
        if not isinstance(lease, ExecutableCapabilityLease):
            raise SensorCapabilityUnavailableError(
                capability_id,
                "resolver does not expose executable implementations",
            )
        implementation = lease.implementation(capability_id)
        if not isinstance(implementation, ProposalSensor):
            raise SensorCapabilityUnavailableError(
                capability_id,
                "implementation does not satisfy ProposalSensor",
            )
        if implementation.sensor_id != capability_id:
            raise SensorCapabilityUnavailableError(
                capability_id,
                "implementation sensor_id does not match capability ID",
            )
        return implementation
