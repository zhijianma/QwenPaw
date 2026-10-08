# -*- coding: utf-8 -*-
"""AgentScope runtime adapter for Driver capabilities.

Driver owns capability discovery and policy execution.  This module is the
thin boundary that turns those protocol-neutral capabilities into
AgentScope ``ToolBase`` instances for the agent runtime.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from agentscope.message import (
    Base64Source,
    DataBlock,
    TextBlock,
    ToolResultState,
)
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolBase, ToolChunk

from ...kernel.invocation import DEFAULT_DRIVER_PROVIDER_ID
from ...kernel.driver import DriverApprovalRejectedError
from ...kernel.models import (
    ActionIdempotencyMode,
    ActionRequest,
    DriverToolDefinition,
    PromptFragment,
    RiskLevel,
    ToolEffect,
)
from ..capabilities import (
    DriverCapability,
    DriverInvocation,
    DriverInvocationResult,
)

logger = logging.getLogger(__name__)

DriverInvoker = Callable[[DriverInvocation], Awaitable[DriverInvocationResult]]


def _action_classification(
    capability: DriverCapability,
) -> tuple[ToolEffect, RiskLevel, bool]:
    """Translate explicit Driver metadata without guessing from names."""
    if capability.metadata.get("read_only") is True:
        return ToolEffect.NONE, RiskLevel.LOW, True
    return ToolEffect.EXTERNAL_WRITE, RiskLevel.HIGH, False


def _action_idempotency(
    capability: DriverCapability,
) -> ActionIdempotencyMode:
    """Read an explicit Driver promise without inferring from names."""
    value = capability.metadata.get("action_idempotency")
    if value is None:
        return ActionIdempotencyMode.UNDECLARED
    return ActionIdempotencyMode(str(value))


def _driver_request_context(
    base: dict[str, Any],
) -> dict[str, Any]:
    """Attach the active Action identity at the Driver boundary."""
    from ...tool_calls._ctxvars import get_call_context

    values = dict(base)
    context = get_call_context()
    if context is None:
        return values
    request = context.extra.get("qwenpaw_action_request")
    if not isinstance(request, ActionRequest):
        return values
    values.update(
        {
            "qwenpaw_action_id": str(request.action_id),
            "qwenpaw_action_attempt": str(request.attempt),
            "qwenpaw_action_idempotency_mode": (
                request.idempotency_mode.value
            ),
            "qwenpaw_action_idempotency_key": request.idempotency_key,
            "qwenpaw_action_executor_item_id": (
                request.executor_item_id or context.tool_call_id
            ),
        },
    )
    return values


def _text_block(text: str) -> Any:
    return TextBlock(type="text", text=text)


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    model_dump_json = getattr(value, "model_dump_json", None)
    if callable(model_dump_json):
        return model_dump_json(indent=2)
    try:
        return json.dumps(value, ensure_ascii=False, default=str, indent=2)
    except TypeError:
        return str(value)


def _blocks_from_mcp_content(content: Any) -> list[Any]:
    blocks: list[Any] = []
    for item in content or []:
        text = getattr(item, "text", None)
        if text is not None:
            blocks.append(_text_block(str(text)))
            continue

        data = getattr(item, "data", None)
        mime_type = getattr(item, "mimeType", None)
        if data is not None and mime_type:
            blocks.append(
                DataBlock(
                    source=Base64Source(
                        type="base64",
                        media_type=str(mime_type),
                        data=str(data),
                    ),
                ),
            )
            continue

        resource = getattr(item, "resource", None)
        if resource is not None:
            resource_text = getattr(resource, "text", None)
            blocks.append(
                _text_block(
                    (
                        str(resource_text)
                        if resource_text is not None
                        else _stringify(resource)
                    ),
                ),
            )
            continue

        blocks.append(_text_block(_stringify(item)))
    return blocks


def _blocks_from_value(value: Any) -> list[Any]:
    content = getattr(value, "content", None)
    is_mcp_call_result = content is not None and hasattr(value, "isError")
    if is_mcp_call_result:
        blocks = _blocks_from_mcp_content(content)
        structured = getattr(value, "structuredContent", None)
        if structured is not None:
            blocks.append(_text_block(_stringify(structured)))
        return blocks or [_text_block("")]
    return [_text_block(_stringify(value))]


def _tool_chunk_from_driver_result(result: DriverInvocationResult) -> Any:
    if result.ok:
        value = result.value
        state = (
            ToolResultState.ERROR
            if bool(getattr(value, "isError", False))
            else ToolResultState.SUCCESS
        )
        return ToolChunk(
            content=_blocks_from_value(value),
            state=state,
            is_last=True,
            metadata=dict(result.metadata or {}),
        )

    error_payload = {
        "ok": False,
        "type": result.error_type,
        "message": result.message,
        "metadata": result.metadata,
    }
    denied_error_types = {
        "driver_policy_approval_required",
        "driver_policy_denied",
    }
    return ToolChunk(
        content=[_text_block(_stringify(error_payload))],
        state=(
            ToolResultState.DENIED
            if result.error_type in denied_error_types
            else ToolResultState.ERROR
        ),
        is_last=True,
        metadata=dict(result.metadata or {}),
    )


class DriverCapabilityTool(ToolBase):
    """Expose one Driver capability as an AgentScope ToolBase instance."""

    name = ""
    description = ""
    input_schema: dict[str, Any] = {}
    is_concurrency_safe = False
    is_read_only = False
    is_external_tool = False
    is_state_injected = False
    is_mcp = False
    mcp_name = None

    def __init__(
        self,
        capability: DriverCapability,
        invoker: DriverInvoker,
        request_context: dict[str, str] | None = None,
    ) -> None:
        self.name = capability.exposure.tool_name or capability.name
        self.description = capability.description
        self.input_schema = dict(capability.input_schema or {})
        self._capability = capability
        self._invoker = invoker
        self._request_context = dict(request_context or {})

    async def check_permissions(
        self,
        *_args: Any,
        **_kwargs: Any,
    ) -> Any:
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Driver capability policy is handled by Driver.",
        )

    async def __call__(self, **kwargs: Any) -> Any:
        recorder = self._request_context.get("_action_recorder")
        if recorder is not None:
            from ...tool_calls._ctxvars import get_call_context

            context = get_call_context()
            if context is None:
                raise RuntimeError(
                    "driver action recorder requires supervised context",
                )
            effect, risk, reversible = _action_classification(
                self._capability,
            )
            await recorder.begin_driver_capability(
                context,
                provider_id=DEFAULT_DRIVER_PROVIDER_ID,
                capability_id=self._capability.capability_id,
                effect=effect,
                risk=risk,
                reversible=reversible,
                idempotency_mode=_action_idempotency(self._capability),
            )
        result = await self._invoker(
            DriverInvocation(
                capability_id=self._capability.capability_id,
                payload=dict(kwargs or {}),
                request_context=_driver_request_context(
                    self._request_context,
                ),
            ),
        )
        return _tool_chunk_from_driver_result(result)


class DriverDefinitionTool(ToolBase):
    """Adapt one public Driver definition to AgentScope at the edge."""

    name = ""
    description = ""
    input_schema: dict[str, Any] = {}
    is_concurrency_safe = False
    is_read_only = False
    is_external_tool = False
    is_state_injected = False
    is_mcp = False
    mcp_name = None

    def __init__(
        self,
        definition: DriverToolDefinition,
        request_context: dict[str, Any] | None = None,
    ) -> None:
        self.name = definition.name
        self.description = definition.description
        self.input_schema = dict(definition.input_schema)
        self._definition = definition
        self._request_context = request_context or {}

    async def check_permissions(
        self,
        *_args: Any,
        **_kwargs: Any,
    ) -> Any:
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Driver capability policy is handled by Driver.",
        )

    async def __call__(self, **kwargs: Any) -> Any:
        recorder = self._request_context.get("_action_recorder")
        if recorder is not None:
            from ...tool_calls._ctxvars import get_call_context

            context = get_call_context()
            if context is None:
                raise RuntimeError(
                    "driver action recorder requires supervised context",
                )
            await recorder.begin_driver(context, self._definition)
        try:
            result = await self._definition.invoke(dict(kwargs))
        except DriverApprovalRejectedError as exc:
            return ToolChunk(
                content=[
                    _text_block(
                        "Driver action was denied by its approval policy.",
                    ),
                ],
                state=ToolResultState.DENIED,
                is_last=True,
                metadata={
                    "error_type": "driver_approval_rejected",
                    "capability_id": exc.capability_id,
                },
            )
        if isinstance(result, DriverInvocationResult):
            return _tool_chunk_from_driver_result(result)
        return ToolChunk(
            content=_blocks_from_value(result),
            state=ToolResultState.SUCCESS,
            is_last=True,
        )


def adapt_driver_definitions(
    definitions: list[DriverToolDefinition],
    *,
    request_context: dict[str, Any] | None = None,
) -> list[ToolBase]:
    """Convert public Driver definitions at the AgentScope boundary."""
    return [
        DriverDefinitionTool(definition, request_context)
        for definition in definitions
    ]


async def build_driver_definitions(
    driver_manager: Any | None,
    request_context: dict[str, str],
) -> tuple[list[DriverToolDefinition], list[PromptFragment]]:
    """Build framework-neutral definitions from configured Drivers."""
    if driver_manager is None:
        return [], []

    try:
        capabilities = await driver_manager.list_capabilities(
            kind="tool",
            request_context=request_context,
        )
    except Exception:
        logger.debug(
            "Failed to build definitions from Driver capabilities",
            exc_info=True,
        )
        return [], []

    definitions: list[DriverToolDefinition] = []
    for capability in capabilities:
        if not getattr(capability.exposure, "as_tool", False):
            continue
        if not getattr(capability, "enabled", True):
            continue

        async def invoke(
            payload: dict[str, Any],
            *,
            capability_id: str = capability.capability_id,
        ) -> object:
            return await driver_manager.invoke_capability(
                DriverInvocation(
                    capability_id=capability_id,
                    payload=payload,
                    request_context=_driver_request_context(request_context),
                ),
            )

        effect, risk, reversible = _action_classification(capability)
        idempotency_mode = _action_idempotency(capability)
        definitions.append(
            DriverToolDefinition(
                provider_id=DEFAULT_DRIVER_PROVIDER_ID,
                capability_id=capability.capability_id,
                name=capability.exposure.tool_name or capability.name,
                description=capability.description,
                input_schema=dict(capability.input_schema or {}),
                effect=effect,
                risk=risk,
                reversible=reversible,
                idempotency_mode=idempotency_mode,
                invoke=invoke,
            ),
        )

    if not definitions:
        return [], []

    from ...agents.prompt import build_driver_policy_recheck_hint

    return definitions, [
        PromptFragment(
            fragment_id=(f"{DEFAULT_DRIVER_PROVIDER_ID}.policy-recheck"),
            content=build_driver_policy_recheck_hint(),
            priority=88,
        ),
    ]


async def build_driver_agent_tools(
    driver_manager: Any | None,
    request_context: dict[str, str],
) -> tuple[list[ToolBase], list[str]]:
    """Build AgentScope tools and prompt hints from active Drivers.

    Keeping this assembly here lets ``stream_query`` stay focused on request
    streaming while Driver remains responsible for capability discovery and
    the AgentScope adapter boundary.
    """
    if driver_manager is None:
        return [], []

    try:
        driver_capabilities = await driver_manager.list_capabilities(
            kind="tool",
            request_context=request_context,
        )
        tools: list[ToolBase] = [
            DriverCapabilityTool(
                capability,
                driver_manager.invoke_capability,
                request_context,
            )
            for capability in driver_capabilities
            if getattr(capability.exposure, "as_tool", False)
            and getattr(capability, "enabled", True)
        ]
    except Exception:
        logger.debug(
            "Failed to build AgentScope tools from Driver capabilities",
            exc_info=True,
        )
        return [], []

    if not tools:
        return [], []

    from ...agents.prompt import build_driver_policy_recheck_hint

    return tools, [build_driver_policy_recheck_hint()]
