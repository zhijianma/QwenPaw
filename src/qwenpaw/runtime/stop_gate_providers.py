# -*- coding: utf-8 -*-
"""Generation-pinned loop stop-gate assembly and evaluation."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from agentscope.message import Msg, TextBlock

from ..kernel.models import (
    JsonObject,
    StopGateAction,
    StopGateDecision,
    StopGateDefinition,
    StopGateInput,
    StopGateMessage,
)

logger = logging.getLogger(__name__)
_GATE_ID_PART_RE = re.compile(r"[^a-z0-9_.-]+")


def _json_metadata(value: Any) -> JsonObject:
    if not isinstance(value, dict):
        return {}
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _message_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content or "Agent response"
    parts: list[str] = []
    for block in content or []:
        value = (
            block.get("text")
            if isinstance(block, dict)
            else getattr(block, "text", None)
        )
        if isinstance(value, str) and value:
            parts.append(value)
    return "\n".join(parts) or "Agent response"


def message_to_stop_gate(message: Any) -> StopGateMessage:
    """Translate an Agent message into the stable loop contract."""
    return StopGateMessage(
        text=_message_text(message),
        metadata=_json_metadata(getattr(message, "metadata", None)),
    )


def stop_gate_input(final_msg: Any, iteration: int) -> StopGateInput:
    """Build bounded provider input after one reasoning iteration."""
    return StopGateInput(
        iteration=max(0, int(iteration)),
        has_tool_calls=final_msg is None,
        final_message=(
            message_to_stop_gate(final_msg) if final_msg is not None else None
        ),
    )


def _to_agent_message(message: StopGateMessage | None) -> Msg | None:
    if message is None:
        return None
    return Msg(
        name="assistant",
        role="assistant",
        content=[TextBlock(type="text", text=message.text)],
        metadata=message.metadata,
    )


def decision_to_legacy_result(decision: StopGateDecision) -> Any:
    """Translate a public decision for the existing Agent loop."""
    from ..loop.gates.base import StopAction, StopHandlerResult

    return StopHandlerResult(
        action=StopAction(decision.action.value),
        continuation_message=decision.continuation_message,
        reason=decision.reason,
        continuation_metadata=decision.continuation_metadata or None,
        final_message=_to_agent_message(decision.final_message),
        inject_on_tool_call=decision.inject_on_tool_call,
    )


def _legacy_decision(result: Any) -> StopGateDecision:
    return StopGateDecision(
        action=StopGateAction(result.action.value),
        continuation_message=result.continuation_message,
        reason=result.reason,
        continuation_metadata=_json_metadata(result.continuation_metadata),
        final_message=(
            message_to_stop_gate(result.final_message)
            if result.final_message is not None
            else None
        ),
        inject_on_tool_call=result.inject_on_tool_call,
    )


@dataclass(frozen=True)
class WorkspaceStopGateHost:
    """Fixed compatibility view of Workspace stop-handler registrations."""

    context: Any
    provider_id: str
    _definitions: tuple[StopGateDefinition, ...]
    _registrations: dict[str, Any]

    @classmethod
    def capture(
        cls,
        context: Any,
        provider_id: str,
    ) -> "WorkspaceStopGateHost":
        """Capture the current registration list for one invocation."""
        plugins = getattr(getattr(context, "workspace", None), "plugins", None)
        registrations = tuple(getattr(plugins, "stop_handlers", ()))
        definitions: list[StopGateDefinition] = []
        by_id: dict[str, Any] = {}
        for index, registration in enumerate(registrations):
            raw_name = (
                getattr(registration, "name", "")
                or getattr(registration, "plugin_id", "")
                or f"gate-{index}"
            )
            slug = (
                _GATE_ID_PART_RE.sub(
                    "-",
                    str(raw_name).casefold(),
                ).strip(".-")
                or f"gate-{index}"
            )
            gate_id = f"{provider_id}.{slug}"
            if gate_id in by_id:
                raise ValueError(f"duplicate stop-gate name '{raw_name}'")
            by_id[gate_id] = registration
            definitions.append(
                StopGateDefinition(
                    gate_id=gate_id,
                    provider_id=provider_id,
                    priority=int(getattr(registration, "priority", 100)),
                    scope=str(getattr(registration, "scope", "") or ""),
                ),
            )
        return cls(context, provider_id, tuple(definitions), by_id)

    def list_gates(self) -> Sequence[StopGateDefinition]:
        """Return the fixed compatibility catalog."""
        return self._definitions

    def is_active(self, gate_id: str) -> bool:
        """Evaluate the captured registration's scope predicate."""
        from ..loop.gates.runner import _registration_is_active

        registration = self._registrations.get(gate_id)
        if registration is None:
            raise LookupError(gate_id)
        return _registration_is_active(registration)

    async def evaluate(
        self,
        gate_id: str,
        gate_input: StopGateInput,
    ) -> StopGateDecision:
        """Run one captured registration with a reconstructed message."""
        from ..loop.gates.runner import run_stop_handlers

        registration = self._registrations.get(gate_id)
        if registration is None:
            raise LookupError(gate_id)
        final_msg = _to_agent_message(gate_input.final_message)
        result = await run_stop_handlers(
            [registration],
            agent=self.context.agent,
            final_msg=final_msg,
            iteration=gate_input.iteration,
        )
        return _legacy_decision(result)


class StopGateRouterSession:
    """Evaluate merged stop-gate catalogs from one pinned generation."""

    def __init__(self, sessions: Sequence[Any]) -> None:
        self._sessions = tuple(
            sorted(sessions, key=lambda item: item.provider_id),
        )
        self._gates: dict[str, tuple[Any, StopGateDefinition]] = {}
        for session in self._sessions:
            for definition in session.list_gates():
                if definition.provider_id != session.provider_id:
                    raise ValueError(
                        f"stop gate '{definition.gate_id}' has mismatched "
                        "provider ownership",
                    )
                if definition.gate_id in self._gates:
                    raise ValueError(
                        f"duplicate stop gate id '{definition.gate_id}'",
                    )
                self._gates[definition.gate_id] = (session, definition)

    def list_gates(self) -> tuple[StopGateDefinition, ...]:
        """Return the fixed merged catalog in evaluation order."""
        return tuple(
            definition
            for _, definition in self._ordered_candidates(include_all=True)
        )

    def _ordered_candidates(
        self,
        *,
        include_all: bool = False,
    ) -> list[tuple[Any, StopGateDefinition]]:
        ordered = sorted(
            self._gates.values(),
            key=lambda item: (item[1].priority, item[1].gate_id),
        )
        if include_all:
            return ordered
        active_scope = ""
        for session, definition in ordered:
            if definition.scope in {"", "default"}:
                continue
            if self._is_active(session, definition.gate_id):
                active_scope = definition.scope
                break
        return [
            (session, definition)
            for session, definition in ordered
            if not definition.scope
            or definition.scope == active_scope
            or (not active_scope and definition.scope == "default")
        ]

    @staticmethod
    def _is_active(session: Any, gate_id: str) -> bool:
        try:
            return bool(session.is_active(gate_id))
        except Exception:  # noqa: BLE001
            logger.warning(
                "Stop gate '%s' active check raised",
                gate_id,
                exc_info=True,
            )
            return False

    async def evaluate(self, gate_input: StopGateInput) -> StopGateDecision:
        """Return the first actionable decision in deterministic order."""
        for session, definition in self._ordered_candidates():
            try:
                decision = await session.evaluate(
                    definition.gate_id,
                    gate_input,
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Stop gate '%s' raised, skipping",
                    definition.gate_id,
                    exc_info=True,
                )
                continue
            if decision.action != StopGateAction.BYPASS:
                return decision
        return StopGateDecision(action=StopGateAction.TERMINATE)

    async def start_turn(self) -> None:
        """Prepare every provider session for a new user turn."""
        for session in self._sessions:
            await session.start_turn()

    async def reset_conversation(self) -> None:
        """Reset every provider session for the current conversation."""
        for session in self._sessions:
            await session.reset_conversation()

    async def close(self) -> None:
        """Close every provider session even if one close operation fails."""
        first_error: BaseException | None = None
        for session in reversed(self._sessions):
            try:
                await session.close()
            except BaseException as error:  # noqa: BLE001
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error


async def close_stop_gate_session(session: Any) -> None:
    """Close an optional stop-gate router session."""
    if session is not None:
        await session.close()


__all__ = [
    "StopGateRouterSession",
    "WorkspaceStopGateHost",
    "close_stop_gate_session",
    "decision_to_legacy_result",
    "message_to_stop_gate",
    "stop_gate_input",
]
