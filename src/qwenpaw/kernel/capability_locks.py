# -*- coding: utf-8 -*-
"""Immutable release evidence for generation-pinned invocations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Annotated, Literal, Self
from uuid import UUID, uuid5

from pydantic import (
    AliasChoices,
    AwareDatetime,
    Field,
    StringConstraints,
    model_validator,
)

from .models import (
    CapabilityProviderKind,
    KernelModel,
    NamespacedId,
    NonEmptyStr,
    RestartPolicy,
    utc_now,
)

Sha256Digest = Annotated[
    str,
    StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$"),
]


def _canonical_chat_id(
    chat_id: str | None,
    conversation_id: str | None,
) -> str | None:
    """Resolve ChatSpec.id while accepting the deprecated identity."""
    if (
        chat_id is not None
        and conversation_id is not None
        and chat_id != conversation_id
    ):
        raise ValueError("chat_id and conversation_id must identify one Chat")
    return chat_id if chat_id is not None else conversation_id


class CapabilityRelease(KernelModel):
    """Content-safe identity of one selected immutable descriptor."""

    schema_id: Literal["qwenpaw.capability-release.v1"] = Field(
        default="qwenpaw.capability-release.v1",
        alias="schema",
    )
    capability_id: NamespacedId
    slot: NamespacedId
    provider_id: NamespacedId
    provider_kind: CapabilityProviderKind
    version: NonEmptyStr
    restart_policy: RestartPolicy
    descriptor_hash: Sha256Digest


class CapabilityLockManifest(KernelModel):
    """Exact selected releases admitted for one Invocation."""

    schema_id: Literal["qwenpaw.capability-lock-manifest.v1"] = Field(
        default="qwenpaw.capability-lock-manifest.v1",
        alias="schema",
    )
    lock_id: UUID
    invocation_id: UUID
    correlation_id: UUID
    agent_id: NonEmptyStr
    chat_id: NonEmptyStr | None = Field(
        default=None,
        validation_alias=AliasChoices("chat_id", "conversation_id"),
        description="Owning ChatSpec.id",
    )
    registry_epoch_id: UUID | None = None
    registry_generation: int = Field(ge=1)
    releases: tuple[CapabilityRelease, ...] = Field(min_length=1)
    manifest_hash: Sha256Digest
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        *,
        invocation_id: UUID,
        correlation_id: UUID,
        agent_id: str,
        registry_generation: int,
        releases: tuple[CapabilityRelease, ...],
        chat_id: str | None = None,
        conversation_id: str | None = None,
        registry_epoch_id: UUID | None = None,
    ) -> Self:
        """Build one deterministic, self-verifying Invocation lock."""
        resolved_chat_id = _canonical_chat_id(chat_id, conversation_id)
        return cls(
            lock_id=uuid5(invocation_id, "capability-lock:v1"),
            invocation_id=invocation_id,
            correlation_id=correlation_id,
            agent_id=agent_id,
            chat_id=resolved_chat_id,
            registry_epoch_id=registry_epoch_id,
            registry_generation=registry_generation,
            releases=releases,
            manifest_hash=cls.calculate_manifest_hash(
                invocation_id=invocation_id,
                correlation_id=correlation_id,
                agent_id=agent_id,
                chat_id=resolved_chat_id,
                registry_epoch_id=registry_epoch_id,
                registry_generation=registry_generation,
                releases=releases,
            ),
        )

    @staticmethod
    def calculate_manifest_hash(
        *,
        invocation_id: UUID,
        correlation_id: UUID,
        agent_id: str,
        registry_generation: int,
        releases: tuple[CapabilityRelease, ...],
        chat_id: str | None = None,
        conversation_id: str | None = None,
        registry_epoch_id: UUID | None = None,
    ) -> str:
        """Hash stable identity and selected releases, excluding time."""
        resolved_chat_id = _canonical_chat_id(chat_id, conversation_id)
        identity = {
            "invocation_id": str(invocation_id),
            "correlation_id": str(correlation_id),
            "agent_id": agent_id,
            # Keep the v1 hash bytes stable for historical immutable locks.
            "conversation_id": resolved_chat_id,
            "registry_generation": registry_generation,
            "releases": [item.model_dump(mode="json") for item in releases],
        }
        if registry_epoch_id is not None:
            identity["registry_epoch_id"] = str(registry_epoch_id)
        encoded = json.dumps(
            identity,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    @model_validator(mode="before")
    @classmethod
    def validate_chat_identity(cls, value: object) -> object:
        """Reject ambiguous canonical and legacy Chat identities."""
        if isinstance(value, Mapping):
            _canonical_chat_id(
                value.get("chat_id"),
                value.get("conversation_id"),
            )
        return value

    @property
    def conversation_id(self) -> str | None:
        """Return the deprecated Python alias during migration."""
        return self.chat_id

    @model_validator(mode="after")
    def validate_releases(self) -> Self:
        """Reject ambiguous or unstable selected-release ordering."""
        identities = [item.capability_id for item in self.releases]
        if len(identities) != len(set(identities)):
            raise ValueError("capability lock releases must be unique")
        expected_lock_id = uuid5(
            self.invocation_id,
            "capability-lock:v1",
        )
        if self.lock_id != expected_lock_id:
            raise ValueError("capability lock identity is invalid")
        expected_hash = self.calculate_manifest_hash(
            invocation_id=self.invocation_id,
            correlation_id=self.correlation_id,
            agent_id=self.agent_id,
            chat_id=self.chat_id,
            registry_epoch_id=self.registry_epoch_id,
            registry_generation=self.registry_generation,
            releases=self.releases,
        )
        if self.manifest_hash != expected_hash:
            raise ValueError("capability lock manifest hash is invalid")
        return self


__all__ = [
    "CapabilityLockManifest",
    "CapabilityRelease",
    "Sha256Digest",
]
