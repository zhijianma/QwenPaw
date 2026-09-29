# -*- coding: utf-8 -*-
"""Workspace assembly for durable non-Task operational notifications."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..delivery import DeliveryDispatcher, SQLiteDeliveryProjectionStore
from ..inbox import SQLiteInboxProjectionStore
from ..kernel import OperationalEvent
from ..operations import (
    OperationalDeliveryResult,
    OperationalDeliveryService,
    SQLiteOperationalEventStore,
)
from ..tasks.system_contributions import (
    SYSTEM_CAPABILITY_BUNDLE,
    system_contribution_factory,
)


async def operational_delivery_service_for_workspace(
    workspace: Any,
) -> OperationalDeliveryService:
    """Assemble one generation-aware operational delivery service."""
    registry = workspace.capability_registry

    async def resolve_workspace(agent_id: str):
        if agent_id != workspace.agent_id:
            raise LookupError(agent_id)
        return workspace

    await registry.ensure_bundle(
        SYSTEM_CAPABILITY_BUNDLE,
        system_contribution_factory(resolve_workspace),
    )
    data_dir = Path(workspace.workspace_dir) / ".qwenpaw" / "lite"
    return OperationalDeliveryService(
        events=SQLiteOperationalEventStore(data_dir / "operations.db"),
        dispatcher=DeliveryDispatcher(
            projection=SQLiteDeliveryProjectionStore(
                data_dir / "delivery.db",
            ),
            capability_resolver=registry,
        ),
        inbox=SQLiteInboxProjectionStore(data_dir / "inbox.db"),
    )


async def publish_operational_event(
    workspace: Any,
    *,
    producer_id: str,
    source_type: str,
    source_id: str,
    event_type: str,
    status: str,
    severity: str,
    title: str,
    body: str,
    payload: dict[str, Any],
) -> OperationalDeliveryResult:
    """Commit and project one content-addressed operational fact."""
    identity_payload = {
        "event_type": event_type,
        "status": status,
        "severity": severity,
        "title": title,
        "body": body,
        "payload": payload,
    }
    identity = json.dumps(
        identity_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    service = await operational_delivery_service_for_workspace(workspace)
    return await service.publish(
        OperationalEvent(
            agent_id=workspace.agent_id,
            producer_id=producer_id,
            event_type=event_type,
            idempotency_key=f"{event_type}:{digest}",
            registry_generation=workspace.capability_registry.generation,
            source_type=source_type,
            source_id=source_id,
            status=status,
            severity=severity,
            title=title,
            body=body,
            payload=payload,
        ),
    )


def operational_event_publisher_for_workspace(
    workspace: Any,
    *,
    producer_id: str,
) -> Callable[..., Awaitable[OperationalEvent]]:
    """Bind a producer to one Agent-owned Workspace publisher."""

    async def publish(
        *,
        agent_id: str,
        source_type: str,
        source_id: str,
        event_type: str,
        status: str,
        severity: str,
        title: str,
        body: str,
        payload: dict[str, Any],
    ) -> OperationalEvent:
        if agent_id != workspace.agent_id:
            raise ValueError(
                "operational event agent does not own the workspace",
            )
        result = await publish_operational_event(
            workspace,
            producer_id=producer_id,
            source_type=source_type,
            source_id=source_id,
            event_type=event_type,
            status=status,
            severity=severity,
            title=title,
            body=body,
            payload=payload,
        )
        return result.event

    return publish


__all__ = [
    "operational_event_publisher_for_workspace",
    "operational_delivery_service_for_workspace",
    "publish_operational_event",
]
