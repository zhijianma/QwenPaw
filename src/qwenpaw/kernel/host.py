# -*- coding: utf-8 -*-
"""Stable host protocol negotiation contracts for QwenPaw clients."""

from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import Field

from .models import KernelModel

# Capability identifiers are intentionally open-ended. Older SDKs must be able
# to negotiate with newer Hosts that publish additional optional features.
HostFeature: TypeAlias = str

HOST_PROTOCOL_VERSION = 1
HOST_FEATURES: tuple[str, ...] = (
    "artifact.references",
    "capability.catalog",
    "chat.control.v1",
    "chat.evidence.v1",
    "chat.execution-manifests.v1",
    "chat.fork.v1",
    "chat.interactions",
    "chat.runtime.v1",
    "task.event-cursor",
    "task.runtime",
)


class HostHandshake(KernelModel):
    """Versioned, transport-neutral capabilities exposed by one Host."""

    schema_id: Literal["qwenpaw.host-handshake.v1"] = Field(
        default="qwenpaw.host-handshake.v1",
        alias="schema",
    )
    product: Literal["qwenpaw"] = "qwenpaw"
    version: str = Field(min_length=1)
    protocol_version: Literal[1] = HOST_PROTOCOL_VERSION
    features: tuple[HostFeature, ...] = HOST_FEATURES


__all__ = [
    "HOST_FEATURES",
    "HOST_PROTOCOL_VERSION",
    "HostFeature",
    "HostHandshake",
]
