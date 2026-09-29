# -*- coding: utf-8 -*-
"""Heartbeat adapter for the shared Lite Scheduled Task Runtime."""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import uuid4

from ...delivery import (
    SYSTEM_CHANNEL_DELIVERY_ID,
    SYSTEM_INBOX_ADDRESS,
    SYSTEM_INBOX_DELIVERY_ID,
    encode_channel_address,
)
from ...kernel import (
    ApprovalLevel,
    AutonomyLevel,
    DeliveryDestination,
    DeliveryKind,
    DeliveryMode,
    DeliveryPolicy,
    ExecutionContract,
    ScheduleDefinition,
    ScheduleTrigger,
    TimeoutPolicy,
)
from ...tasks.system_contributions import (
    SYSTEM_BASIC_PLANNER_ID,
    SYSTEM_CONSOLE_RUNNER_ID,
    SYSTEM_DEFAULT_STRATEGY_ID,
)
from .contracts import HeartbeatExecutionRequest
from .heartbeat import (
    is_cron_expression,
    parse_heartbeat_cron,
    parse_heartbeat_every,
)
from .scheduled_task_runtime import LiteScheduledTaskRuntime

HEARTBEAT_QUIET_RESULT = "HEARTBEAT_OK"


class LiteHeartbeatTaskRuntime:  # pylint: disable=too-few-public-methods
    """Translate Heartbeat compatibility inputs into Kernel contracts."""

    def __init__(self, workspace: Any) -> None:
        self._workspace = workspace
        self._scheduled = LiteScheduledTaskRuntime(workspace)

    async def execute(
        self,
        request: HeartbeatExecutionRequest,
    ) -> dict[str, Any]:
        """Bind a Chat and run one idempotent Heartbeat occurrence."""
        conversation_id = await self._bind_conversation(
            channel=request.channel,
            user_id=request.user_id,
            transport_context=request.transport_context,
        )
        definition = self._definition(
            request,
            conversation_id=conversation_id,
        )
        fire_key = (
            f"scheduled:heartbeat:{request.scheduled_for.isoformat()}"
            if request.trigger == "scheduled"
            else f"manual:heartbeat:{uuid4()}"
        )
        return await self._scheduled.execute(
            definition,
            scheduled_for=request.scheduled_for,
            idempotency_key=fire_key,
            timeout_seconds=request.timeout_seconds,
            owner_prefix="heartbeat",
        )

    async def _bind_conversation(
        self,
        *,
        channel: str,
        user_id: str,
        transport_context: str,
    ) -> str:
        manager = getattr(self._workspace, "chat_manager", None)
        if manager is None:
            raise RuntimeError("Heartbeat requires ChatManager")
        chat = await manager.get_or_create_chat(
            session_id=transport_context,
            user_id=user_id,
            channel=channel,
            name="Heartbeat",
            source="cron",
        )
        conversation_id = getattr(chat, "id", None)
        if not isinstance(conversation_id, str) or not conversation_id:
            raise RuntimeError("Heartbeat Chat has no stable identity")
        return conversation_id

    def _definition(
        self,
        request: HeartbeatExecutionRequest,
        *,
        conversation_id: str,
    ) -> ScheduleDefinition:
        objective = self._objective(request.query_text, request.target)
        policy = self._delivery_policy(
            target=request.target,
            conversation_id=conversation_id,
            channel=request.channel,
            user_id=request.user_id,
            transport_context=request.transport_context,
        )
        return ScheduleDefinition(
            schedule_id=self._schedule_id(),
            agent_id=self._workspace.agent_id,
            conversation_id=conversation_id,
            name="Heartbeat",
            objective=objective,
            trigger=self._trigger(request.every),
            planner_id=SYSTEM_BASIC_PLANNER_ID,
            runner_id=SYSTEM_CONSOLE_RUNNER_ID,
            strategy_id=SYSTEM_DEFAULT_STRATEGY_ID,
            execution_contract=ExecutionContract(
                goal=objective,
                autonomy_level=AutonomyLevel.CONTROLLED_BACKGROUND,
                timeout_policy=TimeoutPolicy(
                    attempt_seconds=request.timeout_seconds,
                ),
            ),
            misfire_grace_seconds=60,
            metadata={
                "source": "heartbeat",
                "approval_level": ApprovalLevel.OFF.value,
                "delivery_policy": policy.model_dump(mode="json"),
            },
        )

    def _schedule_id(self) -> str:
        digest = hashlib.sha256(
            self._workspace.agent_id.encode("utf-8"),
        ).hexdigest()[:20]
        return f"qwenpaw.system.heartbeat.agent-{digest}"

    @staticmethod
    def _trigger(every: str) -> ScheduleTrigger:
        if is_cron_expression(every):
            cron = " ".join(parse_heartbeat_cron(every))
            return ScheduleTrigger(kind="cron", cron=cron)
        return ScheduleTrigger(
            kind="interval",
            interval_seconds=parse_heartbeat_every(every),
        )

    @staticmethod
    def _objective(query_text: str, target: str) -> str:
        if target not in {"last", "inbox"}:
            return query_text
        return (
            f"{query_text}\n\n"
            "If there is no change or action that needs user attention, "
            f"respond with exactly {HEARTBEAT_QUIET_RESULT}."
        )

    @staticmethod
    def _delivery_policy(
        *,
        target: str,
        conversation_id: str,
        channel: str,
        user_id: str,
        transport_context: str,
    ) -> DeliveryPolicy:
        if target == "last":
            destination = DeliveryDestination(
                adapter_id=SYSTEM_CHANNEL_DELIVERY_ID,
                address=encode_channel_address(
                    channel=channel,
                    user_id=user_id,
                    transport_context=transport_context,
                ),
                conversation_id=conversation_id,
            )
            kinds = (DeliveryKind.RESULT,)
        else:
            destination = DeliveryDestination(
                adapter_id=SYSTEM_INBOX_DELIVERY_ID,
                address=SYSTEM_INBOX_ADDRESS,
                conversation_id=conversation_id,
            )
            kinds = (DeliveryKind.RESULT,) if target == "inbox" else ()
        return DeliveryPolicy(
            destination=destination,
            mode=DeliveryMode.FINAL,
            kinds=kinds,
            suppress_empty_text=True,
            suppress_exact_text=(HEARTBEAT_QUIET_RESULT,),
        )


__all__ = ["HEARTBEAT_QUIET_RESULT", "LiteHeartbeatTaskRuntime"]
