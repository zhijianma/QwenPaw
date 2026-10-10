# -*- coding: utf-8 -*-
"""Stable host protocol negotiation contracts for QwenPaw clients."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .models import KernelModel

HostFeature = Literal[
    "artifact.references",
    "capability.catalog",
    "chat.interactions",
    "task.event-cursor",
    "task.runtime",
]

HOST_PROTOCOL_VERSION = 1
HOST_FEATURES: tuple[HostFeature, ...] = (
    "artifact.references",
    "capability.catalog",
    "chat.interactions",
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
