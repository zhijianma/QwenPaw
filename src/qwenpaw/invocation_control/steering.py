# -*- coding: utf-8 -*-
"""In-process delivery mailbox for durable steer commands."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass
from uuid import UUID

from ..kernel import ControlCommand, ControlCommandKind, SteerSafePoint


class SteeringMailboxError(RuntimeError):
    """Base class for live steer-delivery failures."""


class SteerInvocationUnavailableError(SteeringMailboxError):
    """Raised when no live runtime owns the target invocation."""


class SteerDeliveryConflictError(SteeringMailboxError):
    """Raised when a claimed command is acknowledged inconsistently."""


@dataclass(frozen=True, slots=True)
class SteerDelivery:
    """One command claimed by a runtime at a candidate safe point."""

    command_id: UUID
    invocation_id: UUID
    instruction: str


@dataclass(slots=True)
class _SteerEntry:
    command: ControlCommand
    result: asyncio.Future[SteerSafePoint]
    claimed: bool = False


class SteeringMailbox:
    """Route accepted steer commands to one active runtime invocation."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._active: set[UUID] = set()
        self._pending: dict[UUID, deque[UUID]] = defaultdict(deque)
        self._entries: dict[UUID, _SteerEntry] = {}

    async def bind(self, invocation_id: UUID) -> None:
        """Declare that one Runtime can now consume steer commands."""
        async with self._lock:
            self._active.add(invocation_id)

    async def unbind(
        self,
        invocation_id: UUID,
        *,
        reason: str = "invocation closed before steer was applied",
    ) -> None:
        """Close one Runtime binding and fail all undelivered commands."""
        async with self._lock:
            self._active.discard(invocation_id)
            command_ids = tuple(self._pending.pop(invocation_id, ()))
            command_ids += tuple(
                command_id
                for command_id, entry in self._entries.items()
                if entry.command.target_invocation_id == invocation_id
                and entry.claimed
            )
            for command_id in command_ids:
                entry = self._entries.pop(command_id, None)
                if entry is not None and not entry.result.done():
                    entry.result.set_exception(
                        SteerInvocationUnavailableError(reason),
                    )

    async def offer(
        self,
        command: ControlCommand,
    ) -> asyncio.Future[SteerSafePoint]:
        """Offer one durable steer and return its application future."""
        if command.kind is not ControlCommandKind.STEER:
            raise TypeError("steering mailbox only accepts steer commands")
        invocation_id = command.target_invocation_id
        if invocation_id is None:
            raise TypeError("steer command has no target invocation")
        async with self._lock:
            existing = self._entries.get(command.command_id)
            if existing is not None:
                if existing.command != command:
                    raise SteerDeliveryConflictError(
                        "command identity has conflicting steer content",
                    )
                return existing.result
            if invocation_id not in self._active:
                raise SteerInvocationUnavailableError(
                    "target invocation is not active",
                )
            future = asyncio.get_running_loop().create_future()
            self._entries[command.command_id] = _SteerEntry(
                command=command,
                result=future,
            )
            self._pending[invocation_id].append(command.command_id)
            return future

    async def claim(
        self,
        invocation_id: UUID,
    ) -> SteerDelivery | None:
        """Claim the oldest steer without marking it applied."""
        async with self._lock:
            pending = self._pending.get(invocation_id)
            if not pending:
                return None
            command_id = pending.popleft()
            if not pending:
                self._pending.pop(invocation_id, None)
            entry = self._entries[command_id]
            entry.claimed = True
            instruction = entry.command.instruction
            if instruction is None:
                raise SteerDeliveryConflictError(
                    "steer command lost its instruction",
                )
            return SteerDelivery(
                command_id=command_id,
                invocation_id=invocation_id,
                instruction=instruction,
            )

    async def acknowledge(
        self,
        delivery: SteerDelivery,
        safe_point: SteerSafePoint,
    ) -> None:
        """Mark a steer applied only after context mutation succeeds."""
        async with self._lock:
            entry = self._entries.pop(delivery.command_id, None)
            if entry is None or not entry.claimed:
                raise SteerDeliveryConflictError(
                    "steer delivery is not currently claimed",
                )
            if entry.command.target_invocation_id != delivery.invocation_id:
                raise SteerDeliveryConflictError(
                    "steer delivery invocation does not match command",
                )
            if not entry.result.done():
                entry.result.set_result(safe_point)

    async def release(self, delivery: SteerDelivery) -> None:
        """Return a failed safe-point attempt to the front of its queue."""
        async with self._lock:
            entry = self._entries.get(delivery.command_id)
            if entry is None or not entry.claimed:
                raise SteerDeliveryConflictError(
                    "steer delivery is not currently claimed",
                )
            entry.claimed = False
            self._pending[delivery.invocation_id].appendleft(
                delivery.command_id,
            )


__all__ = [
    "SteerDelivery",
    "SteerDeliveryConflictError",
    "SteerInvocationUnavailableError",
    "SteeringMailbox",
    "SteeringMailboxError",
]
