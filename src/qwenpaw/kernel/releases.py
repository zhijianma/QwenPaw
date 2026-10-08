# -*- coding: utf-8 -*-
"""Stable release identities for promoted capability providers."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Self

from pydantic import Field, model_validator

from .capability_locks import CapabilityRelease, Sha256Digest
from .models import (
    CapabilityDescriptor,
    CapabilityProviderKind,
    KernelModel,
    NamespacedId,
    NonEmptyStr,
)


class CapabilityReleaseTag(KernelModel):
    """Content-addressed provider release assigned to one channel."""

    schema_id: Literal["qwenpaw.capability-release-tag.v1"] = Field(
        default="qwenpaw.capability-release-tag.v1",
        alias="schema",
    )
    channel: Literal["stable"] = "stable"
    provider_id: NamespacedId
    provider_kind: CapabilityProviderKind
    version: NonEmptyStr
    promoted_generation: int = Field(ge=2)
    releases: tuple[CapabilityRelease, ...] = ()
    release_hash: Sha256Digest

    @property
    def capability_ids(self) -> tuple[str, ...]:
        """Return stable capability identities in release order."""
        return tuple(item.capability_id for item in self.releases)

    @classmethod
    def create(
        cls,
        *,
        provider_id: str,
        provider_kind: CapabilityProviderKind,
        version: str,
        promoted_generation: int,
        descriptors: tuple[CapabilityDescriptor, ...],
    ) -> Self:
        """Create one deterministic stable tag from promoted descriptors."""
        ordered = tuple(
            sorted(descriptors, key=lambda item: item.capability_id),
        )
        return cls(
            provider_id=provider_id,
            provider_kind=provider_kind,
            version=version,
            promoted_generation=promoted_generation,
            releases=tuple(
                CapabilityRelease(
                    capability_id=item.capability_id,
                    slot=item.slot,
                    provider_id=item.provider_id,
                    provider_kind=item.provider_kind,
                    version=item.version,
                    restart_policy=item.restart_policy,
                    descriptor_hash=cls._descriptor_hash(item),
                )
                for item in ordered
            ),
            release_hash=cls.calculate_release_hash(
                provider_id=provider_id,
                provider_kind=provider_kind,
                version=version,
                descriptors=ordered,
            ),
        )

    @staticmethod
    def calculate_release_hash(
        *,
        provider_id: str,
        provider_kind: CapabilityProviderKind,
        version: str,
        descriptors: tuple[CapabilityDescriptor, ...],
    ) -> str:
        """Hash the immutable provider release without generation state."""
        releases = tuple(
            CapabilityRelease(
                capability_id=item.capability_id,
                slot=item.slot,
                provider_id=item.provider_id,
                provider_kind=item.provider_kind,
                version=item.version,
                restart_policy=item.restart_policy,
                descriptor_hash=CapabilityReleaseTag._descriptor_hash(item),
            )
            for item in descriptors
        )
        return CapabilityReleaseTag._hash_releases(
            provider_id=provider_id,
            provider_kind=provider_kind,
            version=version,
            releases=releases,
        )

    @staticmethod
    def _descriptor_hash(descriptor: CapabilityDescriptor) -> str:
        encoded = json.dumps(
            descriptor.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    @staticmethod
    def _hash_releases(
        *,
        provider_id: str,
        provider_kind: CapabilityProviderKind,
        version: str,
        releases: tuple[CapabilityRelease, ...],
    ) -> str:
        payload = {
            "provider_id": provider_id,
            "provider_kind": provider_kind.value,
            "version": version,
            "releases": [
                item.model_dump(mode="json") for item in releases
            ],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        """Reject ambiguous capability ordering or provider identities."""
        capability_ids = self.capability_ids
        if tuple(sorted(capability_ids)) != capability_ids:
            raise ValueError("release capability IDs must be sorted")
        if len(set(capability_ids)) != len(capability_ids):
            raise ValueError("release capability IDs must be unique")
        prefix = f"{self.provider_id}."
        if any(
            not capability_id.startswith(prefix)
            for capability_id in capability_ids
        ):
            raise ValueError("release capability provider is inconsistent")
        if any(
            item.provider_id != self.provider_id
            or item.provider_kind is not self.provider_kind
            or item.version != self.version
            for item in self.releases
        ):
            raise ValueError("release descriptor identity is inconsistent")
        expected_hash = self._hash_releases(
            provider_id=self.provider_id,
            provider_kind=self.provider_kind,
            version=self.version,
            releases=self.releases,
        )
        if self.release_hash != expected_hash:
            raise ValueError("capability release tag hash is invalid")
        return self


__all__ = ["CapabilityReleaseTag"]
