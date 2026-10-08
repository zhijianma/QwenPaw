# -*- coding: utf-8 -*-
"""Model-independent execution through the governed Action pipeline."""

from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any

from agentscope.permission import PermissionBehavior
from agentscope.tool import ToolResponse

from ..kernel import InvocationScope
from ..kernel.models import JsonObject
from ..tool_calls import ToolCoordinator
from .actions import RuntimeActionRecorder


class GovernedActionDeniedError(RuntimeError):
    """Raised when current policy denies a scheduled Action."""


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
        decision = await check_permissions(dict(arguments), None)
        if decision.behavior is not PermissionBehavior.ALLOW:
            raise GovernedActionDeniedError(
                str(decision.message or "Action denied by current policy"),
            )
        tool_call = SimpleNamespace(
            id=tool_call_id,
            name=tool_name,
            input=dict(arguments),
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
    "GovernedActionDeniedError",
    "GovernedActionExecutor",
]
