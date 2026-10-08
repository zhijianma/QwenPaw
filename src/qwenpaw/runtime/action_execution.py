# -*- coding: utf-8 -*-
"""Model-independent execution through the governed Action pipeline."""

from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator
from dataclasses import dataclass
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
    ActionStore,
    CapabilitySelection,
    InvocationScope,
    ToolSelection,
)
from ..kernel.models import JsonObject
from ..tool_calls import ToolCoordinator
from .actions import RuntimeActionRecorder


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
                        or "Action denied by current policy"
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


__all__ = [
    "ActionRetryAdmissionError",
    "ActionRetryExecutionAdmission",
    "ActionRetryExecutionPlan",
    "GovernedActionDeniedError",
    "GovernedActionExecutor",
    "PreparedActionRetry",
]
