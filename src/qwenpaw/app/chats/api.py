# -*- coding: utf-8 -*-
"""Chat management API."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from collections.abc import AsyncIterator, Awaitable
from typing import Annotated, Any, Literal, Optional
from urllib.parse import quote
from uuid import UUID, uuid4
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import Response, StreamingResponse

from agentscope.message import Msg
from agentscope.state import AgentState

from .manager import (
    MAX_BATCH_SIZE,
    ChatManager,
)
from .models import (
    BatchArchiveResult,
    ChatGroup,
    ChatGroupCreate,
    ChatGroupOrderUpdate,
    ChatGroupUpdate,
    ChatForkRequest,
    ChatControlRequest,
    ChatInteractionDecisionRequest,
    ChatQueueReorderRequest,
    ChatSpec,
    ChatSteerRequest,
    ChatSubmissionRequest,
    ChatUpdate,
    ChatHistory,
)
from .session import (
    SafeJSONSession,
)
from .utils import agentscope_msg_to_message, parse_legacy_memory_state
from ...services.project_directory import (
    agent_project_dirs_from_config,
    resolve_effective_project_dirs,
    session_project_dirs_raw_from_meta,
)
from ...checkpoints.runtime import RUNTIME as CHECKPOINT_RUNTIME
from ...interactions import (
    InteractionConflictError,
    InteractionNotFoundError,
)
from ...invocation_control import (
    ConversationRuntimeProjectionService,
    ControlIdempotencyConflictError,
    QueueCommandConflictError,
    QueueRevisionConflictError,
    QueueTargetNotFoundError,
)
from ...kernel import (
    ActionRecord,
    ActorRef,
    ActorType,
    ArtifactRef,
    InteractionRequest,
    InteractionResolution,
    InteractionResponse,
    ModelCallRecord,
    ObservationPage,
    RuntimeObservation,
    ConversationRuntimeProjection,
    ControlReceipt,
    QueueProjection,
    SubmissionInputEnvelope,
    TurnSubmissionRequest,
    WaitCondition,
    ConversationForkCommand,
    ConversationForkConflictError,
    ConversationForkInvalidAnchorError,
    ConversationForkNotFoundError,
)
from ...conversations import LiteConversationForkAdapter
from ...config.config import load_agent_config_async
from ...kernel.models import ArtifactRenderDisposition
from ...runtime.assembly import capability_registry_for
from ...runtime.actions import lite_action_store, public_action_record
from ...runtime.model_calls import lite_model_call_store
from ...runtime.observation_index import ObservationCursorError
from ...runtime.observations import lite_observation_projection
from ...tasks.artifacts import (
    ArtifactIntegrityError,
    artifact_filename,
    lite_artifact_store,
)
from ...tasks.verification_history import lite_verification_history
from ...tasks.conversation_artifacts import (
    ConversationArtifactReceiptError,
    conversation_artifact_receipts,
)
from ...tasks.renderers import (
    ArtifactPreviewTooLargeError,
    ArtifactRendererUnavailableError,
    ArtifactRenderService,
)
from ...tasks.system_contributions import (
    SYSTEM_CAPABILITY_BUNDLE,
    system_contribution_factory,
)
from .input_artifacts import (
    ConversationInputArtifactError,
    claim_conversation_artifacts,
)
from .submission_dispatcher import CONSOLE_SUBMISSION_ENVELOPE
from .compatibility import (
    ExternalQueueFallbackRequest,
    SQLiteExternalQueueCompatibilityStore,
)

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/chats", tags=["chats"])
_MAX_ARTIFACT_PREVIEW_BYTES = 256 * 1024


def _external_queue_compatibility_store(
    workspace: Any,
) -> SQLiteExternalQueueCompatibilityStore:
    store = getattr(workspace, "_external_queue_compatibility_store", None)
    if isinstance(store, SQLiteExternalQueueCompatibilityStore):
        return store
    store = SQLiteExternalQueueCompatibilityStore(
        Path(workspace.workspace_dir)
        / ".qwenpaw"
        / "lite"
        / "chat-compatibility.db",
    )
    setattr(workspace, "_external_queue_compatibility_store", store)
    return store


def _is_app_owned_chat(chat: ChatSpec) -> bool:
    """Return whether a chat belongs to a PawApp-owned dialogue surface."""
    owner = chat.meta.get("pawapp") if isinstance(chat.meta, dict) else None
    return isinstance(owner, dict) and bool(owner.get("app_id"))


async def get_workspace(request: Request):
    """Get the workspace for the active agent."""
    from ..agent_context import get_agent_for_request

    return await get_agent_for_request(request)


@router.post("/compatibility/external-queue/hits", status_code=202)
async def record_external_queue_fallback(
    body: ExternalQueueFallbackRequest,
    workspace=Depends(get_workspace),
) -> dict[str, bool]:
    """Observe external queue use without accepting message content."""
    profile = await load_agent_config_async(workspace.agent_id)
    if profile.backend == "qwenpaw" or profile.backend != body.backend_id:
        raise HTTPException(
            status_code=409,
            detail="External queue observation does not match agent backend",
        )
    recorded = await _external_queue_compatibility_store(workspace).record(
        agent_id=workspace.agent_id,
        request=body,
    )
    return {"recorded": recorded}


@router.get("/compatibility/external-queue")
async def external_queue_compatibility(
    workspace=Depends(get_workspace),
) -> dict[str, Any]:
    """Return structured external queue migration diagnostics."""
    report = await _external_queue_compatibility_store(workspace).report(
        agent_id=workspace.agent_id,
    )
    return report.model_dump(mode="json")


async def get_chat_manager(
    request: Request,
) -> ChatManager:
    """Get the chat manager for the active agent.

    Args:
        request: FastAPI request object

    Returns:
        ChatManager instance for the specified agent

    Raises:
        HTTPException: If manager is not initialized
    """
    workspace = await get_workspace(request)
    return workspace.chat_manager


async def get_session(
    request: Request,
) -> SafeJSONSession:
    """Get the session for the active agent.

    Args:
        request: FastAPI request object

    Returns:
        SafeJSONSession instance for the specified agent

    Raises:
        HTTPException: If session is not initialized
    """
    workspace = await get_workspace(request)
    return workspace.session


class ProjectDirectoryUpdate(BaseModel):
    """Controlled Session project directory update."""

    project_dir: str


class ChatStatusResponse(BaseModel):
    """Lightweight TaskTracker status for one chat."""

    status: Literal["idle", "running"]


class ProjectDirEntryPayload(BaseModel):
    """One project-directory entry as sent by the client."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        ...,
        min_length=1,
        description="Absolute path to a project directory",
    )
    label: Optional[str] = Field(
        default=None,
        max_length=50,
        description="Optional note describing what this directory is for",
    )


class ProjectDirsRequest(BaseModel):
    """Payload for setting a chat's project-directory list override.

    The list is ordered: the first entry becomes the PRIMARY project
    directory. The payload is the whole desired list — add, remove and
    make-primary are all expressed as list transforms followed by one
    PUT.
    """

    model_config = ConfigDict(extra="forbid")

    project_dirs: list[ProjectDirEntryPayload] = Field(
        ...,
        min_length=1,
        max_length=10,
        description="Full ordered list, primary first",
    )


class ProjectDirEntryView(BaseModel):
    """One effective project-directory entry for the UI."""

    path: str = Field(description="Directory path")
    label: Optional[str] = Field(
        default=None,
        description="Display name for this directory, when one was set",
    )
    exists: bool = Field(
        description=(
            "Whether the path exists. False is surfaced rather than "
            "silently corrected so the UI can flag it as unavailable."
        ),
    )
    nested_with: Optional[str] = Field(
        default=None,
        description=(
            "Path of the nearest ancestor root when this entry is "
            "nested inside another bound root (informational; the "
            "entry stays fully usable)."
        ),
    )
    is_workspace: bool = Field(
        default=False,
        description=(
            "Whether this entry is the agent's own workspace directory. "
            "Decided here by filesystem identity, because the client "
            "cannot: comparing the two paths as text splits one directory "
            "into two roots on a case-sensitive volume and merges two "
            "distinct ones on a folding volume. The Files switcher "
            "collapses such an entry onto its own 'workspace' root rather "
            "than giving it a second one with its own editor tabs."
        ),
    )


class ProjectDirsResponse(BaseModel):
    """Effective project-directory list for a chat, plus provenance."""

    project_dirs: list[ProjectDirEntryView] = Field(
        description=(
            "Effective list, primary first. Empty when nothing is "
            "configured (tools then fall back to the agent workspace; "
            "the workspace path itself is deliberately not listed)."
        ),
    )
    source: str = Field(
        description=(
            "Provenance of the list: 'session' (this chat overrides), "
            "'agent' (agent default), or 'workspace_fallback' (nothing "
            "configured)"
        ),
    )
    agent_project_dir: Optional[str] = Field(
        default=None,
        description=(
            "The agent-level default directory (single value), for "
            "showing inheritance"
        ),
    )


async def _project_directory_response(chat: ChatSpec, workspace) -> dict:
    """Build the effective Session project directory response."""
    from ...config.config import load_agent_config

    def _build() -> dict:
        try:
            config = load_agent_config(workspace.agent_id)
            agent_dir = config.project_dir
            agent_dirs = agent_project_dirs_from_config(config)
        except Exception:
            agent_dir, agent_dirs = None, []
        resolved = resolve_effective_project_dirs(
            workspace.workspace_dir,
            agent_project_dir=agent_dir,
            agent_project_dirs=agent_dirs,
            session_project_dirs=session_project_dirs_raw_from_meta(chat.meta),
        )
        project_dir = resolved.primary_path
        return {
            "project_dir": str(project_dir),
            "source": resolved.source,
            "agent_project_dir": agent_dir,
            "exists": project_dir.is_dir(),
        }

    return await asyncio.to_thread(_build)


async def _project_dirs_response(chat: ChatSpec, workspace) -> dict:
    """Build the effective Session project-directory list response."""
    from ...config.config import load_agent_config
    from ...services.project_directory import (
        nested_root_pairs,
    )

    def _build() -> dict:
        try:
            agent_config = load_agent_config(workspace.agent_id)
            agent_dir = agent_config.project_dir
            agent_dirs = agent_project_dirs_from_config(agent_config)
        except Exception:
            agent_dir, agent_dirs = None, []

        resolved = resolve_effective_project_dirs(
            workspace.workspace_dir,
            agent_project_dir=agent_dir,
            agent_project_dirs=agent_dirs,
            session_project_dirs=session_project_dirs_raw_from_meta(chat.meta),
        )
        # Nearest covering ancestor per entry, for the UI hint. Fed the
        # already-resolved paths so the nesting check does not resolve()
        # every entry a second time.
        nearest: dict[int, str] = {}
        for child_idx, anc_idx in nested_root_pairs(
            [entry.path for entry in resolved.dirs],
        ):
            candidate = str(resolved.dirs[anc_idx].path)
            current = nearest.get(child_idx)
            if current is None or len(candidate) > len(current):
                nearest[child_idx] = candidate

        return {
            "project_dirs": [
                {
                    "path": str(entry.path),
                    "label": entry.label,
                    "exists": entry.exists,
                    "nested_with": nearest.get(index),
                    # Compared by key, not by path text: these are the same
                    # directory exactly when they reach the same entry.
                    "is_workspace": bool(entry.key)
                    and entry.key == resolved.workspace_key,
                }
                for index, entry in enumerate(resolved.dirs)
            ],
            "source": resolved.source,
            "agent_project_dir": agent_dir,
        }

    return await asyncio.to_thread(_build)


@router.get("", response_model=list[ChatSpec])
async def list_chats(
    user_id: Optional[str] = Query(None, description="Filter by user ID"),
    channel: Optional[str] = Query(None, description="Filter by channel"),
    archived: Optional[bool] = Query(
        None,
        description=(
            "Filter by archived status. "
            "false=active only, true=archived only, "
            "null/omit=all (default)"
        ),
    ),
    include_app_owned: bool = Query(
        True,
        description=(
            "Include PawApp-owned chats. Administrative and legacy callers "
            "keep the full catalog by default; the main Chat surface opts out."
        ),
    ),
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
):
    """List all chats with optional filters.

    When ``archived`` is omitted, returns all chats (both active and archived).
    Pass ``archived=false`` for active only,
    ``archived=true`` for archived only.
    """
    chats = await mgr.list_chats(
        user_id=user_id,
        channel=channel,
        archived=archived,
    )
    if not include_app_owned:
        chats = [chat for chat in chats if not _is_app_owned_chat(chat)]
    tracker = workspace.task_tracker
    result = []
    for spec in chats:
        status = await tracker.get_status(spec.id)
        result.append(spec.model_copy(update={"status": status}))
    return result


@router.post("", response_model=ChatSpec)
async def create_chat(
    request: ChatSpec,
    mgr: ChatManager = Depends(get_chat_manager),
):
    """Create a new chat.

    Server generates chat_id (UUID) automatically.

    Args:
        request: Chat creation request
        mgr: Chat manager dependency

    Returns:
        Created chat spec with UUID
    """
    chat_id = str(uuid4())
    spec = ChatSpec(
        id=chat_id,
        name=request.name,
        session_id=request.session_id,
        user_id=request.user_id,
        channel=request.channel,
        meta=request.meta,
        source=request.source,
        group_id=request.group_id,
        parent_session_id=request.parent_session_id,
        root_session_id=request.root_session_id,
    )
    try:
        return await mgr.create_chat(spec)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{parent_chat_id}/fork", response_model=ChatSpec)
async def fork_chat(
    parent_chat_id: str,
    payload: ChatForkRequest,
    mgr: ChatManager = Depends(get_chat_manager),
    session: SafeJSONSession = Depends(get_session),
    workspace=Depends(get_workspace),
):
    """Fork persisted history through one stable source message."""
    adapter = LiteConversationForkAdapter(
        agent_id=workspace.agent_id,
        manager=mgr,
        session=session,
    )
    try:
        result = await adapter.fork(
            ConversationForkCommand(
                agent_id=workspace.agent_id,
                parent_conversation_id=parent_chat_id,
                source_message_id=payload.source_message_id,
                idempotency_key=payload.idempotency_key,
                name=payload.name,
            ),
        )
        child = await mgr.get_chat(result.child_conversation_id)
        if child is None:
            raise ConversationForkNotFoundError(
                result.child_conversation_id,
            )
        return child
    except ConversationForkNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        ConversationForkConflictError,
        ConversationForkInvalidAnchorError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def _chat_interaction_context(
    chat_id: str,
    mgr: ChatManager,
    workspace,
):
    """Resolve a Chat and its workspace-owned interaction service."""
    chat = await mgr.get_chat(chat_id)
    if chat is None:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )
    service = getattr(workspace, "interaction_service", None)
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="Interaction service is unavailable",
        )
    return chat, service


async def _chat_control_context(
    chat_id: str,
    mgr: ChatManager,
    workspace,
):
    """Resolve a Chat and its workspace-owned invocation control service."""
    chat = await mgr.get_chat(chat_id)
    if chat is None:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )
    service = getattr(workspace, "invocation_control", None)
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="Invocation control service is unavailable",
        )
    return service


async def _chat_runtime_projection_context(
    chat_id: str,
    mgr: ChatManager,
    workspace,
) -> ConversationRuntimeProjectionService:
    """Compose the current-state projection from workspace-owned services."""
    control = await _chat_control_context(chat_id, mgr, workspace)
    interactions = getattr(workspace, "interaction_service", None)
    if interactions is None:
        raise HTTPException(
            status_code=503,
            detail="Interaction service is unavailable",
        )
    return ConversationRuntimeProjectionService(control, interactions)


async def _apply_chat_control(
    operation: Awaitable[ControlReceipt | None],
) -> ControlReceipt:
    """Map domain control outcomes to the stable Chat HTTP contract."""
    try:
        receipt = await operation
    except QueueTargetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        ControlIdempotencyConflictError,
        QueueCommandConflictError,
        QueueRevisionConflictError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if receipt is None:
        raise HTTPException(
            status_code=409,
            detail="Chat has no active invocation",
        )
    return receipt


def _chat_submission_content(parts: tuple[dict[str, Any], ...]) -> str:
    """Return the first meaningful text without interpreting tool payloads."""
    for part in parts:
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            return text
    return "[non-text user input]"


@router.get(
    "/{chat_id}/interactions",
    response_model=list[InteractionRequest],
)
async def list_chat_interactions(
    chat_id: str,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
) -> list[InteractionRequest]:
    """List server-authoritative open interactions for one ChatSpec."""
    _, service = await _chat_interaction_context(chat_id, mgr, workspace)
    interactions = await service.list_open(
        agent_id=workspace.agent_id,
        conversation_id=chat_id,
    )
    return list(interactions)


@router.get(
    "/{chat_id}/wait-conditions",
    response_model=list[WaitCondition],
)
async def list_chat_wait_conditions(
    chat_id: str,
    include_terminal: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=1000),
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
) -> list[WaitCondition]:
    """List content-free blockers projected from authoritative facts."""
    _, service = await _chat_interaction_context(chat_id, mgr, workspace)
    conditions = await service.list_wait_conditions(
        agent_id=workspace.agent_id,
        conversation_id=chat_id,
        include_terminal=include_terminal,
        limit=limit,
    )
    return list(conditions)


@router.post(
    "/{chat_id}/interactions/{interaction_id}/response",
    response_model=InteractionResolution,
)
async def respond_chat_interaction(
    chat_id: str,
    interaction_id: UUID,
    body: ChatInteractionDecisionRequest,
    request: Request,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
) -> InteractionResolution:
    """Resolve one Chat-owned interaction using optimistic concurrency."""
    chat, service = await _chat_interaction_context(
        chat_id,
        mgr,
        workspace,
    )
    interaction = await service.get_request(interaction_id)
    if (
        interaction is None
        or interaction.agent_id != workspace.agent_id
        or interaction.conversation_id != chat_id
    ):
        raise HTTPException(
            status_code=404,
            detail="Interaction does not belong to this chat",
        )
    user = getattr(request.state, "user", None)
    actor_id = (
        str(user.get("username") or "") if isinstance(user, dict) else ""
    )
    if not actor_id:
        actor_id = chat.user_id or "local-user"
    try:
        return await service.resolve(
            InteractionResponse(
                interaction_id=interaction_id,
                idempotency_key=body.idempotency_key,
                expected_revision=body.expected_revision,
                actor=ActorRef(type=ActorType.USER, id=actor_id),
                selected_option_ids=body.selected_option_ids,
                text=body.text,
                values=body.values,
            ),
        )
    except InteractionNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail="Interaction does not belong to this chat",
        ) from exc
    except InteractionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/{chat_id}/submissions",
    response_model=ControlReceipt,
)
async def submit_chat_turn(
    chat_id: str,
    body: ChatSubmissionRequest,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
) -> ControlReceipt:
    """Persist a complete Console turn for independent server dispatch."""
    chat = await mgr.get_chat(chat_id)
    if chat is None:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )
    if chat.channel != "console":
        raise HTTPException(
            status_code=409,
            detail="Only Console chats support durable HTTP submission",
        )
    dispatcher = getattr(workspace, "submission_dispatcher", None)
    if dispatcher is None:
        raise HTTPException(
            status_code=503,
            detail="Chat submission dispatcher is unavailable",
        )
    content_parts = [dict(part) for part in body.content_parts]
    try:
        await claim_conversation_artifacts(
            workspace,
            chat_id,
            content_parts,
        )
    except ConversationInputArtifactError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    artifact_refs = tuple(
        ArtifactRef.model_validate(part["artifact_ref"])
        for part in content_parts
        if part.get("artifact_ref") is not None
    )
    message_metadata = dict(body.message_metadata)
    message_metadata.setdefault(
        "qwenpaw_client_message_id",
        body.idempotency_key,
    )
    native_payload: dict[str, Any] = dict(body.request_extensions)
    native_payload.update(
        {
            "channel_id": chat.channel,
            "sender_id": chat.user_id,
            "content_parts": content_parts,
            "message_metadata": message_metadata,
            "message_id": body.idempotency_key,
            "meta": {
                "session_id": chat.session_id,
                "user_id": chat.user_id,
                "request_context": dict(body.request_context),
            },
        },
    )
    if body.model_slot_override is not None:
        native_payload["model_slot_override"] = body.model_slot_override
    request = TurnSubmissionRequest(
        agent_id=workspace.agent_id,
        conversation_id=chat_id,
        priority=body.priority,
        content=_chat_submission_content(body.content_parts),
        artifact_refs=artifact_refs,
        request_context={"channel": chat.channel},
        input_envelope=SubmissionInputEnvelope(
            kind=CONSOLE_SUBMISSION_ENVELOPE,
            payload=native_payload,
        ),
        idempotency_key=body.idempotency_key,
    )
    return await _apply_chat_control(
        dispatcher.enqueue(
            request,
            expected_revision=body.expected_revision,
        ),
    )


@router.get(
    "/{chat_id}/queue",
    response_model=QueueProjection,
)
async def get_chat_queue(
    chat_id: str,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
) -> QueueProjection:
    """Read the server-authoritative queue for one ChatSpec identity."""
    service = await _chat_control_context(chat_id, mgr, workspace)
    return await service.read_queue(
        agent_id=workspace.agent_id,
        conversation_id=chat_id,
    )


@router.post(
    "/{chat_id}/control/steer",
    response_model=ControlReceipt,
)
async def steer_chat(
    chat_id: str,
    body: ChatSteerRequest,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
) -> ControlReceipt:
    """Persist a steer for delivery at the active invocation's safe point."""
    service = await _chat_control_context(chat_id, mgr, workspace)
    return await _apply_chat_control(
        service.steer_current(
            agent_id=workspace.agent_id,
            conversation_id=chat_id,
            instruction=body.instruction,
            idempotency_key=body.idempotency_key,
            expected_revision=body.expected_revision,
        ),
    )


@router.post(
    "/{chat_id}/control/interrupt",
    response_model=ControlReceipt,
)
async def interrupt_chat(
    chat_id: str,
    body: ChatControlRequest,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
) -> ControlReceipt:
    """Interrupt only the invocation currently active for this ChatSpec."""
    service = await _chat_control_context(chat_id, mgr, workspace)
    return await _apply_chat_control(
        service.interrupt_current(
            agent_id=workspace.agent_id,
            conversation_id=chat_id,
            idempotency_key=body.idempotency_key,
            expected_revision=body.expected_revision,
        ),
    )


@router.post(
    "/{chat_id}/control/stop-and-clear",
    response_model=ControlReceipt,
)
async def stop_and_clear_chat(
    chat_id: str,
    body: ChatControlRequest,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
) -> ControlReceipt:
    """Atomically clear queued turns and interrupt the captured invocation."""
    service = await _chat_control_context(chat_id, mgr, workspace)
    return await _apply_chat_control(
        service.stop_and_clear(
            agent_id=workspace.agent_id,
            conversation_id=chat_id,
            idempotency_key=body.idempotency_key,
            expected_revision=body.expected_revision,
        ),
    )


@router.post(
    "/{chat_id}/queue/{submission_id}/cancel",
    response_model=ControlReceipt,
)
async def cancel_chat_queue_item(
    chat_id: str,
    submission_id: UUID,
    body: ChatControlRequest,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
) -> ControlReceipt:
    """Cancel one queued submission without touching the active run."""
    service = await _chat_control_context(chat_id, mgr, workspace)
    return await _apply_chat_control(
        service.cancel_queued(
            agent_id=workspace.agent_id,
            conversation_id=chat_id,
            submission_id=submission_id,
            idempotency_key=body.idempotency_key,
            expected_revision=body.expected_revision,
        ),
    )


@router.post(
    "/{chat_id}/queue/reorder",
    response_model=ControlReceipt,
)
async def reorder_chat_queue(
    chat_id: str,
    body: ChatQueueReorderRequest,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
) -> ControlReceipt:
    """Replace the complete order of queued submissions atomically."""
    service = await _chat_control_context(chat_id, mgr, workspace)
    return await _apply_chat_control(
        service.reorder_queue(
            agent_id=workspace.agent_id,
            conversation_id=chat_id,
            ordered_submission_ids=body.ordered_submission_ids,
            idempotency_key=body.idempotency_key,
            expected_revision=body.expected_revision,
        ),
    )


@router.get(
    "/{chat_id}/runtime",
    response_model=ConversationRuntimeProjection,
)
async def get_chat_runtime_projection(
    chat_id: str,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
) -> ConversationRuntimeProjection:
    """Read one recoverable Queue and Interaction snapshot."""
    service = await _chat_runtime_projection_context(
        chat_id,
        mgr,
        workspace,
    )
    return await service.read(
        agent_id=workspace.agent_id,
        conversation_id=chat_id,
    )


@router.get("/{chat_id}/runtime/stream")
async def stream_chat_runtime_projection(
    chat_id: str,
    request: Request,
    last_event_id: Annotated[
        str | None,
        Header(alias="Last-Event-ID"),
    ] = None,
    after_cursor: Annotated[
        str | None,
        Query(min_length=1, max_length=200),
    ] = None,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
) -> StreamingResponse:
    """Follow latest authoritative snapshots with reconnect recovery."""
    if (
        after_cursor is not None
        and last_event_id is not None
        and after_cursor != last_event_id
    ):
        raise HTTPException(
            status_code=400,
            detail="after_cursor conflicts with Last-Event-ID",
        )
    service = await _chat_runtime_projection_context(
        chat_id,
        mgr,
        workspace,
    )
    starting_cursor = after_cursor or last_event_id or ""

    async def generate() -> AsyncIterator[str]:
        cursor = starting_cursor
        while not await request.is_disconnected():
            projection = await service.wait_for_change(
                agent_id=workspace.agent_id,
                conversation_id=chat_id,
                after_cursor=cursor,
                timeout=15.0,
            )
            if projection is None:
                yield ": keepalive\n\n"
                continue
            cursor = projection.cursor
            payload = json.dumps(
                projection.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            yield (
                f"id: {cursor}\n" f"event: snapshot\n" f"data: {payload}\n\n"
            )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ----- Chat group endpoints -----


@router.get("/groups", response_model=list[ChatGroup])
async def list_chat_groups(
    mgr: ChatManager = Depends(get_chat_manager),
):
    """List built-in and custom groups in display order."""
    return await mgr.list_groups()


@router.post("/groups", response_model=ChatGroup)
async def create_chat_group(
    payload: ChatGroupCreate,
    mgr: ChatManager = Depends(get_chat_manager),
):
    """Create a custom chat group."""
    try:
        return await mgr.create_group(payload.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/groups/order", response_model=list[ChatGroup])
async def reorder_chat_groups(
    payload: ChatGroupOrderUpdate,
    mgr: ChatManager = Depends(get_chat_manager),
):
    """Replace the complete chat-group display order."""
    try:
        return await mgr.reorder_groups(payload.group_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/groups/{group_id}", response_model=ChatGroup)
async def update_chat_group(
    group_id: str,
    payload: ChatGroupUpdate,
    mgr: ChatManager = Depends(get_chat_manager),
):
    """Rename or pin a mutable chat group."""
    try:
        group = await mgr.update_group(
            group_id,
            name=payload.name,
            pinned=payload.pinned,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if group is None:
        raise HTTPException(status_code=404, detail="Chat group not found")
    return group


@router.delete("/groups/{group_id}", response_model=dict)
async def delete_chat_group(
    group_id: str,
    mgr: ChatManager = Depends(get_chat_manager),
):
    """Delete a custom group and re-home its chats."""
    try:
        deleted = await mgr.delete_group(group_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Chat group not found")
    return {"success": True, "group_id": group_id}


@router.post("/batch-delete", response_model=dict)
async def batch_delete_chats(
    chat_ids: list[str],
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
):
    """Delete chats by chat IDs.

    Args:
        chat_ids: List of chat IDs
        mgr: Chat manager dependency
    Returns:
        True if deleted, False if failed

    """
    chats = {chat.id: chat for chat in await mgr.list_chats(archived=None)}
    deleted = await mgr.delete_chats(chat_ids=chat_ids)
    if deleted:
        await CHECKPOINT_RUNTIME.delete_session_checkpoints(
            workspace,
            [
                (chat.session_id, chat.user_id, chat.channel)
                for chat_id in chat_ids
                if (chat := chats.get(chat_id)) is not None
            ],
        )
    return {"deleted": deleted}


# ----- Archive endpoints -----


class BatchChatIds(BaseModel):
    """Request body for batch archive/unarchive."""

    chat_ids: list[str] = Field(
        ...,
        max_length=MAX_BATCH_SIZE,
        description="List of chat IDs to process",
    )


@router.post("/actions/batch-archive", response_model=BatchArchiveResult)
async def batch_archive_chats(
    payload: BatchChatIds,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
):
    """Batch archive chats. Running chats are skipped."""
    tracker = workspace.task_tracker
    return await mgr.batch_archive(
        chat_ids=payload.chat_ids,
        get_status=tracker.get_status,
    )


@router.post("/actions/batch-unarchive", response_model=BatchArchiveResult)
async def batch_unarchive_chats(
    payload: BatchChatIds,
    mgr: ChatManager = Depends(get_chat_manager),
):
    """Batch unarchive chats."""
    return await mgr.batch_unarchive(chat_ids=payload.chat_ids)


@router.post("/{chat_id}/archive", response_model=ChatSpec)
async def archive_chat(
    chat_id: str,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
):
    """Archive a single chat. Idempotent.

    Returns 409 if the chat is currently running.
    """
    status = await workspace.task_tracker.get_status(chat_id)
    try:
        result = await mgr.archive_chat(chat_id, check_status=status)
    except ValueError as e:
        raise HTTPException(
            status_code=409,
            detail="Chat is currently in progress, cannot archive",
        ) from e
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )
    return result


@router.post("/{chat_id}/unarchive", response_model=ChatSpec)
async def unarchive_chat(
    chat_id: str,
    mgr: ChatManager = Depends(get_chat_manager),
):
    """Unarchive a single chat. Idempotent."""
    result = await mgr.unarchive_chat(chat_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )
    return result


@router.get("/{chat_id}/project-dir", deprecated=True)
async def get_chat_project_dir(
    chat_id: str,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
) -> dict:
    """Return the Session override and effective project directory.

    Deprecated single-value view; use ``/project-dirs``.
    """
    chat = await mgr.get_chat(chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    return await _project_directory_response(chat, workspace)


@router.put("/{chat_id}/project-dir", deprecated=True)
async def set_chat_project_dir(
    chat_id: str,
    body: ProjectDirectoryUpdate,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
) -> dict:
    """Persist a validated Session project directory override.

    Deprecated single-value write; stored as a one-entry list so the
    plural endpoints see the same state. Use ``PUT /project-dirs``.
    """

    def _resolve_target() -> Path:
        target = Path(body.project_dir).expanduser().resolve()
        if not target.is_dir():
            raise NotADirectoryError(str(target))
        return target

    try:
        target = await asyncio.to_thread(_resolve_target)
    except NotADirectoryError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Project directory is unavailable: {exc}",
        ) from exc
    chat = await mgr.set_session_project_dirs(
        chat_id,
        [{"path": str(target), "label": None}],
    )
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    return await _project_directory_response(chat, workspace)


@router.delete("/{chat_id}/project-dir", deprecated=True)
async def clear_chat_project_dir(
    chat_id: str,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
) -> dict:
    """Clear the override and inherit the Agent default project directory.

    Deprecated; use ``DELETE /project-dirs``.
    """
    chat = await mgr.set_session_project_dirs(chat_id, None)
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    return await _project_directory_response(chat, workspace)


@router.get("/{chat_id}/project-dirs", response_model=ProjectDirsResponse)
async def get_chat_project_dirs(
    chat_id: str,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
) -> dict:
    """Return this chat's effective project-directory list, primary first."""
    chat = await mgr.get_chat(chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    return await _project_dirs_response(chat, workspace)


@router.put("/{chat_id}/project-dirs", response_model=ProjectDirsResponse)
async def set_chat_project_dirs(
    chat_id: str,
    payload: ProjectDirsRequest,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
) -> dict:
    """Bind this chat to an ordered project-directory list.

    The first entry is the primary project directory. The override is
    persisted server-side, so it survives a page reload or a different
    browser. It takes effect on the **next** turn — an in-flight turn
    keeps the directories it started with.

    Paths that do not exist are rejected here (rather than stored and
    flagged) because this endpoint is the point where the user picks
    them and can still correct the mistake. Duplicate paths
    (case-insensitive) are collapsed, keeping the first occurrence.
    """
    from ...services.project_directory import (
        MAX_PROJECT_DIRS,
        normalize_project_dir_list,
    )

    def _normalize() -> tuple[list[dict], Optional[str], int]:
        """Normalize and existence-check in one worker thread.

        The ``is_dir()`` calls belong in here with the ``resolve()`` that
        ``normalize_project_dir_list`` does: leaving them on the event
        loop meant up to ``MAX_PROJECT_DIRS`` blocking stats per request,
        and one unresponsive mount stalled every other connection.
        """
        entries = normalize_project_dir_list(
            [entry.model_dump() for entry in payload.project_dirs],
        )
        missing = next(
            (str(path) for path, _label in entries if not path.is_dir()),
            None,
        )
        stored = [
            {"path": str(path), "label": label} for path, label in entries
        ]
        return stored, missing, len(entries)

    stored, missing, count = await asyncio.to_thread(_normalize)
    if not count:
        raise HTTPException(
            status_code=422,
            detail="project_dirs must contain at least one valid entry",
        )
    if count > MAX_PROJECT_DIRS:
        raise HTTPException(
            status_code=422,
            detail=f"Too many project dirs (max {MAX_PROJECT_DIRS})",
        )
    if missing is not None:
        raise HTTPException(
            status_code=422,
            detail=f"Not a directory: {missing}",
        )

    updated = await mgr.set_session_project_dirs(chat_id, stored)
    if updated is None:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )
    return await _project_dirs_response(updated, workspace)


@router.delete(
    "/{chat_id}/project-dirs",
    response_model=ProjectDirsResponse,
)
async def clear_chat_project_dirs(
    chat_id: str,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
) -> dict:
    """Drop this chat's override so it inherits the agent default again."""
    updated = await mgr.set_session_project_dirs(chat_id, None)
    if updated is None:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )
    return await _project_dirs_response(updated, workspace)


# ----- Existing CRUD endpoints -----


@router.get("/{chat_id}/actions", response_model=list[ActionRecord])
async def list_chat_actions(
    chat_id: str,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
) -> list[ActionRecord]:
    """Return the newest privacy-safe Action records for one ChatSpec."""
    chat_spec = await mgr.get_chat(chat_id)
    if not chat_spec:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )
    records = await lite_action_store(
        Path(workspace.workspace_dir),
    ).list_for_conversation(
        chat_id,
        limit=limit,
    )
    return [public_action_record(record) for record in records]


@router.get("/{chat_id}/model-calls", response_model=list[ModelCallRecord])
async def list_chat_model_calls(
    chat_id: str,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
) -> list[ModelCallRecord]:
    """Return content-free provider attempts for one owned ChatSpec."""
    chat_spec = await mgr.get_chat(chat_id)
    if not chat_spec:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )
    records = await lite_model_call_store(
        Path(workspace.workspace_dir),
    ).list_for_conversation(
        chat_id,
        limit=limit,
    )
    return list(records)


@router.get(
    "/{chat_id}/observations",
    response_model=list[RuntimeObservation],
)
async def list_chat_observations(
    chat_id: str,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
) -> list[RuntimeObservation]:
    """Return semantic observations for one owned ChatSpec."""
    chat_spec = await mgr.get_chat(chat_id)
    if not chat_spec:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )
    observations = await lite_observation_projection(
        Path(workspace.workspace_dir),
        agent_id=workspace.agent_id,
        interactions=workspace.interaction_service,
        controls=workspace.invocation_control,
        verifications=lite_verification_history(
            Path(workspace.workspace_dir),
        ),
    ).list_for_conversation(
        chat_id,
        limit=limit,
    )
    return list(observations)


@router.get(
    "/{chat_id}/observations/page",
    response_model=ObservationPage,
)
async def page_chat_observations(
    chat_id: str,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
) -> ObservationPage:
    """Return a stable fixed-watermark semantic-observation page."""
    chat_spec = await mgr.get_chat(chat_id)
    if not chat_spec:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )
    projection = lite_observation_projection(
        Path(workspace.workspace_dir),
        agent_id=workspace.agent_id,
        interactions=workspace.interaction_service,
        controls=workspace.invocation_control,
        verifications=lite_verification_history(
            Path(workspace.workspace_dir),
        ),
    )
    try:
        return await projection.page_for_conversation(
            chat_id,
            limit=limit,
            cursor=cursor,
        )
    except ObservationCursorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{chat_id}/status", response_model=ChatStatusResponse)
async def get_chat_status(
    chat_id: str,
    workspace=Depends(get_workspace),
) -> ChatStatusResponse:
    """Return agent-scoped run status without reading chat persistence."""
    status = await workspace.task_tracker.get_status(chat_id)
    return ChatStatusResponse(status=status)


@router.get("/{chat_id}", response_model=ChatHistory)
async def get_chat(
    chat_id: str,
    include_app_owned: bool = Query(
        True,
        description=(
            "Allow reading PawApp-owned chat history. The main Chat surface "
            "opts out so app dialogues stay inside their owning app."
        ),
    ),
    mgr: ChatManager = Depends(get_chat_manager),
    session: SafeJSONSession = Depends(get_session),
    workspace=Depends(get_workspace),
):
    """Get detailed information about a specific chat by UUID.

    Args:
        request: FastAPI request (for agent context)
        chat_id: Chat UUID
        mgr: Chat manager dependency
        session: SafeJSONSession dependency

    Returns:
        ChatHistory with messages and status (idle/running)

    Raises:
        HTTPException: If chat not found (404)
    """
    chat_spec = await mgr.get_chat(chat_id)
    if not chat_spec:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )
    if not include_app_owned and _is_app_owned_chat(chat_spec):
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )

    state = await session.get_session_state_dict(
        chat_spec.session_id,
        chat_spec.user_id,
        chat_spec.channel,
    )
    backend = workspace.config.backend
    context = ((state.get("agent") or {}).get("state") or {}).get("context")
    if not context and backend != "qwenpaw":
        try:
            await workspace.harness_runtime.hydrate_session(
                backend=backend,
                session_id=chat_spec.session_id,
                user_id=chat_spec.user_id,
                channel=chat_spec.channel,
                settings=dict(workspace.config.backend_settings),
            )
            state = await session.get_session_state_dict(
                chat_spec.session_id,
                chat_spec.user_id,
                chat_spec.channel,
            )
        except Exception:
            logger.debug(
                "Third-party session recovery failed for %s",
                chat_spec.session_id,
                exc_info=True,
            )
    status = await workspace.task_tracker.get_status(chat_id)
    if not state:
        return ChatHistory(messages=[], status=status)

    agent_raw = state.get("agent", {})
    memories: list[Msg] = []

    state_raw = agent_raw.get("state")
    if isinstance(state_raw, dict):
        try:
            agent_state = AgentState.model_validate(state_raw)
            memories = list(agent_state.context)
        except Exception:
            logger.debug(
                "Failed to parse agent.state, falling back to legacy",
                exc_info=True,
            )

    # Legacy fallback: 1.x ``agent.memory`` format.
    if not memories:
        memory_raw = agent_raw.get("memory", {})
        if memory_raw:
            memories, _summary = parse_legacy_memory_state(memory_raw)

    messages = agentscope_msg_to_message(memories)
    return ChatHistory(messages=messages, status=status)


def _artifact_receipt_from_history(
    history: ChatHistory,
    artifact_id: UUID,
) -> str | None:
    """Find the opaque receipt paired with an artifact in Chat history."""
    for message in history.messages:
        for part in message.content:
            if isinstance(part, dict):
                raw_artifact = part.get("artifact_ref")
                receipt = part.get("artifact_receipt")
                data = part.get("data")
            else:
                raw_artifact = getattr(part, "artifact_ref", None)
                receipt = getattr(part, "artifact_receipt", None)
                data = getattr(part, "data", None)
            if not isinstance(raw_artifact, dict) or not receipt:
                raw_artifact = None
            if raw_artifact is not None and str(
                raw_artifact.get("artifact_id"),
            ) == str(artifact_id):
                return str(receipt)
            if not isinstance(data, dict):
                continue
            links = data.get("artifact_links")
            if not isinstance(links, list):
                continue
            for link in links:
                if not isinstance(link, dict):
                    continue
                linked_artifact = link.get("artifact_ref")
                linked_receipt = link.get("artifact_receipt")
                if (
                    isinstance(linked_artifact, dict)
                    and linked_receipt
                    and str(linked_artifact.get("artifact_id"))
                    == str(artifact_id)
                ):
                    return str(linked_receipt)
    return None


@router.get("/{chat_id}/artifacts/{artifact_id}/content")
async def get_chat_artifact_content(
    chat_id: str,
    artifact_id: UUID,
    disposition: Annotated[
        str,
        Query(pattern=r"^(inline|attachment)$"),
    ] = "inline",
    mgr: ChatManager = Depends(get_chat_manager),
    session: SafeJSONSession = Depends(get_session),
    workspace=Depends(get_workspace),
) -> Response:
    """Read a Chat-owned artifact through the shared safe renderer."""
    history = await get_chat(
        chat_id,
        True,
        mgr,
        session,
        workspace,
    )
    receipt_id = _artifact_receipt_from_history(history, artifact_id)
    if receipt_id is None:
        raise HTTPException(
            status_code=404,
            detail="Artifact does not belong to this chat",
        )
    try:
        lineage_ids = await mgr.get_fork_lineage_ids(chat_id)
        artifact, _ = await conversation_artifact_receipts(
            Path(workspace.workspace_dir),
        ).resolve(
            receipt_id=receipt_id,
            chat_id=chat_id,
            artifact_id=artifact_id,
            inherited_chat_ids=lineage_ids[1:],
        )
    except ConversationArtifactReceiptError as exc:
        raise HTTPException(
            status_code=404,
            detail="Artifact does not belong to this chat",
        ) from exc
    if (
        disposition == "inline"
        and artifact.size_bytes > _MAX_ARTIFACT_PREVIEW_BYTES
    ):
        raise HTTPException(
            status_code=413,
            detail="Artifact is too large for inline preview",
        )
    try:
        content = await lite_artifact_store(
            Path(workspace.workspace_dir),
        ).read(artifact)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail="Artifact content is unavailable",
        ) from exc
    except ArtifactIntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail="Artifact content failed integrity verification",
        ) from exc

    registry = capability_registry_for(workspace)

    async def resolve_workspace(agent_id: str):
        if agent_id == workspace.agent_id:
            return workspace
        raise LookupError(agent_id)

    await registry.ensure_bundle(
        SYSTEM_CAPABILITY_BUNDLE,
        system_contribution_factory(resolve_workspace),
    )
    render_disposition = ArtifactRenderDisposition(disposition)
    render_budget = (
        _MAX_ARTIFACT_PREVIEW_BYTES
        if render_disposition is ArtifactRenderDisposition.INLINE
        else max(len(content), 1)
    )
    try:
        rendered, generation = await ArtifactRenderService(registry).render(
            artifact,
            content,
            disposition=render_disposition,
            filename=artifact_filename(
                artifact.metadata,
                artifact.artifact_id,
            ),
            max_output_bytes=render_budget,
        )
    except ArtifactPreviewTooLargeError as exc:
        raise HTTPException(
            status_code=413,
            detail="Artifact is too large for inline preview",
        ) from exc
    except ArtifactRendererUnavailableError as exc:
        status_code = 503 if exc.failures else 415
        detail = (
            "Artifact renderers failed without a safe fallback"
            if exc.failures
            else "No safe renderer supports this artifact"
        )
        raise HTTPException(status_code=status_code, detail=detail) from exc

    encoded_filename = quote(rendered.filename, safe="")
    headers = {
        "Cache-Control": "private, no-store",
        "Content-Disposition": (
            f"{rendered.disposition.value}; "
            f"filename*=UTF-8''{encoded_filename}"
        ),
        "X-Content-Type-Options": "nosniff",
        "X-QwenPaw-Artifact-Renderer": rendered.renderer_id,
        "X-QwenPaw-Registry-Generation": str(generation),
        "X-QwenPaw-Source-Hash": rendered.source_content_hash,
    }
    if rendered.disposition is ArtifactRenderDisposition.INLINE:
        headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return Response(
        content=rendered.content,
        media_type=rendered.media_type,
        headers=headers,
    )


@router.put("/{chat_id}", response_model=ChatSpec)
async def update_chat(
    chat_id: str,
    spec: ChatUpdate,
    mgr: ChatManager = Depends(get_chat_manager),
):
    """Update an existing chat.

    Args:
        chat_id: Chat UUID
        spec: Partial chat update payload
        mgr: Chat manager dependency

    Returns:
        Updated chat spec

    Raises:
        HTTPException: If chat not found (404)
    """
    try:
        updated = await mgr.patch_chat(chat_id, spec)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )
    return updated


@router.delete("/{chat_id}", response_model=dict)
async def delete_chat(
    chat_id: str,
    mgr: ChatManager = Depends(get_chat_manager),
    workspace=Depends(get_workspace),
):
    """Delete a chat by UUID.

    Note: This only deletes the chat spec (UUID mapping).
    JSONSession state is NOT deleted.

    Args:
        chat_id: Chat UUID
        mgr: Chat manager dependency

    Returns:
        True if deleted, False if failed

    Raises:
        HTTPException: If chat not found (404)
    """
    chat = await mgr.get_chat(chat_id)
    deleted = await mgr.delete_chats(chat_ids=[chat_id])
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"Chat not found: {chat_id}",
        )
    if chat is not None:
        await CHECKPOINT_RUNTIME.delete_session_checkpoints(
            workspace,
            [(chat.session_id, chat.user_id, chat.channel)],
        )
    return {"deleted": True}
