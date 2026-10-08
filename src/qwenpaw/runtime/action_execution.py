# -*- coding: utf-8 -*-
"""Model-independent execution through the governed Action pipeline."""

from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid5

from agentscope.permission import PermissionBehavior
from agentscope.tool import ToolResponse

from ..kernel import (
    ActionRecord,
    ActionRetryContinuation,
    ActionRetryDisposition,
    ActionRetryInputStore,
    ActionRetryContinuationStore,
    ActionStore,
    CapabilitySelection,
    InvocationScope,
    ToolSelection,
)
from ..kernel.models import JsonObject
from ..tool_calls import ToolCoordinator
from .actions import RuntimeActionRecorder
from .assembly import RuntimeAssemblyFactory, capability_registry_for
from .builder import AgentBuilder


class GovernedActionDeniedError(RuntimeError):
    """Raised when current policy denies a scheduled Action."""


class ActionRetryAdmissionError(RuntimeError):
    """Raised when durable retry evidence cannot authorize execution."""


@dataclass(frozen=True)
class PreparedActionRetry:
    """Verified private input and immutable source Action evidence."""

    continuation: ActionRetryContinuation
    previous: ActionRecord
    arguments: JsonObject


@dataclass(frozen=True)
class ActionRetryExecutionPlan:
    """Deterministic Orchestrator admission for one outbox revision."""

    invocation_id: UUID
    correlation_id: UUID
    chat_id: str
    tool_call_id: str
    registry_generation: int
    capability_selection: CapabilitySelection
    tool_selection: ToolSelection
    provider_execution_digest: str

    @classmethod
    def from_prepared(
        cls,
        prepared: PreparedActionRetry,
    ) -> "ActionRetryExecutionPlan":
        """Compile stable identities without transport session aliases."""
        continuation = prepared.continuation
        checkpoint = continuation.checkpoint
        chat_id = checkpoint.conversation_id
        selection = checkpoint.tool_selection
        provider_digest = checkpoint.provider_execution_digest
        if chat_id is None or selection is None or provider_digest is None:
            raise ActionRetryAdmissionError(
                "Action retry cannot compile an execution plan",
            )
        return cls(
            invocation_id=uuid5(
                continuation.continuation_id,
                f"execution:{continuation.revision}",
            ),
            correlation_id=checkpoint.correlation_id,
            chat_id=chat_id,
            tool_call_id=(
                f"action-retry:{continuation.continuation_id}:"
                f"{continuation.revision}"
            ),
            registry_generation=checkpoint.registry_generation,
            capability_selection=CapabilitySelection(
                tool_provider_ids=(checkpoint.capability_id,),
                memory_provider_id=None,
                prompt_provider_ids=(),
                driver_provider_id=None,
                command_provider_ids=(),
                hook_provider_ids=(),
                stop_gate_provider_ids=(),
            ),
            tool_selection=selection,
            provider_execution_digest=provider_digest,
        )


class ActionRetryExecutionAdmission:
    """Rebuild exact retry authority immediately before execution."""

    def __init__(
        self,
        *,
        input_store: ActionRetryInputStore,
        action_store: ActionStore,
    ) -> None:
        self._inputs = input_store
        self._actions = action_store

    async def prepare(
        self,
        continuation: ActionRetryContinuation,
    ) -> PreparedActionRetry:
        """Fail closed unless every durable identity still agrees."""
        checkpoint = continuation.checkpoint
        if (
            checkpoint.tool_selection is None
            or checkpoint.provider_execution_digest is None
        ):
            raise ActionRetryAdmissionError(
                "Action retry lacks a reproducible execution snapshot",
            )
        stored, arguments = await self._inputs.load(
            checkpoint.checkpoint_id,
        )
        if stored != checkpoint:
            raise ActionRetryAdmissionError(
                "Action retry input checkpoint identity mismatch",
            )
        previous = await self._actions.get(
            checkpoint.action_id,
            invocation_id=checkpoint.invocation_id,
            conversation_id=checkpoint.conversation_id,
        )
        if previous is None or previous.result is None:
            raise ActionRetryAdmissionError(
                "Action retry source evidence is unavailable",
            )
        result = previous.result
        decision = result.retry_decision
        source_drifted = (
            result.observation_digest
            != continuation.source_observation_digest
        )
        retry_invalid = decision is None or not result.retryable
        if decision is not None:
            retry_invalid = retry_invalid or (
                decision.disposition
                is not ActionRetryDisposition.RETRY_FROM_NEW_ACTION
                or decision.input_checkpoint_id
                != checkpoint.checkpoint_id
                or decision.next_attempt != checkpoint.next_attempt
            )
        if source_drifted or retry_invalid:
            raise ActionRetryAdmissionError(
                "Action retry source result no longer authorizes execution",
            )
        return PreparedActionRetry(
            continuation=continuation,
            previous=previous,
            arguments=arguments,
        )


class GovernedActionExecutor:
    """Execute one exact tool without invoking model reasoning."""

    def __init__(self, coordinator: ToolCoordinator) -> None:
        self._coordinator = coordinator

    async def execute(
        self,
        *,
        tool: Any,
        arguments: JsonObject,
        tool_call_id: str,
        scope: InvocationScope,
        recorder: RuntimeActionRecorder,
    ) -> ToolResponse:
        """Run permission, supervision and Action evidence in order."""
        tool_name = str(getattr(tool, "name", "") or "")
        if not tool_name:
            raise TypeError("governed Action tool has no stable name")
        request_context = getattr(tool, "_qp_request_context", None)
        if (
            not isinstance(request_context, dict)
            or request_context.get("_action_recorder") is not recorder
        ):
            raise TypeError(
                "governed Action tool is not bound to its retry recorder",
            )
        check_permissions = getattr(tool, "check_permissions", None)
        if not callable(check_permissions):
            raise TypeError(
                "governed Action tool has no permission boundary",
            )
        tool_call = SimpleNamespace(
            id=tool_call_id,
            name=tool_name,
            input=dict(arguments),
        )

        async def admit(_context: Any) -> None:
            decision = await check_permissions(dict(arguments), None)
            if decision.behavior is not PermissionBehavior.ALLOW:
                raise GovernedActionDeniedError(
                    str(
                        decision.message
                        or "Action denied by current policy",
                    ),
                )

        async def invoke(
            *,
            tool_call: Any,
        ) -> AsyncGenerator[Any, None]:
            result = tool(**tool_call.input)
            if inspect.isawaitable(result):
                result = await result
            if hasattr(result, "__aiter__"):
                async for chunk in result:
                    yield chunk
                return
            yield result

        async for _ in self._coordinator.execute(
            tool_call=tool_call,
            next_handler=invoke,
            session_id=scope.session_id,
            agent_id=scope.agent_id,
            root_session_id=scope.root_session_id,
            root_agent_id=scope.root_agent_id,
            result_processor=recorder.complete,
            allow_offload=False,
            admission_check=admit,
        ):
            pass
        entry = self._coordinator.get(tool_call_id)
        if entry is None or entry.final_response is None:
            raise RuntimeError("governed Action produced no terminal result")
        record = await recorder.committed_record(entry.ctx)
        if record is None:
            raise RuntimeError(
                "governed Action terminal evidence was not committed",
            )
        return entry.final_response


class RuntimeActionRetryRunner:
    """Run one admitted retry through a pinned Runtime assembly."""

    def __init__(
        self,
        *,
        workspace: Any,
        coordinator: ToolCoordinator,
        action_store: ActionStore,
        retry_input_store: ActionRetryInputStore,
        retry_continuation_store: ActionRetryContinuationStore,
        builder: Any | None = None,
        executor: Any | None = None,
        assembly_factory: Any | None = None,
    ) -> None:
        self._workspace = workspace
        self._actions = action_store
        self._retry_inputs = retry_input_store
        self._retry_continuations = retry_continuation_store
        self._builder = builder or AgentBuilder()
        self._executor = executor or GovernedActionExecutor(coordinator)
        self._assembly_factory = assembly_factory

    async def run(
        self,
        *,
        prepared: PreparedActionRetry,
        plan: ActionRetryExecutionPlan,
        agent_config: Any,
        governor: Any,
    ) -> ToolResponse:
        """Resolve and execute without invoking model reasoning."""
        checkpoint = prepared.continuation.checkpoint
        factory = self._assembly_factory or RuntimeAssemblyFactory(
            capability_registry_for(self._workspace),
        )
        assembly = await factory.open(
            agent_id=checkpoint.agent_id,
            conversation_id=plan.chat_id,
            session_id=plan.chat_id,
            root_agent_id=checkpoint.agent_id,
            root_session_id=plan.chat_id,
            workspace_dir=self._workspace.workspace_dir,
            selection=plan.capability_selection,
            registry_generation=plan.registry_generation,
            invocation_id=plan.invocation_id,
            correlation_id=plan.correlation_id,
        )
        try:
            recorder = RuntimeActionRecorder(
                assembly.scope,
                self._actions,
                retry_input_store=self._retry_inputs,
                retry_continuation_store=self._retry_continuations,
                retry_of=prepared.previous,
                tool_selection=plan.tool_selection,
                provider_execution_digests={
                    checkpoint.capability_id: (
                        plan.provider_execution_digest
                    ),
                },
            )
            request_context = self._request_context(
                recorder=recorder,
                plan=plan,
                scope=assembly.scope,
            )
            context = SimpleNamespace(
                invocation_scope=assembly.scope,
                extras={"runtime_assembly": assembly},
                workspace=self._workspace,
            )
            tool = await self._builder.resolve_governed_action_tool(
                ctx=context,
                agent_config=agent_config,
                request_context=request_context,
                provider_id=checkpoint.capability_id,
                tool_name=checkpoint.action_name,
                tool_selection=plan.tool_selection,
                governor=governor,
            )
            current_digest = request_context.get(
                "_tool_provider_execution_digests",
                {},
            ).get(checkpoint.capability_id)
            if current_digest != plan.provider_execution_digest:
                raise ActionRetryAdmissionError(
                    "Action retry provider configuration changed",
                )
            return await self._executor.execute(
                tool=tool,
                arguments=prepared.arguments,
                tool_call_id=plan.tool_call_id,
                scope=assembly.scope,
                recorder=recorder,
            )
        finally:
            await assembly.close()

    def _request_context(
        self,
        *,
        recorder: RuntimeActionRecorder,
        plan: ActionRetryExecutionPlan,
        scope: InvocationScope,
    ) -> dict[str, Any]:
        from .environments import FilesystemEnvironmentStore
        from .sandbox_environments import RuntimeSandboxEnvironmentManager

        service = getattr(self._workspace, "interaction_service", None)
        app_services = getattr(self._workspace, "app_services", None)
        context: dict[str, Any] = {
            "agent_id": self._workspace.agent_id,
            "session_id": plan.chat_id,
            "root_agent_id": self._workspace.agent_id,
            "root_session_id": plan.chat_id,
            "channel": "console",
            "os_invocation_id": str(plan.invocation_id),
            "os_correlation_id": str(plan.correlation_id),
            "os_conversation_id": plan.chat_id,
            "os_registry_generation": plan.registry_generation,
            "_action_recorder": recorder,
            "_interaction_service": service,
            "_sandbox_environment_manager": (
                RuntimeSandboxEnvironmentManager(
                    scope,
                    FilesystemEnvironmentStore(
                        Path(self._workspace.workspace_dir),
                    ),
                )
            ),
            "approval_coordinator": getattr(
                app_services,
                "approval_coordinator",
                None,
            ),
            "tool_coordinator": getattr(
                app_services,
                "tool_coordinator",
                None,
            ),
        }
        if service is not None:
            from ..interactions import runtime_interaction_broker_from_context

            broker = runtime_interaction_broker_from_context(context)
            if broker is not None:
                context["_interaction_broker"] = broker
        return context


__all__ = [
    "ActionRetryAdmissionError",
    "ActionRetryExecutionAdmission",
    "ActionRetryExecutionPlan",
    "GovernedActionDeniedError",
    "GovernedActionExecutor",
    "PreparedActionRetry",
    "RuntimeActionRetryRunner",
]
