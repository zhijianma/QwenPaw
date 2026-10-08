# -*- coding: utf-8 -*-
"""Transport-neutral communication capability contracts."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import Field, model_validator

from .models import KernelModel, NamespacedId


class CommunicationDeliveryMode(str, Enum):
    """Observable delivery semantics, independent from one protocol."""

    REQUEST_RESPONSE = "request_response"
    REQUEST_STREAM = "request_stream"
    DURABLE_HANDLE = "durable_handle"
    DURABLE_CHANNEL = "durable_channel"


class CommunicationOrderingScope(str, Enum):
    """Largest scope in which an Adapter preserves ordering."""

    NONE = "none"
    CONNECTION = "connection"
    CONVERSATION = "conversation"
    CORRELATION = "correlation"
    GLOBAL = "global"


class CommunicationIdempotency(str, Enum):
    """Whether mutation replay requires a stable caller key."""

    NOT_APPLICABLE = "not_applicable"
    OPTIONAL = "optional"
    REQUIRED = "required"


class CommunicationCursorSemantics(str, Enum):
    """What a cursor can recover after transport disconnection."""

    NONE = "none"
    SNAPSHOT_CHANGE = "snapshot_change"
    REPLAY_OFFSET = "replay_offset"


class CommunicationRetention(str, Enum):
    """Retention guarantee attached to one delivery capability."""

    EPHEMERAL = "ephemeral"
    LATEST_STATE = "latest_state"
    DURABLE = "durable"


class CommunicationBackpressure(str, Enum):
    """Backpressure behavior exposed by an Adapter."""

    NONE = "none"
    COALESCE_LATEST = "coalesce_latest"
    SERVER_QUEUE = "server_queue"
    ACKNOWLEDGED = "acknowledged"


class CommunicationDisconnectPolicy(str, Enum):
    """Execution behavior after the client connection disappears."""

    CANCEL = "cancel"
    CONTINUE = "continue"
    RECONNECT_SNAPSHOT = "reconnect_snapshot"
    RESUME_CURSOR = "resume_cursor"


class CommunicationCapability(KernelModel):
    """One truthful transport capability exposed by an Adapter."""

    schema_id: Literal["qwenpaw.communication-capability.v1"] = Field(
        default="qwenpaw.communication-capability.v1",
        alias="schema",
    )
    capability_id: NamespacedId
    delivery_mode: CommunicationDeliveryMode
    ordering_scope: CommunicationOrderingScope
    idempotency: CommunicationIdempotency
    cursor_semantics: CommunicationCursorSemantics
    retention: CommunicationRetention
    backpressure: CommunicationBackpressure
    disconnect_policy: CommunicationDisconnectPolicy

    @model_validator(mode="after")
    def validate_semantics(self) -> "CommunicationCapability":
        """Reject capability combinations that overstate recovery."""
        if (
            self.cursor_semantics
            is CommunicationCursorSemantics.REPLAY_OFFSET
            and self.retention is not CommunicationRetention.DURABLE
        ):
            raise ValueError("replay cursor requires durable retention")
        if (
            self.disconnect_policy
            is CommunicationDisconnectPolicy.RESUME_CURSOR
            and self.cursor_semantics
            is not CommunicationCursorSemantics.REPLAY_OFFSET
        ):
            raise ValueError("cursor resume requires replay semantics")
        if (
            self.disconnect_policy
            is CommunicationDisconnectPolicy.RECONNECT_SNAPSHOT
            and self.cursor_semantics
            is not CommunicationCursorSemantics.SNAPSHOT_CHANGE
        ):
            raise ValueError(
                "snapshot reconnect requires snapshot-change cursor",
            )
        if self.delivery_mode is CommunicationDeliveryMode.DURABLE_CHANNEL:
            durable_storage_missing = (
                self.retention is not CommunicationRetention.DURABLE
                or self.cursor_semantics
                is not CommunicationCursorSemantics.REPLAY_OFFSET
                or self.backpressure
                is not CommunicationBackpressure.ACKNOWLEDGED
            )
            durable_dispatch_missing = (
                self.disconnect_policy
                is not CommunicationDisconnectPolicy.RESUME_CURSOR
                or self.idempotency
                is not CommunicationIdempotency.REQUIRED
            )
            if durable_storage_missing or durable_dispatch_missing:
                raise ValueError(
                    "durable channel requires replay and acknowledgements",
                )
        if (
            self.delivery_mode
            is CommunicationDeliveryMode.DURABLE_HANDLE
            and (
                self.retention is not CommunicationRetention.DURABLE
                or self.disconnect_policy
                is not CommunicationDisconnectPolicy.CONTINUE
                or self.idempotency
                is not CommunicationIdempotency.REQUIRED
            )
        ):
            raise ValueError(
                "durable handle must outlive the client connection",
            )
        return self


class CommunicationContract(KernelModel):
    """Versioned capabilities for one concrete communication Adapter."""

    schema_id: Literal["qwenpaw.communication-contract.v1"] = Field(
        default="qwenpaw.communication-contract.v1",
        alias="schema",
    )
    contract_id: NamespacedId
    capabilities: tuple[CommunicationCapability, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_capabilities(self) -> "CommunicationContract":
        """Require stable, unique identities within one Adapter contract."""
        identities = [item.capability_id for item in self.capabilities]
        if len(identities) != len(set(identities)):
            raise ValueError("communication capabilities must be unique")
        return self


__all__ = [
    "CommunicationBackpressure",
    "CommunicationCapability",
    "CommunicationContract",
    "CommunicationCursorSemantics",
    "CommunicationDeliveryMode",
    "CommunicationDisconnectPolicy",
    "CommunicationIdempotency",
    "CommunicationOrderingScope",
    "CommunicationRetention",
]
