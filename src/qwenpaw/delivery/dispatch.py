# -*- coding: utf-8 -*-
"""Generation-pinned delivery execution over durable attempt leases."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from ..kernel import (
    DeliveryAdapter,
    DeliveryAttempt,
    DeliveryAttemptStatus,
    DeliveryMode,
    DeliveryProjectionPort,
    DeliveryReceipt,
    DeliveryRequest,
    DeliveryStatus,
)
from ..kernel.ports import CapabilityResolver, ExecutableCapabilityLease


class DeliveryDispatchDisposition(str, Enum):
    """Outcome of one delivery attempt admission."""

    DELIVERED = "delivered"
    SUPPRESSED = "suppressed"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    OWNED_ELSEWHERE = "owned_elsewhere"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True)
class DeliveryDispatchResult:
    """Authoritative durable attempt and its dispatch outcome."""

    attempt: DeliveryAttempt
    disposition: DeliveryDispatchDisposition


class DeliveryDispatchAccountingError(RuntimeError):
    """Raised when an adapter failure cannot be durably recorded."""


class DeliveryDispatcher:
    """Claim, resolve, execute, and settle one pinned delivery attempt."""

    def __init__(
        self,
        *,
        projection: DeliveryProjectionPort,
        capability_resolver: CapabilityResolver,
        lease_seconds: float = 30.0,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("delivery lease duration must be positive")
        self._projection = projection
        self._capability_resolver = capability_resolver
        self._lease_seconds = lease_seconds

    async def dispatch(
        self,
        request: DeliveryRequest,
        *,
        attempt: int,
        owner_id: str,
    ) -> DeliveryDispatchResult:
        """Execute an explicitly numbered attempt exactly once."""
        claimed = await self._projection.claim(
            request,
            attempt=attempt,
            owner_id=owner_id,
            lease_seconds=self._lease_seconds,
        )
        if claimed.status is not DeliveryAttemptStatus.CLAIMED:
            return DeliveryDispatchResult(
                attempt=claimed,
                disposition=DeliveryDispatchDisposition.REPLAYED,
            )
        if claimed.owner_id != owner_id:
            return DeliveryDispatchResult(
                attempt=claimed,
                disposition=DeliveryDispatchDisposition.OWNED_ELSEWHERE,
            )

        if request.mode is DeliveryMode.SILENT:
            receipt = self._receipt(
                request,
                attempt,
                DeliveryStatus.SUPPRESSED,
            )
            settled = await self._settle(claimed, receipt, owner_id)
            return DeliveryDispatchResult(
                attempt=settled,
                disposition=DeliveryDispatchDisposition.SUPPRESSED,
            )

        generation_lease = None
        try:
            generation_lease = await self._capability_resolver.pin(
                request.registry_generation,
            )
            adapter = self._resolve_adapter(
                generation_lease,
                request,
            )
        except Exception as error:  # pylint: disable=broad-except
            receipt = self._receipt(
                request,
                attempt,
                DeliveryStatus.FAILED,
                error_code=type(error).__name__[:100],
            )
        else:
            try:
                receipt = await adapter.deliver(request, attempt=attempt)
            except Exception as error:  # pylint: disable=broad-except
                receipt = self._receipt(
                    request,
                    attempt,
                    DeliveryStatus.UNCERTAIN,
                    error_code=type(error).__name__[:100],
                )
            if not self._valid_receipt(request, attempt, receipt):
                receipt = self._receipt(
                    request,
                    attempt,
                    DeliveryStatus.UNCERTAIN,
                    error_code="invalid_adapter_receipt",
                )
        finally:
            if generation_lease is not None:
                await generation_lease.close()

        settled = await self._settle(claimed, receipt, owner_id)
        dispositions = {
            DeliveryStatus.DELIVERED: DeliveryDispatchDisposition.DELIVERED,
            DeliveryStatus.SUPPRESSED: (
                DeliveryDispatchDisposition.SUPPRESSED
            ),
            DeliveryStatus.FAILED: DeliveryDispatchDisposition.FAILED,
            DeliveryStatus.UNCERTAIN: DeliveryDispatchDisposition.UNCERTAIN,
        }
        return DeliveryDispatchResult(
            attempt=settled,
            disposition=dispositions[receipt.status],
        )

    async def wait_for_terminal(
        self,
        delivery_id: UUID,
        *,
        attempt: int,
        timeout_seconds: float = 5.0,
    ) -> DeliveryAttempt:
        """Wait for another owner to settle one durable attempt."""
        if timeout_seconds <= 0:
            raise ValueError("delivery wait timeout must be positive")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            attempts = await self._projection.list_attempts(delivery_id)
            current = next(
                (item for item in attempts if item.attempt == attempt),
                None,
            )
            if current is not None and current.receipt is not None:
                return current
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(
                    "delivery attempt did not reach a terminal receipt",
                )
            await asyncio.sleep(min(0.01, remaining))

    def _resolve_adapter(
        self,
        lease,
        request: DeliveryRequest,
    ) -> DeliveryAdapter:
        adapter_id = request.destination.adapter_id
        descriptor = lease.resolve(adapter_id)
        if descriptor is None or descriptor.slot != "delivery.adapter":
            raise LookupError(f"delivery adapter unavailable: {adapter_id}")
        if not isinstance(lease, ExecutableCapabilityLease):
            raise LookupError("delivery resolver has no implementations")
        implementation = lease.implementation(adapter_id)
        if not isinstance(implementation, DeliveryAdapter):
            raise TypeError(f"invalid delivery adapter: {adapter_id}")
        if implementation.adapter_id != adapter_id:
            raise ValueError(
                f"delivery adapter identity mismatch: {adapter_id}",
            )
        if not implementation.supports(request):
            raise ValueError(f"delivery destination unsupported: {adapter_id}")
        return implementation

    async def _settle(
        self,
        claimed: DeliveryAttempt,
        receipt: DeliveryReceipt,
        owner_id: str,
    ) -> DeliveryAttempt:
        try:
            return await self._projection.settle(
                receipt,
                owner_id=owner_id,
                expected_revision=claimed.revision,
            )
        except Exception as error:
            raise DeliveryDispatchAccountingError(
                "delivery receipt could not be persisted",
            ) from error

    @staticmethod
    def _valid_receipt(
        request: DeliveryRequest,
        attempt: int,
        receipt: DeliveryReceipt,
    ) -> bool:
        return (
            receipt.delivery_id == request.delivery_id
            and receipt.adapter_id == request.destination.adapter_id
            and receipt.attempt == attempt
        )

    @staticmethod
    def _receipt(
        request: DeliveryRequest,
        attempt: int,
        status: DeliveryStatus,
        *,
        error_code: str = "",
    ) -> DeliveryReceipt:
        delivery_id = request.delivery_id
        if delivery_id is None:  # pragma: no cover - Kernel validator
            raise ValueError("delivery request has no stable identity")
        return DeliveryReceipt(
            delivery_id=delivery_id,
            adapter_id=request.destination.adapter_id,
            status=status,
            attempt=attempt,
            error_code=error_code,
        )


__all__ = [
    "DeliveryDispatchAccountingError",
    "DeliveryDispatchDisposition",
    "DeliveryDispatchResult",
    "DeliveryDispatcher",
]
