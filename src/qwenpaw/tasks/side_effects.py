# -*- coding: utf-8 -*-
"""Run-scoped adapter for durable side-effect records."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from ..kernel.models import (
    SideEffectRecord,
    SideEffectReservation,
    SideEffectStatus,
    ToolEffect,
)
from .service import TaskService


@dataclass(frozen=True, slots=True)
class TaskSideEffectBroker:
    """Bind side-effect persistence to one active Task run."""

    service: TaskService
    task_id: UUID
    run_id: UUID

    async def begin(
        self,
        *,
        action: str,
        target: str,
        effect: ToolEffect,
        idempotency_key: str,
        request_hash: str,
        invocation_id: UUID | None = None,
        correlation_id: UUID | None = None,
        approval_id: UUID | None = None,
        policy_decision: str = "",
    ) -> SideEffectReservation:
        """Reserve one operation against this broker's active run."""
        return await self.service.begin_side_effect(
            self.task_id,
            self.run_id,
            action=action,
            target=target,
            effect=effect,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            invocation_id=invocation_id,
            correlation_id=correlation_id,
            approval_id=approval_id,
            policy_decision=policy_decision,
        )

    async def finish(
        self,
        record_id: UUID,
        *,
        status: SideEffectStatus,
        result_digest: str | None = None,
        external_ref: str = "",
        error_code: str = "",
    ) -> SideEffectRecord:
        """Commit one terminal outcome through the Task service."""
        return await self.service.finish_side_effect(
            record_id,
            status=status,
            result_digest=result_digest,
            external_ref=external_ref,
            error_code=error_code,
        )


__all__ = ["TaskSideEffectBroker"]
