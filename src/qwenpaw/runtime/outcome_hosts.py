# -*- coding: utf-8 -*-
"""Invocation-bound Outcome Host assembly."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from ..kernel import (
    CapabilityProviderKind,
    ConversationOutcome,
    ConversationOutcomeDeclaration,
    ConversationOutcomeRequest,
    ConversationOutcomeStore,
    InvocationScope,
    OutcomeProducerRegistration,
)
from ..kernel.models import utc_now
from ..tasks.conversation_artifacts import conversation_artifact_receipts
from ..tasks.ledger import SQLiteExecutionLedger
from .outcome_broker import HostOutcomeBroker, OutcomeProducerLease
from .outcomes import lite_conversation_outcome_store


@dataclass(frozen=True)
class InvocationOutcomeHost:
    """Bind provider requests to one immutable Invocation identity."""

    _broker: HostOutcomeBroker
    _scope: InvocationScope
    _producer_lease: OutcomeProducerLease

    async def declare(
        self,
        request: ConversationOutcomeRequest,
    ) -> ConversationOutcome:
        """Submit without allowing a provider to forge ownership fields."""
        if self._scope.chat_id is None:
            raise ValueError("outcome requires a stable ChatSpec.id")
        if self._scope.correlation_id is None:
            raise ValueError("outcome requires a correlation identity")
        if request.declared_at < self._scope.started_at:
            raise ValueError("outcome cannot predate its Invocation")
        if request.declared_at > utc_now() + timedelta(seconds=5):
            raise ValueError("outcome declaration cannot be future dated")
        return await self._broker.declare_bound(
            self._producer_lease,
            ConversationOutcomeDeclaration(
                outcome_id=request.outcome_id,
                agent_id=self._scope.agent_id,
                chat_id=self._scope.chat_id,
                correlation_id=self._scope.correlation_id,
                status=request.status,
                producer_id=(self._producer_lease.registration.producer_id),
                summary=request.summary,
                invocation_id=self._scope.invocation_id,
                registry_generation=self._scope.registry_generation,
                task_id=request.task_id,
                run_id=request.run_id,
                artifact_ids=request.artifact_ids,
                evidence_ids=request.evidence_ids,
                verification_ids=request.verification_ids,
                supersedes_outcome_id=request.supersedes_outcome_id,
                declared_at=request.declared_at,
            ),
        )


def workspace_outcome_broker(
    workspace: Any,
    *,
    workspace_dir: str | Path | None = None,
) -> HostOutcomeBroker:
    """Return one Workspace-scoped Broker over shared Lite authorities."""
    broker = getattr(workspace, "outcome_broker", None)
    if isinstance(broker, HostOutcomeBroker):
        return broker
    raw_workspace_dir = workspace_dir or getattr(
        workspace,
        "workspace_dir",
        None,
    )
    if raw_workspace_dir is None:
        raise ValueError("outcome broker requires a workspace directory")
    resolved_workspace_dir = Path(raw_workspace_dir)
    task_ledger = SQLiteExecutionLedger(
        resolved_workspace_dir / ".qwenpaw" / "lite" / "tasks.db",
    )
    existing_store = getattr(workspace, "conversation_outcome_store", None)
    outcome_store = (
        existing_store
        if isinstance(existing_store, ConversationOutcomeStore)
        else lite_conversation_outcome_store(resolved_workspace_dir)
    )
    broker = HostOutcomeBroker(
        store=outcome_store,
        conversation_artifacts=conversation_artifact_receipts(
            resolved_workspace_dir,
        ),
        tasks=task_ledger,
        task_events=task_ledger,
    )
    setattr(workspace, "conversation_outcome_store", outcome_store)
    setattr(workspace, "outcome_broker", broker)
    return broker


def provider_outcome_host(
    workspace: Any,
    scope: InvocationScope,
    *,
    producer_id: str,
    provider_kind: CapabilityProviderKind,
) -> InvocationOutcomeHost | None:
    """Bind an admitted provider from the pinned capability generation."""
    broker = workspace_outcome_broker(
        workspace,
        workspace_dir=scope.workspace_dir,
    )
    if provider_kind is CapabilityProviderKind.SYSTEM:
        broker.register(
            OutcomeProducerRegistration(
                producer_id=producer_id,
                provider_kind=provider_kind,
            ),
        )
    producer_lease = broker.bind(producer_id)
    if producer_lease is None:
        return None
    return InvocationOutcomeHost(broker, scope, producer_lease)


__all__ = [
    "InvocationOutcomeHost",
    "provider_outcome_host",
    "workspace_outcome_broker",
]
