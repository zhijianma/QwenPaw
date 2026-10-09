# -*- coding: utf-8 -*-
"""Chat-first runtime assembly over one pinned capability generation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from ..capabilities import GenerationLease, GenerationRegistry
from ..capabilities.system_chat import (
    SYSTEM_CHAT_CAPABILITY_BUNDLE,
    system_chat_contribution_factory,
)
from ..capabilities.system_tools import (
    SYSTEM_TOOL_CAPABILITY_BUNDLE,
    system_tool_contribution_factory,
)
from ..capabilities.system_memory import (
    SYSTEM_MEMORY_CAPABILITY_BUNDLE,
    system_memory_contribution_factory,
)
from ..capabilities.system_modes import (
    SYSTEM_MODE_CAPABILITY_BUNDLE,
    system_mode_contribution_factory,
)
from ..capabilities.system_prompts import (
    SYSTEM_PROMPT_CAPABILITY_BUNDLE,
    system_prompt_contribution_factory,
)
from ..capabilities.system_drivers import (
    SYSTEM_DRIVER_CAPABILITY_BUNDLE,
    system_driver_contribution_factory,
)
from ..capabilities.system_commands import (
    SYSTEM_COMMAND_CAPABILITY_BUNDLE,
    system_command_contribution_factory,
)
from ..capabilities.system_hooks import (
    SYSTEM_HOOK_CAPABILITY_BUNDLE,
    system_hook_contribution_factory,
)
from ..capabilities.system_stop_gates import (
    SYSTEM_STOP_GATE_CAPABILITY_BUNDLE,
    system_stop_gate_contribution_factory,
)
from ..kernel.invocation import (
    CapabilitySelection,
    CapabilitySelectionOverrides,
    InvocationScope,
)
from ..kernel.models import (
    ApprovalLevel,
    CapabilityDescriptor,
    EnvironmentContract,
    EnvironmentRecord,
    EnvironmentResolutionStatus,
)
from ..kernel.ports import (
    CapabilityLockStore,
    EnvironmentResolver,
    EnvironmentStore,
)
from .capability_locks import (
    CapabilityLockCompiler,
    FilesystemCapabilityLockStore,
)
from .environments import (
    EnvironmentContractUnsatisfiedError,
    FilesystemEnvironmentStore,
    LiteEnvironmentResolver,
    default_lite_environment_contract,
)


class CapabilityUnavailableError(RuntimeError):
    """Raised when a selected capability cannot be dispatched safely."""


@dataclass(frozen=True)
class InvocationAssembly:
    """Runtime implementations resolved from one immutable generation."""

    scope: InvocationScope
    _lease: GenerationLease

    def require(self, capability_id: str, slot: str) -> object:
        """Resolve one capability and validate its declared slot."""
        descriptor = self._lease.resolve(capability_id)
        if descriptor is None:
            raise CapabilityUnavailableError(
                f"capability '{capability_id}' is unavailable",
            )
        if descriptor.slot != slot:
            raise CapabilityUnavailableError(
                f"capability '{capability_id}' uses slot "
                f"'{descriptor.slot}', expected '{slot}'",
            )
        implementation = self._lease.implementation(capability_id)
        if implementation is None:
            raise CapabilityUnavailableError(
                f"capability '{capability_id}' has no implementation",
            )
        return implementation

    def descriptor(self, capability_id: str) -> CapabilityDescriptor:
        """Return pinned metadata for one selected capability."""
        descriptor = self._lease.resolve(capability_id)
        if descriptor is None:
            raise CapabilityUnavailableError(
                f"capability '{capability_id}' is unavailable",
            )
        return descriptor

    async def close(self) -> None:
        """Release the pinned generation."""
        await self._lease.close()

    def validate_selection(self) -> None:
        """Fail closed for an absent or incorrectly slotted selection."""
        selected = self.scope.selection
        required_scalar_slots = (
            (selected.agent_factory_id, "agent.factory"),
            (selected.agent_mode_provider_id, "agent.mode.provider"),
        )
        for capability_id, slot in required_scalar_slots:
            if capability_id is None:
                raise CapabilityUnavailableError(
                    f"required slot '{slot}' cannot be disabled",
                )
        requirements = [
            *required_scalar_slots,
            *((item, "tool.provider") for item in selected.tool_provider_ids),
            *(
                (item, "prompt.provider")
                for item in selected.prompt_provider_ids
            ),
            *(
                (item, "command.provider")
                for item in selected.command_provider_ids
            ),
            *((item, "hook.provider") for item in selected.hook_provider_ids),
            *(
                (item, "loop.gate.provider")
                for item in selected.stop_gate_provider_ids
            ),
        ]
        if selected.strategy_id is not None:
            requirements.append((selected.strategy_id, "strategy"))
        if selected.memory_provider_id is not None:
            requirements.append(
                (selected.memory_provider_id, "memory.provider"),
            )
        if selected.driver_provider_id is not None:
            requirements.append(
                (selected.driver_provider_id, "driver.provider"),
            )
        for capability_id, slot in requirements:
            self.require(capability_id, slot)


class RuntimeAssemblyFactory:
    """Open immutable OS invocation assemblies for Chat requests."""

    def __init__(self, registry: GenerationRegistry) -> None:
        self._registry = registry

    async def prepare(self) -> None:
        """Ensure all built-in Chat runtime bundles are published."""
        await self._registry.ensure_bundle(
            SYSTEM_CHAT_CAPABILITY_BUNDLE,
            system_chat_contribution_factory,
        )
        await self._registry.ensure_bundle(
            SYSTEM_TOOL_CAPABILITY_BUNDLE,
            system_tool_contribution_factory,
        )
        await self._registry.ensure_bundle(
            SYSTEM_MEMORY_CAPABILITY_BUNDLE,
            system_memory_contribution_factory,
        )
        await self._registry.ensure_bundle(
            SYSTEM_MODE_CAPABILITY_BUNDLE,
            system_mode_contribution_factory,
        )
        await self._registry.ensure_bundle(
            SYSTEM_PROMPT_CAPABILITY_BUNDLE,
            system_prompt_contribution_factory,
        )
        await self._registry.ensure_bundle(
            SYSTEM_DRIVER_CAPABILITY_BUNDLE,
            system_driver_contribution_factory,
        )
        await self._registry.ensure_bundle(
            SYSTEM_COMMAND_CAPABILITY_BUNDLE,
            system_command_contribution_factory,
        )
        await self._registry.ensure_bundle(
            SYSTEM_HOOK_CAPABILITY_BUNDLE,
            system_hook_contribution_factory,
        )
        await self._registry.ensure_bundle(
            SYSTEM_STOP_GATE_CAPABILITY_BUNDLE,
            system_stop_gate_contribution_factory,
        )

    async def open(
        self,
        *,
        agent_id: str,
        chat_id: str | None = None,
        session_id: str,
        root_agent_id: str,
        root_session_id: str,
        workspace_dir: str | Path,
        selection: CapabilitySelection | None = None,
        selection_overrides: CapabilitySelectionOverrides | None = None,
        approval_level: ApprovalLevel = ApprovalLevel.AGENT_PROFILE,
        registry_generation: int | None = None,
        invocation_id: UUID | None = None,
        correlation_id: UUID | None = None,
        environment_contract: EnvironmentContract | None = None,
        environment_resolver: EnvironmentResolver | None = None,
        environment_store: EnvironmentStore | None = None,
        capability_lock_store: CapabilityLockStore | None = None,
        conversation_id: str | None = None,
    ) -> InvocationAssembly:
        """Pin the catalog and return one task-independent scope."""
        if (
            chat_id is not None
            and conversation_id is not None
            and chat_id != conversation_id
        ):
            raise ValueError(
                "chat_id and conversation_id must identify one Chat",
            )
        if chat_id is not None and not chat_id.strip():
            raise ValueError("chat_id cannot be empty")
        if conversation_id is not None and not conversation_id.strip():
            raise ValueError("conversation_id cannot be empty")
        resolved_chat_id = (
            chat_id if chat_id is not None else conversation_id
        )
        if selection is not None and selection_overrides is not None:
            raise ValueError(
                "selection and selection_overrides are mutually exclusive",
            )
        await self.prepare()
        lease = await self._registry.pin(registry_generation)
        if selection is None:
            tool_provider_ids = tuple(
                descriptor.capability_id
                for descriptor in lease.descriptors("tool.provider")
            )
            prompt_provider_ids = tuple(
                descriptor.capability_id
                for descriptor in lease.descriptors("prompt.provider")
            )
            command_provider_ids = tuple(
                descriptor.capability_id
                for descriptor in lease.descriptors("command.provider")
            )
            hook_provider_ids = tuple(
                descriptor.capability_id
                for descriptor in lease.descriptors("hook.provider")
            )
            stop_gate_provider_ids = tuple(
                descriptor.capability_id
                for descriptor in lease.descriptors("loop.gate.provider")
            )
            resolved_selection = CapabilitySelection(
                tool_provider_ids=tool_provider_ids,
                prompt_provider_ids=prompt_provider_ids,
                command_provider_ids=command_provider_ids,
                hook_provider_ids=hook_provider_ids,
                stop_gate_provider_ids=stop_gate_provider_ids,
            )
            if selection_overrides is not None:
                override_fields = (
                    "agent_factory_id",
                    "agent_mode_provider_id",
                    "strategy_id",
                    "tool_provider_ids",
                    "memory_provider_id",
                    "prompt_provider_ids",
                    "driver_provider_id",
                    "command_provider_ids",
                    "hook_provider_ids",
                    "stop_gate_provider_ids",
                )
                updates = {
                    field_name: getattr(selection_overrides, field_name)
                    for field_name in override_fields
                    if getattr(selection_overrides, field_name) is not None
                }
                disabled_fields = {
                    "strategy": "strategy_id",
                    "memory.provider": "memory_provider_id",
                    "driver.provider": "driver_provider_id",
                }
                updates.update(
                    {
                        disabled_fields[slot]: None
                        for slot in (
                            selection_overrides.disabled_optional_slots
                        )
                    },
                )
                resolved_selection = resolved_selection.model_copy(
                    update=updates,
                )
        else:
            resolved_selection = selection
        resolved_invocation_id = invocation_id or uuid4()
        resolved_workspace_dir = str(Path(workspace_dir).resolve(strict=False))
        contract = environment_contract or default_lite_environment_contract(
            resolved_workspace_dir,
        )
        resolver = environment_resolver or LiteEnvironmentResolver()
        store = environment_store or FilesystemEnvironmentStore(
            resolved_workspace_dir,
        )
        try:
            resolution = await resolver.resolve(
                contract,
                invocation_id=resolved_invocation_id,
                workspace_dir=resolved_workspace_dir,
            )
            if (
                environment_store is not None
                or Path(
                    resolved_workspace_dir,
                ).is_dir()
            ):
                await store.record(
                    EnvironmentRecord(
                        contract=contract,
                        resolution=resolution,
                    ),
                    conversation_id=resolved_chat_id,
                )
            if resolution.status is EnvironmentResolutionStatus.UNSATISFIED:
                raise EnvironmentContractUnsatisfiedError(resolution)
            scope = InvocationScope(
                invocation_id=resolved_invocation_id,
                correlation_id=(correlation_id or resolved_invocation_id),
                agent_id=agent_id,
                chat_id=resolved_chat_id,
                session_id=session_id,
                root_agent_id=root_agent_id,
                root_session_id=root_session_id,
                workspace_dir=resolved_workspace_dir,
                registry_epoch_id=lease.registry_epoch_id,
                registry_generation=lease.generation,
                approval_level=approval_level,
                environment_contract=contract,
                environment_resolution=resolution,
                selection=resolved_selection,
            )
            assembly = InvocationAssembly(scope=scope, _lease=lease)
            assembly.validate_selection()
            lock = CapabilityLockCompiler.compile(scope, lease)
            lock_store = (
                capability_lock_store
                or FilesystemCapabilityLockStore(
                    Path(resolved_workspace_dir),
                )
            )
            await lock_store.append(lock)
            scope = scope.model_copy(
                update={
                    "capability_lock_id": lock.lock_id,
                    "capability_lock_hash": lock.manifest_hash,
                },
            )
            assembly = InvocationAssembly(scope=scope, _lease=lease)
        except Exception:
            await lease.close()
            raise
        return assembly


def capability_registry_for(workspace: Any) -> GenerationRegistry:
    """Return the Host-owned catalog or fail closed when it is absent."""
    registry = getattr(workspace, "capability_registry", None)
    if not isinstance(registry, GenerationRegistry):
        raise CapabilityUnavailableError(
            "workspace has no shared capability registry",
        )
    return registry


__all__ = [
    "CapabilityUnavailableError",
    "InvocationAssembly",
    "RuntimeAssemblyFactory",
    "capability_registry_for",
]
