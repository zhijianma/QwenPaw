# -*- coding: utf-8 -*-
"""Task-independent contracts for one QwenPaw runtime invocation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import AliasChoices, AwareDatetime, Field, model_validator

from .capability_locks import Sha256Digest
from .models import (
    ApprovalLevel,
    EnvironmentContract,
    EnvironmentResolution,
    KernelModel,
    NamespacedId,
    NonEmptyStr,
    utc_now,
)

DEFAULT_AGENT_FACTORY_ID = "qwenpaw.system.chat.agent-factory"
DEFAULT_AGENT_MODE_PROVIDER_ID = "qwenpaw.system.modes.workspace-modes"
DEFAULT_TOOL_PROVIDER_ID = "qwenpaw.system.workspace-tools"
DEFAULT_MEMORY_PROVIDER_ID = "qwenpaw.system.memory.workspace-memory"
DEFAULT_PROMPT_PROVIDER_ID = "qwenpaw.system.prompts.workspace-prompt"
DEFAULT_DRIVER_PROVIDER_ID = "qwenpaw.system.drivers.workspace-driver"
DEFAULT_COMMAND_PROVIDER_ID = "qwenpaw.system.commands.workspace-commands"
DEFAULT_HOOK_PROVIDER_ID = "qwenpaw.system.hooks.workspace-hooks"
DEFAULT_STOP_GATE_PROVIDER_ID = "qwenpaw.system.loop-gates.workspace-gates"


class CapabilitySelection(KernelModel):
    """Capability IDs selected before one invocation starts."""

    agent_factory_id: NamespacedId = DEFAULT_AGENT_FACTORY_ID
    agent_mode_provider_id: NamespacedId = DEFAULT_AGENT_MODE_PROVIDER_ID
    strategy_id: NamespacedId | None = None
    tool_provider_ids: tuple[NamespacedId, ...] = (DEFAULT_TOOL_PROVIDER_ID,)
    memory_provider_id: NamespacedId | None = DEFAULT_MEMORY_PROVIDER_ID
    prompt_provider_ids: tuple[NamespacedId, ...] = (
        DEFAULT_PROMPT_PROVIDER_ID,
    )
    driver_provider_id: NamespacedId | None = DEFAULT_DRIVER_PROVIDER_ID
    command_provider_ids: tuple[NamespacedId, ...] = (
        DEFAULT_COMMAND_PROVIDER_ID,
    )
    hook_provider_ids: tuple[NamespacedId, ...] = (DEFAULT_HOOK_PROVIDER_ID,)
    stop_gate_provider_ids: tuple[NamespacedId, ...] = (
        DEFAULT_STOP_GATE_PROVIDER_ID,
    )


class CapabilitySelectionOverrides(KernelModel):
    """Explicit Profile overrides layered onto automatic Slot selection."""

    agent_factory_id: NamespacedId | None = None
    agent_mode_provider_id: NamespacedId | None = None
    strategy_id: NamespacedId | None = None
    tool_provider_ids: tuple[NamespacedId, ...] | None = None
    memory_provider_id: NamespacedId | None = None
    prompt_provider_ids: tuple[NamespacedId, ...] | None = None
    driver_provider_id: NamespacedId | None = None
    command_provider_ids: tuple[NamespacedId, ...] | None = None
    hook_provider_ids: tuple[NamespacedId, ...] | None = None
    stop_gate_provider_ids: tuple[NamespacedId, ...] | None = None
    disabled_optional_slots: tuple[
        Literal["strategy", "memory.provider", "driver.provider"],
        ...,
    ] = ()

    @model_validator(mode="after")
    def validate_disabled_slots(self) -> Self:
        """Keep selected and disabled optional scalar slots unambiguous."""
        if len(self.disabled_optional_slots) != len(
            set(self.disabled_optional_slots),
        ):
            raise ValueError("disabled optional slots must be unique")
        selected_by_slot = {
            "strategy": self.strategy_id,
            "memory.provider": self.memory_provider_id,
            "driver.provider": self.driver_provider_id,
        }
        conflicts = [
            slot
            for slot in self.disabled_optional_slots
            if selected_by_slot[slot] is not None
        ]
        if conflicts:
            raise ValueError(
                "optional slots cannot be selected and disabled: "
                f"{', '.join(conflicts)}",
            )
        return self


class InvocationScope(KernelModel):
    """Immutable identity and capability generation for one request."""

    invocation_id: UUID = Field(default_factory=uuid4)
    correlation_id: UUID | None = None
    agent_id: NonEmptyStr
    chat_id: NonEmptyStr | None = Field(
        default=None,
        validation_alias=AliasChoices("chat_id", "conversation_id"),
        description="Owning ChatSpec.id; null for non-Chat invocations",
    )
    session_id: NonEmptyStr
    root_agent_id: NonEmptyStr
    root_session_id: NonEmptyStr
    workspace_dir: NonEmptyStr
    registry_epoch_id: UUID | None = None
    registry_generation: int = Field(ge=1)
    capability_lock_id: UUID | None = None
    capability_lock_hash: Sha256Digest | None = None
    approval_level: ApprovalLevel = ApprovalLevel.AGENT_PROFILE
    environment_contract: EnvironmentContract | None = None
    environment_resolution: EnvironmentResolution | None = None
    selection: CapabilitySelection = Field(
        default_factory=CapabilitySelection,
    )
    started_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="before")
    @classmethod
    def validate_chat_identity(cls, value: object) -> object:
        """Reject ambiguous canonical and legacy Chat identities."""
        if isinstance(value, Mapping):
            chat_id = value.get("chat_id")
            conversation_id = value.get("conversation_id")
            if (
                chat_id is not None
                and conversation_id is not None
                and chat_id != conversation_id
            ):
                raise ValueError(
                    "chat_id and conversation_id must identify one Chat",
                )
        return value

    @property
    def conversation_id(self) -> str | None:
        """Return the deprecated Python alias during migration."""
        return self.chat_id

    @model_validator(mode="after")
    def validate_environment_identity(self) -> Self:
        """Bind environment evidence to this immutable invocation."""
        if (self.capability_lock_id is None) != (
            self.capability_lock_hash is None
        ):
            raise ValueError(
                "capability lock identity and hash must be paired",
            )
        contract = self.environment_contract
        resolution = self.environment_resolution
        if contract is None and resolution is None:
            return self
        if contract is None or resolution is None:
            raise ValueError(
                "environment contract and resolution must be paired",
            )
        if resolution.invocation_id != self.invocation_id:
            raise ValueError("environment resolution invocation mismatch")
        if resolution.action_id is not None:
            raise ValueError("invocation environment cannot bind an action")
        if resolution.contract_id != contract.contract_id:
            raise ValueError("environment resolution contract mismatch")
        if resolution.contract_version != contract.version:
            raise ValueError("environment resolution version mismatch")
        return self

    @property
    def capability_ids(self) -> tuple[str, ...]:
        """Return selected capability IDs in stable resolution order."""
        identifiers = [
            self.selection.agent_factory_id,
            self.selection.agent_mode_provider_id,
        ]
        if self.selection.strategy_id is not None:
            identifiers.append(self.selection.strategy_id)
        identifiers.extend(self.selection.tool_provider_ids)
        if self.selection.memory_provider_id is not None:
            identifiers.append(self.selection.memory_provider_id)
        identifiers.extend(self.selection.prompt_provider_ids)
        if self.selection.driver_provider_id is not None:
            identifiers.append(self.selection.driver_provider_id)
        identifiers.extend(self.selection.command_provider_ids)
        identifiers.extend(self.selection.hook_provider_ids)
        identifiers.extend(self.selection.stop_gate_provider_ids)
        return tuple(identifiers)


__all__ = [
    "DEFAULT_AGENT_FACTORY_ID",
    "DEFAULT_AGENT_MODE_PROVIDER_ID",
    "DEFAULT_COMMAND_PROVIDER_ID",
    "DEFAULT_DRIVER_PROVIDER_ID",
    "DEFAULT_HOOK_PROVIDER_ID",
    "DEFAULT_MEMORY_PROVIDER_ID",
    "DEFAULT_PROMPT_PROVIDER_ID",
    "DEFAULT_STOP_GATE_PROVIDER_ID",
    "DEFAULT_TOOL_PROVIDER_ID",
    "CapabilitySelection",
    "CapabilitySelectionOverrides",
    "InvocationScope",
]
