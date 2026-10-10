# -*- coding: utf-8 -*-
"""End-to-end unit coverage for persisted Chat message forks."""

# pylint: disable=protected-access

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from agentscope.message import Msg, TextBlock
from agentscope.state import AgentState
from fastapi import HTTPException

from qwenpaw.app.chats.api import (
    fork_chat,
    get_chat_artifact_content,
    list_chat_artifacts,
)
from qwenpaw.app.chats.manager import ChatManager
from qwenpaw.app.chats.models import ChatForkRequest, ChatSpec
from qwenpaw.app.chats.repo import JsonChatRepository
from qwenpaw.app.chats.session import (
    SafeJSONSession,
    SessionForkAnchorNotFoundError,
    SessionForkInvalidAnchorError,
)
from qwenpaw.conversations import LiteConversationForkAdapter
from qwenpaw.interactions import InteractionService
from qwenpaw.invocation_control import InvocationControlService
from qwenpaw.kernel import (
    ActorRef,
    ActorType,
    ControlCommandStatus,
    ConversationForkCommand,
    ConversationForkNotFoundError,
    ConversationForkPort,
    EvidenceRef,
    InteractionKind,
    InteractionMode,
    InteractionOption,
    InteractionRequest,
    InteractionResponse,
    SteerSafePoint,
    SubmissionStatus,
    PlanStep,
    RunnerSignal,
    TurnSubmissionRequest,
)
from qwenpaw.plugins.generations import GenerationRegistry
from qwenpaw.constant import QWENPAW_USER_CONTENT_KEY
from qwenpaw.schemas import FileContent
from qwenpaw.tasks.artifacts import lite_artifact_store
from qwenpaw.tasks.ledger import SQLiteExecutionLedger
from qwenpaw.tasks.service import TaskService
from qwenpaw.tasks.conversation_artifacts import (
    conversation_artifact_receipts,
)


class _StateModule:
    def __init__(self, state: AgentState) -> None:
        self.state = state

    def state_dict(self) -> dict:
        return {"state": self.state.model_dump(mode="json")}


def _message(message_id: str, role: str, text: str) -> Msg:
    return Msg(
        id=message_id,
        name=role,
        role=role,
        content=[TextBlock(type="text", text=text)],
        metadata={
            "artifact_ref": {
                "artifact_id": f"artifact:{message_id}",
                "version": 1,
            },
            "evidence_ref": {
                "evidence_id": f"evidence:{message_id}",
                "version": 1,
            },
            "artifact_receipt": f"receipt:{message_id}",
        },
    )


def _attachment_message(
    message_id: str,
    *,
    artifact,
    evidence: EvidenceRef,
    receipt_id: str,
) -> Msg:
    content = FileContent(
        filename="shared.txt",
        file_url="shared.txt",
        artifact_ref=artifact.model_dump(mode="json"),
        evidence_ref=evidence.model_dump(mode="json"),
        artifact_receipt=receipt_id,
    )
    return Msg(
        id=message_id,
        name="user",
        role="user",
        content=[TextBlock(type="text", text="Read shared.txt")],
        metadata={
            QWENPAW_USER_CONTENT_KEY: [
                content.model_dump(mode="json", exclude_none=True),
            ],
        },
    )


async def _seed_parent(
    tmp_path,
) -> tuple[ChatManager, SafeJSONSession, ChatSpec]:
    manager = ChatManager(
        repo=JsonChatRepository(tmp_path / "chats.json"),
    )
    session = SafeJSONSession(str(tmp_path / "sessions"))
    parent = await manager.create_chat(
        ChatSpec(
            id="parent-chat",
            name="Parent",
            session_id="console:parent",
            user_id="local-user",
            channel="console",
        ),
    )
    state = AgentState(
        session_id=parent.session_id,
        summary="Earlier compacted context",
        context=[
            _message("user-1", "user", "first question"),
            _message("assistant-1", "assistant", "first answer"),
            _message("user-2", "user", "later question"),
        ],
    )
    state.reply_context.cur_iter = 4
    state.tool_context.activated_groups = ["temporary-tools"]
    state.middle_context = {"ephemeral": "do-not-inherit"}
    await session.save_session_state(
        parent.session_id,
        parent.user_id,
        parent.channel,
        agent=_StateModule(state),
    )
    return manager, session, parent


@pytest.mark.asyncio
async def test_session_fork_truncates_and_resets_runtime_contexts(
    tmp_path,
) -> None:
    _, session, parent = await _seed_parent(tmp_path)

    count = await session.fork_session_state(
        source_session_id=parent.session_id,
        source_user_id=parent.user_id,
        source_channel=parent.channel,
        destination_session_id="console:fork:child",
        destination_user_id=parent.user_id,
        destination_channel=parent.channel,
        source_message_id="assistant-1",
    )
    raw = await session.get_session_state_dict(
        "console:fork:child",
        parent.user_id,
        parent.channel,
    )
    child_state = AgentState.model_validate(raw["agent"]["state"])

    assert count == 2
    assert child_state.session_id == "console:fork:child"
    assert child_state.summary == "Earlier compacted context"
    assert [message.id for message in child_state.context] == [
        "user-1",
        "assistant-1",
    ]
    assert child_state.context[-1].metadata == {
        "artifact_ref": {
            "artifact_id": "artifact:assistant-1",
            "version": 1,
        },
        "evidence_ref": {
            "evidence_id": "evidence:assistant-1",
            "version": 1,
        },
        "artifact_receipt": "receipt:assistant-1",
    }
    assert child_state.reply_context.cur_iter == 0
    assert child_state.reply_context.structured_output is None
    assert child_state.permission_context.allow_rules == {}
    assert child_state.permission_context.deny_rules == {}
    assert child_state.tool_context.activated_groups == []
    assert child_state.tasks_context.tasks == []
    assert child_state.middle_context == {}


@pytest.mark.asyncio
async def test_session_fork_rejects_unknown_message_without_output(
    tmp_path,
) -> None:
    _, session, parent = await _seed_parent(tmp_path)

    with pytest.raises(SessionForkAnchorNotFoundError):
        await session.fork_session_state(
            source_session_id=parent.session_id,
            source_user_id=parent.user_id,
            source_channel=parent.channel,
            destination_session_id="console:fork:missing",
            destination_user_id=parent.user_id,
            destination_channel=parent.channel,
            source_message_id="missing",
        )

    assert (
        await session.get_session_state_dict(
            "console:fork:missing",
            parent.user_id,
            parent.channel,
        )
        == {}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("source_message_id", ["user-1", "assistant-mid"])
async def test_session_fork_rejects_non_turn_boundary(
    tmp_path,
    source_message_id: str,
) -> None:
    _, session, parent = await _seed_parent(tmp_path)
    state = AgentState(
        session_id=parent.session_id,
        context=[
            _message("user-1", "user", "first question"),
            _message("assistant-mid", "assistant", "tool planning"),
            _message("assistant-final", "assistant", "final answer"),
        ],
    )
    await session.save_session_state(
        parent.session_id,
        parent.user_id,
        parent.channel,
        agent=_StateModule(state),
    )

    with pytest.raises(
        SessionForkInvalidAnchorError,
        match="completed assistant reply",
    ):
        await session.fork_session_state(
            source_session_id=parent.session_id,
            source_user_id=parent.user_id,
            source_channel=parent.channel,
            destination_session_id=f"console:fork:{source_message_id}",
            destination_user_id=parent.user_id,
            destination_channel=parent.channel,
            source_message_id=source_message_id,
        )


@pytest.mark.asyncio
async def test_fork_api_maps_invalid_turn_boundary_to_conflict(
    tmp_path,
) -> None:
    manager, session, parent = await _seed_parent(tmp_path)
    workspace = SimpleNamespace(
        agent_id="default",
        task_tracker=SimpleNamespace(
            get_status=AsyncMock(return_value="idle"),
        ),
    )

    with pytest.raises(HTTPException) as invalid:
        await fork_chat(
            parent.id,
            ChatForkRequest(
                source_message_id="user-1",
                idempotency_key="invalid-user-anchor",
            ),
            manager,
            session,
            workspace,
        )

    assert invalid.value.status_code == 409
    assert "completed assistant reply" in invalid.value.detail
    assert len(await manager.list_chats()) == 1


@pytest.mark.asyncio
async def test_session_fork_rejects_unfinished_modern_reply(tmp_path) -> None:
    _, session, parent = await _seed_parent(tmp_path)
    completed = _message("assistant-1", "assistant", "first answer")
    completed.finished_at = "2026-09-28T10:00:00+00:00"
    state = AgentState(
        session_id=parent.session_id,
        context=[
            _message("user-1", "user", "first question"),
            completed,
            _message("user-2", "user", "second question"),
            _message("assistant-2", "assistant", "partial answer"),
        ],
    )
    await session.save_session_state(
        parent.session_id,
        parent.user_id,
        parent.channel,
        agent=_StateModule(state),
    )

    with pytest.raises(SessionForkInvalidAnchorError):
        await session.fork_session_state(
            source_session_id=parent.session_id,
            source_user_id=parent.user_id,
            source_channel=parent.channel,
            destination_session_id="console:fork:unfinished",
            destination_user_id=parent.user_id,
            destination_channel=parent.channel,
            source_message_id="assistant-2",
        )


@pytest.mark.asyncio
async def test_fork_api_creates_isolated_idempotent_child(tmp_path) -> None:
    manager, session, parent = await _seed_parent(tmp_path)
    tracker = SimpleNamespace(
        get_status=AsyncMock(return_value="idle"),
    )
    payload = ChatForkRequest(
        source_message_id="assistant-1",
        idempotency_key="fork-click-1",
        name="Alternative",
    )

    workspace = SimpleNamespace(agent_id="default", task_tracker=tracker)
    child, concurrent_replay = await asyncio.gather(
        fork_chat(parent.id, payload, manager, session, workspace),
        fork_chat(parent.id, payload, manager, session, workspace),
    )
    replay = await fork_chat(
        parent.id,
        payload,
        manager,
        session,
        workspace,
    )

    assert concurrent_replay.id == child.id
    assert replay.id == child.id
    assert child.id != parent.id
    assert child.session_id != parent.session_id
    assert child.fork_origin is not None
    assert child.fork_origin.parent_chat_id == parent.id
    assert child.fork_origin.source_message_id == "assistant-1"
    tracker.get_status.assert_not_awaited()
    persisted = await manager.list_chats()
    assert [chat.id for chat in persisted].count(child.id) == 1


@pytest.mark.asyncio
async def test_lite_adapter_implements_kernel_fork_contract(tmp_path) -> None:
    manager, session, parent = await _seed_parent(tmp_path)
    adapter = LiteConversationForkAdapter(
        agent_id="default",
        manager=manager,
        session=session,
    )
    command = ConversationForkCommand(
        agent_id="default",
        parent_chat_id=parent.id,
        source_message_id="assistant-1",
        idempotency_key="kernel-fork-1",
    )

    result = await adapter.fork(command)
    replay = await adapter.fork(command)

    assert isinstance(adapter, ConversationForkPort)
    assert replay.child_chat_id == result.child_chat_id
    assert result.origin.parent_chat_id == parent.id
    assert result.origin.root_chat_id == parent.id
    assert result.origin.depth == 1
    assert await adapter.lineage(
        agent_id="default",
        chat_id=result.child_chat_id,
    ) == (result.child_chat_id, parent.id)
    assert await adapter.lineage(
        agent_id="default",
        conversation_id=result.child_chat_id,
    ) == (result.child_chat_id, parent.id)
    with pytest.raises(ValueError, match="must identify one Chat"):
        await adapter.lineage(
            agent_id="default",
            chat_id=result.child_chat_id,
            conversation_id=parent.id,
        )

    nested = await adapter.fork(
        command.model_copy(
            update={
                "parent_chat_id": result.child_chat_id,
                "idempotency_key": "kernel-fork-2",
            },
        ),
    )
    assert nested.origin.root_chat_id == parent.id
    assert nested.origin.depth == 2

    with pytest.raises(ConversationForkNotFoundError):
        await adapter.lineage(
            agent_id="another-agent",
            chat_id=result.child_chat_id,
        )


@pytest.mark.asyncio
async def test_fork_api_allows_completed_anchor_while_parent_running(
    tmp_path,
) -> None:
    manager, session, parent = await _seed_parent(tmp_path)
    workspace = SimpleNamespace(
        agent_id="default",
        task_tracker=SimpleNamespace(
            get_status=AsyncMock(return_value="running"),
        ),
    )

    first = ChatForkRequest(
        source_message_id="assistant-1",
        idempotency_key="fork-click-active-parent",
    )
    child = await fork_chat(
        parent.id,
        first,
        manager,
        session,
        workspace,
    )

    assert child.fork_origin is not None
    assert child.fork_origin.source_message_id == "assistant-1"
    workspace.task_tracker.get_status.assert_not_awaited()
    with pytest.raises(HTTPException) as conflict:
        await fork_chat(
            parent.id,
            first.model_copy(update={"source_message_id": "user-1"}),
            manager,
            session,
            workspace,
        )
    assert conflict.value.status_code == 409


@pytest.mark.asyncio
async def test_fork_api_branches_task_transcript_without_session_state(
    tmp_path,
) -> None:
    manager = ChatManager(
        repo=JsonChatRepository(tmp_path / "chats.json"),
    )
    session = SafeJSONSession(str(tmp_path / "sessions"))
    parent = await manager.create_chat(
        ChatSpec(
            id="scheduled-parent",
            name="Scheduled parent",
            session_id="console:scheduled-parent",
            user_id="local-user",
            channel="console",
        ),
    )
    ledger = SQLiteExecutionLedger(
        tmp_path / ".qwenpaw" / "lite" / "tasks.db",
    )
    await ledger.initialize()
    service = TaskService(store=ledger, registry_generation=7)
    task = await service.create_task(
        objective="Scheduled prompt",
        agent_id="default",
        metadata={"conversation_id": parent.id},
    )
    await service.plan_task(
        task.task_id,
        steps=(PlanStep(title="Answer", objective=task.objective),),
    )
    _, run = await service.start_task(
        task.task_id,
        runner_id="qwenpaw.runner.tests",
    )
    await service.record_runner_signal(
        task.task_id,
        run.run_id,
        RunnerSignal(
            event_type="conversation.user",
            payload={"role": "user", "text": task.objective},
        ),
    )
    await service.record_runner_signal(
        task.task_id,
        run.run_id,
        RunnerSignal(
            event_type="conversation.assistant.completed",
            payload={"role": "assistant", "text": "Scheduled answer"},
        ),
    )
    events = await ledger.list_events(task.task_id, limit=200)
    assistant = next(
        event
        for event in events
        if event.event_type == "conversation.assistant.completed"
    )
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
    )

    child = await fork_chat(
        parent.id,
        ChatForkRequest(
            source_message_id=str(assistant.event_id),
            idempotency_key="fork-task-transcript",
        ),
        manager,
        session,
        workspace,
    )

    raw = await session.get_session_state_dict(
        child.session_id,
        child.user_id,
        child.channel,
    )
    child_state = AgentState.model_validate(raw["agent"]["state"])
    assert [message.role for message in child_state.context] == [
        "user",
        "assistant",
    ]
    assert [message.content[0].text for message in child_state.context] == [
        "Scheduled prompt",
        "Scheduled answer",
    ]
    assert child_state.context[-1].id == str(assistant.event_id)


@pytest.mark.asyncio
async def test_fork_registration_failure_removes_orphan_snapshot(
    tmp_path,
    monkeypatch,
) -> None:
    manager, session, parent = await _seed_parent(tmp_path)

    async def fail_save(_chats_file) -> None:
        raise OSError("registry write failed")

    monkeypatch.setattr(manager._repo, "save", fail_save)
    with pytest.raises(OSError, match="registry write failed"):
        await fork_chat(
            parent.id,
            ChatForkRequest(
                source_message_id="assistant-1",
                idempotency_key="fork-rollback",
            ),
            manager,
            session,
            SimpleNamespace(
                agent_id="default",
                task_tracker=SimpleNamespace(
                    get_status=AsyncMock(return_value="idle"),
                ),
            ),
        )

    session_files = list((tmp_path / "sessions").rglob("*.json"))
    assert len(session_files) == 1
    assert "parent" in session_files[0].name


@pytest.mark.asyncio
async def test_fork_uses_control_plane_and_isolates_child_state(
    tmp_path,
) -> None:
    manager, session, parent = await _seed_parent(tmp_path)
    control = InvocationControlService(
        database_path=tmp_path / "control.sqlite3",
    )
    interactions = InteractionService(tmp_path / "interactions.sqlite3")
    invocation_id = uuid4()
    lease = await control.begin_turn(
        TurnSubmissionRequest(
            agent_id="default",
            conversation_id=parent.id,
            content="parent turn",
            idempotency_key="parent-turn",
        ),
        invocation_id=invocation_id,
    )
    workspace = SimpleNamespace(
        agent_id="default",
        invocation_control=control,
        interaction_service=interactions,
        task_tracker=SimpleNamespace(
            get_status=AsyncMock(return_value="idle"),
        ),
    )
    payload = ChatForkRequest(
        source_message_id="assistant-1",
        idempotency_key="isolated-fork",
    )

    child = await fork_chat(
        parent.id,
        payload,
        manager,
        session,
        workspace,
    )
    parent_running_queue = await control.read_queue(
        agent_id="default",
        conversation_id=parent.id,
    )
    child_queue = await control.read_queue(
        agent_id="default",
        conversation_id=child.id,
    )
    assert (
        parent_running_queue.active_submission_id
        == lease.submission.submission_id
    )
    assert child_queue.active_submission_id is None
    assert child_queue.submissions == ()

    terminal = await control.finish_turn(
        lease,
        SubmissionStatus.SUCCEEDED,
    )
    assert terminal.status is SubmissionStatus.SUCCEEDED
    parent_interaction = InteractionRequest(
        kind=InteractionKind.APPROVAL,
        mode=InteractionMode.BLOCKING,
        agent_id="default",
        conversation_id=parent.id,
        invocation_id=invocation_id,
        title="Parent-only approval",
        prompt="Approve the parent branch only.",
    )
    await interactions.open(parent_interaction)
    parent_queue = await control.read_queue(
        agent_id="default",
        conversation_id=parent.id,
    )
    parent_open = await interactions.list_open(
        agent_id="default",
        conversation_id=parent.id,
    )
    child_open = await interactions.list_open(
        agent_id="default",
        conversation_id=child.id,
    )

    assert parent_queue.submissions == ()
    assert parent_queue.revision > child_queue.revision
    assert child_queue.submissions == ()
    assert parent_open == (parent_interaction,)
    assert child_open == ()

    nested = await fork_chat(
        child.id,
        payload.model_copy(
            update={"idempotency_key": "nested-isolated-fork"},
        ),
        manager,
        session,
        workspace,
    )
    assert await manager.get_fork_lineage_ids(nested.id) == (
        nested.id,
        child.id,
        parent.id,
    )


@pytest.mark.asyncio
async def test_fork_scopes_steer_and_interrupt_to_each_chat(tmp_path) -> None:
    manager, session, parent = await _seed_parent(tmp_path)
    control = InvocationControlService(
        database_path=tmp_path / "control.sqlite3",
    )
    child = await fork_chat(
        parent.id,
        ChatForkRequest(
            source_message_id="assistant-1",
            idempotency_key="fork-control-isolation",
        ),
        manager,
        session,
        SimpleNamespace(agent_id="default"),
    )
    parent_invocation_id = uuid4()
    child_invocation_id = uuid4()
    parent_lease = await control.begin_turn(
        TurnSubmissionRequest(
            agent_id="default",
            conversation_id=parent.id,
            content="parent runtime",
            idempotency_key="parent-runtime",
        ),
        invocation_id=parent_invocation_id,
    )
    child_lease = await control.begin_turn(
        TurnSubmissionRequest(
            agent_id="default",
            conversation_id=child.id,
            content="child runtime",
            idempotency_key="child-runtime",
        ),
        invocation_id=child_invocation_id,
    )

    steer_receipt = await control.steer_current(
        agent_id="default",
        conversation_id=parent.id,
        instruction="Parent branch only",
        idempotency_key="parent-steer",
    )
    parent_injected: list[str] = []
    child_injected: list[str] = []

    async def inject_parent(delivery, _safe_point) -> None:
        parent_injected.append(delivery.instruction)

    async def inject_child(delivery, _safe_point) -> None:
        child_injected.append(delivery.instruction)

    child_count = await child_lease.steering.apply_pending(
        SteerSafePoint.BEFORE_REASONING,
        inject_child,
    )
    parent_count = await parent_lease.steering.apply_pending(
        SteerSafePoint.BEFORE_REASONING,
        inject_parent,
    )
    assert steer_receipt.status is ControlCommandStatus.ACCEPTED
    assert child_count == 0
    assert not child_injected
    assert parent_count == 1
    assert parent_injected == ["Parent branch only"]

    parent_release = asyncio.Event()
    parent_binding_holder = {}
    child_binding_holder = {}

    async def parent_runtime() -> None:
        await parent_release.wait()
        await parent_binding_holder["binding"].close()
        await control.finish_turn(
            parent_lease,
            SubmissionStatus.SUCCEEDED,
        )

    async def child_runtime() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await child_binding_holder["binding"].close()
            await control.finish_turn(
                child_lease,
                SubmissionStatus.INTERRUPTED,
            )
            raise

    parent_task = asyncio.create_task(parent_runtime())
    child_task = asyncio.create_task(child_runtime())
    parent_binding_holder["binding"] = await control.bind_interrupt(
        parent_invocation_id,
        parent_task,
        lease=parent_lease,
        agent_id="default",
        conversation_id=parent.id,
    )
    child_binding_holder["binding"] = await control.bind_interrupt(
        child_invocation_id,
        child_task,
        lease=child_lease,
        agent_id="default",
        conversation_id=child.id,
    )

    interrupt_receipt = await control.interrupt_current(
        agent_id="default",
        conversation_id=child.id,
        idempotency_key="child-interrupt",
    )
    assert interrupt_receipt is not None
    assert interrupt_receipt.status is ControlCommandStatus.APPLIED
    with pytest.raises(asyncio.CancelledError):
        await child_task
    assert child_lease.submission.status is SubmissionStatus.INTERRUPTED
    assert parent_lease.submission.status is SubmissionStatus.RUNNING
    assert not parent_task.done()

    parent_release.set()
    await parent_task
    assert parent_lease.submission.status is SubmissionStatus.SUCCEEDED
    await control.close()


@pytest.mark.asyncio
async def test_fork_scopes_strict_approvals_to_each_chat(tmp_path) -> None:
    manager, session, parent = await _seed_parent(tmp_path)
    child = await fork_chat(
        parent.id,
        ChatForkRequest(
            source_message_id="assistant-1",
            idempotency_key="fork-approval-isolation",
        ),
        manager,
        session,
        SimpleNamespace(agent_id="default"),
    )
    interactions = InteractionService(tmp_path / "interactions.sqlite3")
    options = (
        InteractionOption(option_id="approve", label="Approve"),
        InteractionOption(option_id="deny", label="Deny"),
    )
    parent_approval = InteractionRequest(
        kind=InteractionKind.APPROVAL,
        mode=InteractionMode.BLOCKING,
        agent_id="default",
        conversation_id=parent.id,
        invocation_id=uuid4(),
        title="Parent strict approval",
        prompt="Allow the parent branch tool call?",
        options=options,
        metadata={"approval_level": "strict", "risk": "high"},
    )
    child_approval = InteractionRequest(
        kind=InteractionKind.APPROVAL,
        mode=InteractionMode.BLOCKING,
        agent_id="default",
        conversation_id=child.id,
        invocation_id=uuid4(),
        title="Child strict approval",
        prompt="Allow the child branch tool call?",
        options=options,
        metadata={"approval_level": "strict", "risk": "high"},
    )
    await asyncio.gather(
        interactions.open(parent_approval),
        interactions.open(child_approval),
    )

    assert await interactions.list_open(
        agent_id="default",
        conversation_id=parent.id,
    ) == (parent_approval,)
    assert await interactions.list_open(
        agent_id="default",
        conversation_id=child.id,
    ) == (child_approval,)

    child_resolution = await interactions.resolve(
        InteractionResponse(
            interaction_id=child_approval.interaction_id,
            idempotency_key="approve-child",
            expected_revision=child_approval.revision,
            actor=ActorRef(type=ActorType.USER, id="local-user"),
            selected_option_ids=("approve",),
        ),
    )
    assert child_resolution.response is not None
    assert child_resolution.response.selected_option_ids == ("approve",)
    assert (
        await interactions.list_open(
            agent_id="default",
            conversation_id=child.id,
        )
        == ()
    )
    assert await interactions.list_open(
        agent_id="default",
        conversation_id=parent.id,
    ) == (parent_approval,)

    await interactions.resolve(
        InteractionResponse(
            interaction_id=parent_approval.interaction_id,
            idempotency_key="deny-parent",
            expected_revision=parent_approval.revision,
            actor=ActorRef(type=ActorType.USER, id="local-user"),
            selected_option_ids=("deny",),
        ),
    )
    await interactions.close()


@pytest.mark.asyncio
async def test_fork_inherits_artifact_without_sharing_child_writes(
    tmp_path,
) -> None:
    manager, session, parent = await _seed_parent(tmp_path)
    artifact_store = lite_artifact_store(tmp_path)
    receipt_store = conversation_artifact_receipts(tmp_path)

    parent_artifact = await artifact_store.put(
        kind="chat.attachment",
        media_type="text/plain",
        content=b"parent version",
        metadata={"name": "shared.txt"},
    )
    parent_evidence = EvidenceRef(
        artifact_id=parent_artifact.artifact_id,
        claim="Parent attachment",
        producer="test.fork",
    )
    parent_receipt = await receipt_store.create(
        parent_artifact,
        parent_evidence,
    )
    await receipt_store.claim(
        receipt_id=parent_receipt,
        chat_id=parent.id,
        artifact_id=parent_artifact.artifact_id,
        evidence_id=parent_evidence.evidence_id,
    )
    parent_state = AgentState(
        session_id=parent.session_id,
        context=[
            _attachment_message(
                "user-with-file",
                artifact=parent_artifact,
                evidence=parent_evidence,
                receipt_id=parent_receipt,
            ),
            _message("assistant-with-file", "assistant", "Read it"),
        ],
    )
    await session.save_session_state(
        parent.session_id,
        parent.user_id,
        parent.channel,
        agent=_StateModule(parent_state),
    )
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        config=SimpleNamespace(backend="qwenpaw"),
        capability_registry=GenerationRegistry(),
        task_tracker=SimpleNamespace(
            get_status=AsyncMock(return_value="idle"),
        ),
    )
    child = await fork_chat(
        parent.id,
        ChatForkRequest(
            source_message_id="assistant-with-file",
            idempotency_key="fork-with-artifact",
        ),
        manager,
        session,
        workspace,
    )

    late_parent_artifact = await artifact_store.put(
        kind="chat.attachment",
        media_type="text/plain",
        content=b"late parent version",
        metadata={"name": "late-parent.txt"},
    )
    late_parent_evidence = EvidenceRef(
        artifact_id=late_parent_artifact.artifact_id,
        claim="Parent attachment created after fork",
        producer="test.fork",
    )
    late_parent_receipt = await receipt_store.create(
        late_parent_artifact,
        late_parent_evidence,
    )
    await receipt_store.claim(
        receipt_id=late_parent_receipt,
        chat_id=parent.id,
        artifact_id=late_parent_artifact.artifact_id,
        evidence_id=late_parent_evidence.evidence_id,
    )
    parent_state.context.append(
        _attachment_message(
            "late-parent-file",
            artifact=late_parent_artifact,
            evidence=late_parent_evidence,
            receipt_id=late_parent_receipt,
        ),
    )
    await session.save_session_state(
        parent.session_id,
        parent.user_id,
        parent.channel,
        agent=_StateModule(parent_state),
    )

    inherited = await get_chat_artifact_content(
        child.id,
        parent_artifact.artifact_id,
        "inline",
        manager,
        session,
        workspace,
    )
    assert inherited.body == b"parent version"
    inherited_records = await list_chat_artifacts(
        child.id,
        100,
        manager,
        session,
        workspace,
    )
    assert [record.artifact.artifact_id for record in inherited_records] == [
        parent_artifact.artifact_id,
    ]
    with pytest.raises(HTTPException) as late_parent_read:
        await get_chat_artifact_content(
            child.id,
            late_parent_artifact.artifact_id,
            "inline",
            manager,
            session,
            workspace,
        )
    assert late_parent_read.value.status_code == 404

    child_artifact = await artifact_store.put(
        kind="chat.attachment",
        media_type="text/plain",
        content=b"child version",
        metadata={"name": "shared.txt"},
    )
    child_evidence = EvidenceRef(
        artifact_id=child_artifact.artifact_id,
        claim="Child attachment",
        producer="test.fork",
    )
    child_receipt = await receipt_store.create(
        child_artifact,
        child_evidence,
    )
    await receipt_store.claim(
        receipt_id=child_receipt,
        chat_id=child.id,
        artifact_id=child_artifact.artifact_id,
        evidence_id=child_evidence.evidence_id,
    )
    child_raw = await session.get_session_state_dict(
        child.session_id,
        child.user_id,
        child.channel,
    )
    child_state = AgentState.model_validate(child_raw["agent"]["state"])
    child_state.context.append(
        _attachment_message(
            "child-user-with-file",
            artifact=child_artifact,
            evidence=child_evidence,
            receipt_id=child_receipt,
        ),
    )
    await session.save_session_state(
        child.session_id,
        child.user_id,
        child.channel,
        agent=_StateModule(child_state),
    )

    child_owned = await get_chat_artifact_content(
        child.id,
        child_artifact.artifact_id,
        "inline",
        manager,
        session,
        workspace,
    )
    assert child_owned.body == b"child version"
    assert child_artifact.artifact_id != parent_artifact.artifact_id
    assert child_artifact.content_hash != parent_artifact.content_hash
    child_records = await list_chat_artifacts(
        child.id,
        100,
        manager,
        session,
        workspace,
    )
    assert {record.artifact.artifact_id for record in child_records} == {
        parent_artifact.artifact_id,
        child_artifact.artifact_id,
    }

    with pytest.raises(HTTPException) as reverse_read:
        await get_chat_artifact_content(
            parent.id,
            child_artifact.artifact_id,
            "inline",
            manager,
            session,
            workspace,
        )
    assert reverse_read.value.status_code == 404
