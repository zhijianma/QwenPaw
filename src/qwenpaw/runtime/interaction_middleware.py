# -*- coding: utf-8 -*-
"""AgentScope middleware for invocation-scoped user interaction."""

from __future__ import annotations

from typing import Any, AsyncGenerator, Callable

from agentscope.message import Msg, TextBlock
from agentscope.middleware import MiddlewareBase

from ..invocation_control import SteerDelivery
from ..kernel import SteerSafePoint


def _steering_session(agent: Any) -> Any | None:
    """Return the invocation-bound session from an Agent request context."""
    request_context = getattr(agent, "_request_context", None) or {}
    return request_context.get("_steering_session")


async def append_pending_steers(
    agent: Any,
    safe_point: SteerSafePoint,
) -> int:
    """Append accepted steers as auditable user messages."""
    session = _steering_session(agent)
    if session is None:
        return 0

    async def inject(
        delivery: SteerDelivery,
        actual_safe_point: SteerSafePoint,
    ) -> None:
        append_steer_delivery(agent, delivery, actual_safe_point)

    return await session.apply_pending(safe_point, inject)


def append_steer_delivery(
    agent: Any,
    delivery: SteerDelivery,
    safe_point: SteerSafePoint,
) -> None:
    """Append one claimed steer using the shared auditable message shape."""
    agent.state.context.append(
        Msg(
            name="user",
            role="user",
            content=[TextBlock(type="text", text=delivery.instruction)],
            metadata={
                "qwenpaw_control_command_id": str(delivery.command_id),
                "qwenpaw_steer_safe_point": safe_point.value,
            },
        ),
    )


class RuntimeInteractionMiddleware(MiddlewareBase):
    """Deliver runtime controls around AgentScope reasoning."""

    async def on_reasoning(
        self,
        agent: Any,
        input_kwargs: dict[str, Any],  # pylint: disable=unused-argument
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        """Apply steers before reasoning and before final text commits."""
        await append_pending_steers(
            agent,
            SteerSafePoint.BEFORE_REASONING,
        )

        final_message = None
        async for item in next_handler():
            if isinstance(item, Msg):
                final_message = item
            else:
                yield item

        if final_message is None:
            return
        if await append_pending_steers(
            agent,
            SteerSafePoint.AFTER_REASONING,
        ):
            setattr(agent, "_steer_forced_continue", True)
            return
        yield final_message


__all__ = [
    "RuntimeInteractionMiddleware",
    "append_pending_steers",
    "append_steer_delivery",
]
