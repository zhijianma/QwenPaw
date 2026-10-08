# -*- coding: utf-8 -*-
"""Stable Driver control-plane errors."""

from __future__ import annotations

import json

from .models import DriverToolDefinition, PromptFragment

_MAX_DRIVER_TOOLS = 128
_MAX_DRIVER_PROMPT_FRAGMENTS = 16
_MAX_DRIVER_PROMPT_BYTES = 32 * 1024
_MAX_DRIVER_CATALOG_BYTES = 64 * 1024


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


def _validate_driver_definitions(
    definitions: tuple[DriverToolDefinition, ...],
    provider_id: str,
) -> None:
    """Validate typed tools, ownership, uniqueness, and catalog budget."""
    if len(definitions) > _MAX_DRIVER_TOOLS:
        raise ValueError(
            f"driver provider '{provider_id}' returned too many tools",
        )
    capability_ids: set[str] = set()
    tool_names: set[str] = set()
    for definition in definitions:
        if not isinstance(definition, DriverToolDefinition):
            raise TypeError(
                f"driver provider '{provider_id}' returned an untyped tool",
            )
        if definition.provider_id != provider_id:
            raise ValueError(
                f"driver tool '{definition.name}' has foreign ownership",
            )
        if definition.capability_id in capability_ids:
            raise ValueError("driver capability IDs must be unique")
        if definition.name in tool_names:
            raise ValueError("driver tool names must be unique")
        capability_ids.add(definition.capability_id)
        tool_names.add(definition.name)
    encoded = json.dumps(
        [definition.model_dump(mode="json") for definition in definitions],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > _MAX_DRIVER_CATALOG_BYTES:
        raise ValueError(
            f"driver provider '{provider_id}' tool catalog is too large",
        )


def _validate_driver_fragments(
    fragments: tuple[PromptFragment, ...],
    provider_id: str,
) -> None:
    """Validate bounded prompts and their provider ownership."""
    if len(fragments) > _MAX_DRIVER_PROMPT_FRAGMENTS:
        raise ValueError(
            f"driver provider '{provider_id}' returned too many prompts",
        )
    fragment_ids: set[str] = set()
    prompt_bytes = 0
    for fragment in fragments:
        if not isinstance(fragment, PromptFragment):
            raise TypeError(
                f"driver provider '{provider_id}' returned an untyped prompt",
            )
        if not fragment.fragment_id.startswith(f"{provider_id}."):
            raise ValueError(
                f"driver prompt '{fragment.fragment_id}' has foreign "
                "ownership",
            )
        if fragment.fragment_id in fragment_ids:
            raise ValueError("driver prompt IDs must be unique")
        fragment_ids.add(fragment.fragment_id)
        prompt_bytes += len(fragment.content.encode("utf-8"))
    if prompt_bytes > _MAX_DRIVER_PROMPT_BYTES:
        raise ValueError(
            f"driver provider '{provider_id}' prompt content is too large",
        )


def validate_driver_session(
    session: object,
    provider_id: str,
) -> tuple[tuple[DriverToolDefinition, ...], tuple[PromptFragment, ...]]:
    """Validate one pinned Driver session before catalog publication."""
    if getattr(session, "provider_id", None) != provider_id:
        raise TypeError(
            f"driver session for '{provider_id}' has a mismatched identity",
        )
    list_tools = getattr(session, "list_tools", None)
    prompt_fragments = getattr(session, "prompt_fragments", None)
    if not callable(list_tools) or not callable(prompt_fragments):
        raise TypeError(f"driver session for '{provider_id}' is invalid")
    definitions = tuple(list_tools())
    fragments = tuple(prompt_fragments())
    _validate_driver_definitions(definitions, provider_id)
    _validate_driver_fragments(fragments, provider_id)
    return definitions, tuple(
        sorted(fragments, key=lambda item: (item.priority, item.fragment_id)),
    )


__all__ = [
    "CapabilityCredentialUnavailableError",
    "DriverApprovalRejectedError",
    "DriverCredentialUnavailableError",
    "validate_driver_session",
]
