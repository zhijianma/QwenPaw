# -*- coding: utf-8 -*-
"""Lite adapter for durable, ChatSpec-owned Conversation forks."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..kernel import (
    ConversationForkCommand,
    ConversationForkConflictError,
    ConversationForkInvalidAnchorError,
    ConversationForkNotFoundError,
    ConversationForkOrigin,
    ConversationForkResult,
)

if TYPE_CHECKING:
    from ..app.chats.manager import ChatManager
    from ..app.chats.models import ChatSpec
    from ..app.chats.session import SafeJSONSession


class LiteConversationForkAdapter:
    """Implement the Kernel Fork port over the legacy Lite JSON stores."""

    def __init__(
        self,
        *,
        agent_id: str,
        manager: ChatManager,
        session: SafeJSONSession,
    ) -> None:
        self._agent_id = agent_id
        self._manager = manager
        self._session = session

    async def fork(
        self,
        command: ConversationForkCommand,
    ) -> ConversationForkResult:
        """Create an isolated child while hiding legacy storage handles."""
        from ..app.chats.manager import (
            ChatForkConflictError as LegacyForkConflictError,
            ChatForkNotFoundError as LegacyForkNotFoundError,
        )
        from ..app.chats.models import ChatForkRequest
        from ..app.chats.session import (
            SessionForkAnchorNotFoundError,
            SessionForkDestinationExistsError,
            SessionForkInvalidAnchorError,
            SessionForkSourceNotFoundError,
        )

        self._validate_agent(command.agent_id)

        async def write_snapshot(
            parent: ChatSpec,
            child: ChatSpec,
            source_message_id: str,
        ) -> None:
            await self._session.fork_session_state(
                source_session_id=parent.session_id,
                source_user_id=parent.user_id,
                source_channel=parent.channel,
                destination_session_id=child.session_id,
                destination_user_id=child.user_id,
                destination_channel=child.channel,
                source_message_id=source_message_id,
            )

        async def rollback_snapshot(child: ChatSpec) -> None:
            await self._session.delete_session_state(
                child.session_id,
                child.user_id,
                child.channel,
            )

        try:
            child = await self._manager.fork_chat(
                command.parent_chat_id,
                ChatForkRequest(
                    source_message_id=command.source_message_id,
                    idempotency_key=command.idempotency_key,
                    name=command.name,
                ),
                snapshot_writer=write_snapshot,
                snapshot_rollback=rollback_snapshot,
            )
        except LegacyForkConflictError as error:
            raise ConversationForkConflictError(str(error)) from error
        except (
            LegacyForkNotFoundError,
            SessionForkAnchorNotFoundError,
            SessionForkSourceNotFoundError,
        ) as error:
            raise ConversationForkNotFoundError(str(error)) from error
        except SessionForkDestinationExistsError as error:
            raise ConversationForkConflictError(str(error)) from error
        except SessionForkInvalidAnchorError as error:
            raise ConversationForkInvalidAnchorError(str(error)) from error

        lineage = await self.lineage(
            agent_id=command.agent_id,
            chat_id=child.id,
        )
        return ConversationForkResult(
            child_chat_id=child.id,
            origin=ConversationForkOrigin(
                agent_id=command.agent_id,
                parent_chat_id=command.parent_chat_id,
                root_chat_id=lineage[-1],
                source_message_id=command.source_message_id,
                boundary=command.boundary,
                depth=len(lineage) - 1,
                created_at=child.created_at,
            ),
        )

    async def lineage(
        self,
        *,
        agent_id: str,
        chat_id: str | None = None,
        conversation_id: str | None = None,
    ) -> tuple[str, ...]:
        """Return trusted child-to-root ChatSpec identities."""
        self._validate_agent(agent_id)
        if (
            chat_id is not None
            and conversation_id is not None
            and chat_id != conversation_id
        ):
            raise ValueError(
                "chat_id and conversation_id must identify one Chat",
            )
        resolved_chat_id = chat_id or conversation_id
        if not resolved_chat_id:
            raise ValueError("chat_id cannot be empty")
        lineage = await self._manager.get_fork_lineage_ids(resolved_chat_id)
        if not lineage:
            raise ConversationForkNotFoundError(resolved_chat_id)
        return lineage

    def _validate_agent(self, agent_id: str) -> None:
        if agent_id != self._agent_id:
            raise ConversationForkNotFoundError(
                "conversation does not belong to this agent",
            )
