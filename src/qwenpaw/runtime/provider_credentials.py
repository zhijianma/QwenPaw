# -*- coding: utf-8 -*-
"""Shared provider-scoped credential handles for public Runtime Hosts."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ..kernel.driver import CapabilityCredentialUnavailableError


@dataclass(frozen=True, repr=False)
class WorkspaceCredentialHandle:
    """Lazy access to one alias without exposing its backing store or ref."""

    _store: Any
    _ref: str
    _provider_id: str
    _alias: str

    def __repr__(self) -> str:
        """Never expose credential references or values in diagnostics."""
        return (
            "WorkspaceCredentialHandle("
            f"provider_id={self._provider_id!r}, alias={self._alias!r})"
        )

    async def _record(self) -> Any:
        try:
            return await self._store.get(self._ref)
        except Exception as error:
            raise CapabilityCredentialUnavailableError(
                self._provider_id,
                self._alias,
            ) from error

    async def public_values(self) -> dict[str, Any]:
        """Return a detached copy of non-secret credential values."""
        record = await self._record()
        return deepcopy(dict(getattr(record, "public", {}) or {}))

    async def read_secret(self, name: str) -> str:
        """Return one named secret without exposing the backing record."""
        record = await self._record()
        secrets = dict(getattr(record, "secrets", {}) or {})
        value = secrets.get(name)
        if not isinstance(value, str):
            raise CapabilityCredentialUnavailableError(
                self._provider_id,
                self._alias,
                name,
            )
        return value


def provider_credential_handle(
    manager: Any,
    refs: dict[str, str],
    provider_id: str,
    alias: str,
) -> WorkspaceCredentialHandle | None:
    """Return one pre-bound alias without accepting arbitrary references."""
    ref = refs.get(alias)
    if not ref:
        return None
    store = getattr(manager, "credential_store", None)
    if store is None:
        raise CapabilityCredentialUnavailableError(provider_id, alias)
    return WorkspaceCredentialHandle(store, ref, provider_id, alias)


__all__ = [
    "WorkspaceCredentialHandle",
    "provider_credential_handle",
]
