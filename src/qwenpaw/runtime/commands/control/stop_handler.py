# -*- coding: utf-8 -*-
"""Handler for /stop command.

The /stop command immediately terminates an ongoing agent task.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from uuid import uuid4

from ....invocation_control import (
    QueueCommandConflictError,
    QueueRevisionConflictError,
)
from ....kernel import ControlCommandStatus
from .base import BaseControlCommandHandler, ControlContext

logger = logging.getLogger(__name__)


class StopCommandHandler(BaseControlCommandHandler):
    """Handler for /stop command.

    Features:
    - Immediate response (priority level 0)
    - Submits ``interrupt_current`` through the OS control plane
    - Falls back to TaskTracker only for a legacy run without a live binding
    - Default: stops current session
    - Optional: specify target session_id
    - Never clears queued messages; that is a separate user intent

    Usage:
        /stop                  # Stop current session
        /stop session=console:user1  # Stop specific session
    """

    command_name = "/stop"

    async def handle(self, context: ControlContext) -> str:
        """Handle /stop command.

        Args:
            context: Control command context

        Returns:
            Response text (success or error message)
        """
        target_session_id = context.args.get(
            "session",
            context.session_id,
        )

        logger.info(
            f"/stop command: current_session={context.session_id[:30]} "
            f"target_session={target_session_id[:30]} "
            f"user_id={context.user_id}",
        )

        workspace = context.workspace
        channel_id = context.channel.channel
        observation_id = f"channel-stop:{_message_identity(context.payload)}"

        # Scope the lookup to the requesting user so users sharing the same
        # session_id (group members, or DM users whose conversation_id suffix
        # collides) can only stop their own task.
        chat_id = await workspace.chat_manager.get_chat_id_by_session(
            target_session_id,
            channel_id,
            user_id=context.user_id,
        )

        if chat_id is None:
            await _observe_legacy_stop(
                workspace,
                observation_id=observation_id,
                entrypoint="channel.slash_stop",
                disposition="chat_not_found",
            )
            logger.warning(
                f"/stop: No active chat found for "
                f"session={target_session_id[:30]} channel={channel_id}",
            )
            return (
                f"**No Active Task**\n\n"
                f"No running task found for session "
                f"`{target_session_id[:40]}`."
            )

        control = getattr(workspace, "invocation_control", None)
        receipt = None
        if control is not None:
            try:
                receipt = await control.interrupt_current(
                    agent_id=workspace.agent_id,
                    conversation_id=chat_id,
                    idempotency_key=observation_id,
                )
            except (QueueCommandConflictError, QueueRevisionConflictError):
                logger.info(
                    "/stop: invocation changed before interrupt applied "
                    "chat_id=%s",
                    chat_id,
                )
        if (
            receipt is not None
            and receipt.status is ControlCommandStatus.APPLIED
        ):
            await _observe_legacy_stop(
                workspace,
                observation_id=observation_id,
                entrypoint="channel.slash_stop",
                disposition="os_interrupt",
            )
            logger.info(
                "/stop: OS interrupt applied chat_id=%s session=%s",
                chat_id,
                target_session_id[:30],
            )
            return (
                f"**Task Stopped**\n\n"
                f"Session `{target_session_id[:40]}`: "
                f"running invocation interrupted."
            )

        stopped = await workspace.task_tracker.request_stop(chat_id)
        if receipt is not None and receipt.command_id is not None:
            await control.acknowledge_interrupt(
                receipt.command_id,
                applied=stopped,
                detail=(
                    "channel compatibility cancellation applied"
                    if stopped
                    else "runtime binding and compatibility run unavailable"
                ),
            )
        if stopped:
            await _observe_legacy_stop(
                workspace,
                observation_id=observation_id,
                entrypoint="channel.slash_stop",
                disposition="compatibility_cancelled",
            )
            logger.info(
                "/stop: compatibility cancellation applied chat_id=%s "
                "session=%s",
                chat_id,
                target_session_id[:30],
            )
            return (
                f"**Task Stopped**\n\n"
                f"Session `{target_session_id[:40]}`: "
                f"running invocation interrupted."
            )

        await _observe_legacy_stop(
            workspace,
            observation_id=observation_id,
            entrypoint="channel.slash_stop",
            disposition="no_active_invocation",
        )
        logger.warning(
            "/stop: Nothing to stop: chat_id=%s session=%s",
            chat_id,
            target_session_id[:30],
        )
        return (
            f"**Task Not Running**\n\n"
            f"No active invocation for session "
            f"`{target_session_id[:40]}`."
        )


def _message_identity(payload) -> str:
    """Return a stable channel message identity when the adapter has one."""
    if isinstance(payload, dict):
        meta = payload.get("meta")
        candidates = (
            payload.get("message_id"),
            payload.get("id"),
            meta.get("message_id") if isinstance(meta, dict) else None,
        )
    else:
        inputs = getattr(payload, "input", None) or ()
        first = inputs[0] if inputs else None
        candidates = (
            getattr(payload, "message_id", None),
            getattr(first, "id", None),
        )
    return next(
        (str(candidate) for candidate in candidates if candidate),
        str(uuid4()),
    )


async def _observe_legacy_stop(
    workspace,
    *,
    observation_id: str,
    entrypoint: str,
    disposition: str,
) -> None:
    """Call the optional Host observer without coupling Runtime to App."""
    observer = getattr(workspace, "legacy_stop_compatibility", None)
    record = getattr(observer, "record", None)
    if not callable(record):
        return
    try:
        result = record(
            observation_id=observation_id,
            entrypoint=entrypoint,
            disposition=disposition,
        )
        if inspect.isawaitable(result):
            await asyncio.wait_for(result, timeout=0.25)
    except Exception:  # pylint: disable=broad-except
        logger.warning(
            "legacy stop compatibility observation failed",
            exc_info=True,
        )
