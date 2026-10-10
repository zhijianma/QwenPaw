# -*- coding: utf-8 -*-
"""Lite adapter for durable, ChatSpec-owned Conversation forks."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentscope.message import Msg, TextBlock
from agentscope.state import AgentState

from ..kernel import (
    ConversationForkCommand,
    ConversationForkConflictError,
    ConversationForkInvalidAnchorError,
    ConversationForkNotFoundError,
    ConversationForkOrigin,
    ConversationForkResult,
    TaskConversationHistoryPort,
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
        task_history: TaskConversationHistoryPort | None = None,
    ) -> None:
        self._agent_id = agent_id
        self._manager = manager
        self._session = session
        self._task_history = task_history

    async def _fork_combined_history(
        self,
        *,
        parent: ChatSpec,
        child: ChatSpec,
        source_message_id: str,
    ) -> None:
        """Fork a merged Session and Task transcript through one anchor."""
        if self._task_history is None:
            raise ConversationForkNotFoundError(source_message_id)
        transcript = await self._task_history.read_messages_for_conversation(
            parent.id,
        )
        task_messages = [
            Msg(
                id=record.message_id,
                name=record.role,
                role=record.role,
                content=[TextBlock(type="text", text=record.text)],
                metadata={
                    "source": "task_ledger",
                    "task_id": str(record.task_id),
                    "run_id": (
                        str(record.run_id)
                        if record.run_id is not None
                        else None
                    ),
                    "artifact_refs": [
                        item.model_dump(mode="json")
                        for item in record.artifact_refs
                    ],
                    "evidence_refs": [
                        item.model_dump(mode="json")
                        for item in record.evidence_refs
                    ],
                },
                created_at=record.created_at.isoformat(),
                finished_at=(
                    record.completed_at.isoformat()
                    if record.status == "completed"
                    else None
                ),
            )
            for record in transcript.messages
        ]
        raw_state = await self._session.get_session_state_dict(
            parent.session_id,
            parent.user_id,
            parent.channel,
        )
        session_messages: list[Msg] = []
        summary = None
        agent_raw = raw_state.get("agent") if raw_state else None
        state_raw = (
            agent_raw.get("state") if isinstance(agent_raw, dict) else None
        )
        if isinstance(state_raw, dict):
            parent_state = AgentState.model_validate(state_raw)
            session_messages = list(parent_state.context)
            summary = parent_state.summary
        merged: dict[str, Msg] = {
            message.id: message
            for message in (*session_messages, *task_messages)
        }
        messages = sorted(
            merged.values(),
            key=lambda message: (message.created_at, message.id),
        )
        if not any(message.id == source_message_id for message in messages):
            raise ConversationForkNotFoundError(source_message_id)
        await self._session.fork_message_history(
            messages=messages,
            summary=summary,
            destination_session_id=child.session_id,
            destination_user_id=child.user_id,
            destination_channel=child.channel,
            source_message_id=source_message_id,
        )

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
            try:
                await self._session.fork_session_state(
                    source_session_id=parent.session_id,
                    source_user_id=parent.user_id,
                    source_channel=parent.channel,
                    destination_session_id=child.session_id,
                    destination_user_id=child.user_id,
                    destination_channel=child.channel,
                    source_message_id=source_message_id,
                )
            except (
                SessionForkAnchorNotFoundError,
                SessionForkSourceNotFoundError,
            ):
                await self._fork_combined_history(
                    parent=parent,
                    child=child,
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
