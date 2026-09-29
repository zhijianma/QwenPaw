# -*- coding: utf-8 -*-
"""Stable Driver control-plane errors."""

from __future__ import annotations


class DriverApprovalRejectedError(PermissionError):
    """Raised when a Driver approval cannot authorize execution."""

    def __init__(
        self,
        capability_id: str,
        reason: str,
    ) -> None:
        self.capability_id = capability_id
        self.reason = reason
        super().__init__(
            f"driver approval rejected for '{capability_id}': {reason}",
        )


class CapabilityCredentialUnavailableError(RuntimeError):
    """Raised when an invocation capability credential cannot be read."""

    def __init__(
        self,
        provider_id: str,
        alias: str,
        field: str = "",
    ) -> None:
        self.provider_id = provider_id
        self.alias = alias
        self.field = field
        target = f"{alias}.{field}" if field else alias
        super().__init__(
            f"capability credential '{target}' is unavailable for "
            f"'{provider_id}'",
        )


DriverCredentialUnavailableError = CapabilityCredentialUnavailableError


__all__ = [
    "CapabilityCredentialUnavailableError",
    "DriverApprovalRejectedError",
    "DriverCredentialUnavailableError",
]
