# -*- coding: utf-8 -*-
"""Host-owned Conversation binding for legacy Cron declarations."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .models import CronJobSpec

logger = logging.getLogger(__name__)


class CronConversationBindingError(RuntimeError):
    """Raised when a Cron job cannot bind its declared Chat ownership."""


@dataclass(frozen=True, slots=True)
class CronConversationBinding:
    """Verified mapping from legacy dispatch identity to one ChatSpec."""

    conversation_id: str
    session_id: str
    user_id: str
    channel: str


class CronConversationBinder:
    """Resolve a Cron dispatch target without leaking it into Kernel."""

    def __init__(self, workspace: Any) -> None:
        self._workspace = workspace

    async def bind(
        self,
        job: CronJobSpec,
        *,
        session_id: str,
        required: bool,
    ) -> CronConversationBinding | None:
        """Create or resolve one Chat and verify its ownership fields."""
        manager = getattr(self._workspace, "chat_manager", None)
        if manager is None:
            if required:
                raise CronConversationBindingError(
                    "Cron Conversation binding requires ChatManager",
                )
            return None

        expected_user = job.dispatch.target.user_id or "cron"
        expected_channel = job.dispatch.channel
        try:
            chat = await manager.get_or_create_chat(
                session_id=session_id,
                user_id=expected_user,
                channel=expected_channel,
                name=job.name or f"Cron: {job.id}",
                source="cron",
            )
        except Exception as error:
            if required:
                raise CronConversationBindingError(
                    "Cron Conversation binding failed",
                ) from error
            return None

        conversation_id = getattr(chat, "id", None)
        actual = {
            "session_id": getattr(chat, "session_id", session_id),
            "user_id": getattr(chat, "user_id", expected_user),
            "channel": getattr(chat, "channel", expected_channel),
        }
        expected = {
            "session_id": session_id,
            "user_id": expected_user,
            "channel": expected_channel,
        }
        if not isinstance(conversation_id, str) or not conversation_id:
            raise CronConversationBindingError(
                "Cron ChatSpec has no stable Conversation identity",
            )
        if actual != expected:
            raise CronConversationBindingError(
                "Cron ChatSpec ownership does not match dispatch target",
            )
        return CronConversationBinding(
            conversation_id=conversation_id,
            session_id=session_id,
            user_id=expected_user,
            channel=expected_channel,
        )

    async def touch(
        self,
        binding: CronConversationBinding | None,
        *,
        required: bool,
    ) -> None:
        """Record activity without exposing ChatManager to the caller."""
        if binding is None:
            return
        manager = getattr(self._workspace, "chat_manager", None)
        if manager is None:
            if required:
                raise CronConversationBindingError(
                    "Cron Conversation binding lost ChatManager",
                )
            return
        try:
            await manager.touch_chat(binding.conversation_id)
        except Exception as error:
            if required:
                raise CronConversationBindingError(
                    "Cron Conversation activity update failed",
                ) from error
            logger.debug(
                "Cron Conversation activity update failed",
                exc_info=True,
            )


__all__ = [
    "CronConversationBinder",
    "CronConversationBinding",
    "CronConversationBindingError",
]
