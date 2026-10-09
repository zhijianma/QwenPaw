# -*- coding: utf-8 -*-
"""Console APIs: push messages, chat, and file upload for chat."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, Optional, Union

from fastapi import (
    APIRouter,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from pydantic import BaseModel, ValidationError
from starlette.responses import StreamingResponse

from qwenpaw.schemas import (
    AgentRequest,
    _coerce_content_item,
)
from qwenpaw.tool_calls import CancelReason
from qwenpaw.utils.timeout import resolve_stream_task_timeout
from ...invocation_control import (
    QueueCommandConflictError,
    QueueRevisionConflictError,
)
from ...kernel import ControlCommandStatus
from ...inbox import SQLiteInboxProjectionStore
from ...kernel.models import (
    EvidenceRef,
    ExecutionContract,
    PlanStep,
    Task,
    TaskSource,
)
from ...tasks.artifacts import lite_artifact_store
from ...tasks.bootstrap import task_service_for_workspace
from ...tasks.conversation_artifacts import conversation_artifact_receipts
from ...tasks.service import TaskService
from ...tasks.usage_scope import (
    RECORD_MODEL_USAGE_CONTEXT_KEY,
    USAGE_SCOPE_CONTEXT_KEY,
    USAGE_SCOPE_LEASE_CONTEXT_KEY,
    UsageScopeUnavailableError,
    close_usage_scope_in_context,
    lite_usage_scope_registry,
)
from ...utils.logging import LOG_FILE_PATH, sanitize_log_value
from ..agent_context import get_agent_for_request
from ..approvals.display import approval_display_fields
from ..chats.models import ChatUpdate
from ..chats.input_artifacts import (
    ConversationInputArtifactError,
    claim_conversation_artifacts,
)
from ..chats.compatibility import observe_legacy_stop
from ..chats.title_generator import generate_and_update_title
from ..legacy_inbox_observation import (
    LegacyInboxDualReadAssessment,
    SQLiteLegacyInboxObservationStore,
)
from ..utils import check_upload_size

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/console", tags=["console"])


# ── Background task store ──


@dataclass
class _BackgroundTask:
    """In-memory state for a background chat task."""

    status: str = "submitted"
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    result: Optional[Dict[str, Any]] = None
    asyncio_task: Optional[asyncio.Task] = None


_bg_tasks: Dict[str, _BackgroundTask] = {}
_bg_lock = asyncio.Lock()


class MarkInboxReadRequest(BaseModel):
    event_ids: list[str] = []
    all: bool = False


def _inbox_projection_store(workspace: Any) -> SQLiteInboxProjectionStore:
    return SQLiteInboxProjectionStore(
        Path(workspace.workspace_dir) / ".qwenpaw" / "lite" / "inbox.db",
    )


def _legacy_inbox_event(item: Any) -> dict[str, Any]:
    payload = {
        **item.source_payload,
        "delivery_id": str(item.delivery_id),
        "delivery_status": item.delivery_status.value,
        "delivery_attempt": item.delivery_attempt,
        "conversation_id": item.conversation_id,
        "task_id": str(item.task_id) if item.task_id else None,
        "run_id": str(item.run_id) if item.run_id else None,
        "artifact_refs": [
            ref.model_dump(mode="json") for ref in item.artifact_refs
        ],
        "evidence_refs": [
            ref.model_dump(mode="json") for ref in item.evidence_refs
        ],
    }
    return {
        "id": str(item.item_id),
        "agent_id": item.agent_id,
        "source_type": item.source_type,
        "source_id": item.source_id,
        "event_type": item.event_type,
        "status": item.source_status,
        "severity": item.severity,
        "title": item.title,
        "body": item.summary,
        "payload": payload,
        "read": item.read,
        "created_at": item.created_at.timestamp(),
    }


MAX_DEBUG_LOG_LINES = 1000


def _resolve_effective_stream_task_timeout(
    raw_timeout: Any,
) -> int:
    """Resolve background chat-task timeout in seconds.

    Thin wrapper over :func:`qwenpaw.utils.timeout.resolve_stream_task_timeout`
    so console routes share one parse/default contract with tools.
    """
    return resolve_stream_task_timeout(raw_timeout, field_name="timeout")


def _background_task_cancel_error(
    *,
    timed_out: bool,
    timeout_seconds: int,
) -> Dict[str, Any]:
    """Build the error payload for a cancelled background chat task."""
    if timed_out:
        return {
            "message": f"Task timed out after {timeout_seconds}s",
            "code": "timeout",
        }
    return {"message": "Task cancelled"}


def _finish_background_task_with_error(
    task: _BackgroundTask,
    error: Dict[str, Any],
) -> None:
    """Record one terminal background-task failure consistently."""
    task.status = "finished"
    task.finished_at = time.time()
    task.result = {"status": "failed", "error": error}


def _safe_filename(name: str) -> str:
    """Safe basename, alphanumeric/./-/_, max 200 chars."""
    base = Path(name).name if name else "file"
    return re.sub(r"[^\w.\-]", "_", base)[:200] or "file"


def _extract_placeholder_name(content_parts: list) -> tuple[str, str]:
    """Return ``(placeholder_name, first_user_text)`` for a new chat.

    The placeholder name shows up in the session drawer immediately while a
    background task asks the model for a real title. Content shapes match
    ``channels/base.py::_extract_chat_name``: dict blocks like
    ``{"type": "text", "text": "..."}``, raw strings, and objects with a
    ``.text`` attribute. Anything else (audio/image/file blocks) is treated
    as media and gets the generic "Media Message" placeholder.
    """
    if not content_parts:
        return "New Chat", ""
    content = content_parts[0]
    if not content:
        return "Media Message", ""
    if isinstance(content, str):
        first_text = content
    elif isinstance(content, dict):
        text = content.get("text", "")
        first_text = text if isinstance(text, str) else ""
    elif hasattr(content, "text"):
        first_text = content.text or ""
    else:
        first_text = ""
    if not first_text:
        return "Media Message", ""
    return first_text[:10], first_text


async def _persist_pending_project_dirs(
    workspace,
    chat,
    native_payload: dict[str, Any],
):
    """Bind pending project dirs sent with a new chat's first message.

    The console can only offer a directory picker *before* a chat
    exists, so the choice arrives in ``request_context`` as
    ``session_project_dirs`` (ordered list, primary first; the legacy
    singular ``session_project_dir`` is still honoured). Entries are
    validated here rather than trusted: they come from a client, and a
    bad value would otherwise be written into the chat and silently
    steer every later turn.

    Never overwrites an existing session override — a chat that already
    has one is not a new chat, and clobbering it would lose the user's
    setting.

    The keys are popped once they have been **consumed** — persisted onto
    the chat, where every later turn reads them from. If persistence does
    not happen (the chat vanished), they are put back so that
    ``ContextVarsSetupHook`` can still honour the user's pick for this
    first turn instead of silently falling back to the agent default.
    """
    from ..chats.input_context import persist_pending_project_dirs

    return await persist_pending_project_dirs(
        workspace,
        chat,
        native_payload,
    )


def _extract_session_and_payload(  # pylint: disable=too-many-branches
    request_data: Union[AgentRequest, dict],
):
    """Extract run_key (ChatSpec.id), session_id, and native payload.

    run_key must be ChatSpec.id (chat_id) so it matches list_chats/get_chat.
    """
    if isinstance(request_data, AgentRequest):
        channel_id = getattr(request_data, "channel", None) or "console"
        sender_id = request_data.user_id or "default"
        session_id = request_data.session_id or "default"
        content_parts = (
            list(request_data.input[0].content) if request_data.input else []
        )
        message_metadata = (
            request_data.input[0].metadata if request_data.input else None
        )
        message_id = request_data.input[0].id if request_data.input else ""
    else:
        channel_id = request_data.get("channel", "console")
        sender_id = request_data.get("user_id", "default")
        session_id = request_data.get("session_id", "default")
        input_data = request_data.get("input", [])
        content_parts = []
        message_metadata = None
        message_id = ""
        for content_part in input_data:
            if hasattr(content_part, "content"):
                content_parts.extend(list(content_part.content or []))
                message_metadata = getattr(
                    content_part,
                    "metadata",
                    message_metadata,
                )
                message_id = str(
                    getattr(content_part, "id", message_id) or message_id,
                )
            elif isinstance(content_part, dict) and "content" in content_part:
                # Coerce raw dicts to typed Content models so downstream
                # getattr checks (e.g. _content_has_text) see real attrs.
                content_parts.extend(
                    _coerce_content_item(c)
                    for c in (content_part["content"] or [])
                )
                if isinstance(content_part.get("metadata"), dict):
                    message_metadata = content_part["metadata"]
                message_id = str(content_part.get("id") or message_id)

    if isinstance(message_metadata, dict):
        message_id = str(
            message_metadata.get("qwenpaw_client_message_id") or message_id,
        )

    meta: dict = {
        "session_id": session_id,
        "user_id": sender_id,
    }

    # Preserve request_context (e.g. session-level approval_level)
    if isinstance(request_data, AgentRequest):
        rc = getattr(request_data, "request_context", None)
    else:
        rc = request_data.get("request_context")
    if isinstance(rc, dict) and rc:
        meta["request_context"] = rc

    native_payload = {
        "channel_id": channel_id,
        "sender_id": sender_id,
        "content_parts": content_parts,
        "message_metadata": message_metadata,
        "message_id": message_id,
        "meta": meta,
    }

    if isinstance(request_data, AgentRequest):
        mso = getattr(request_data, "model_slot_override", None)
    else:
        mso = request_data.get("model_slot_override")
    if mso is not None:
        native_payload["model_slot_override"] = mso

    return native_payload


def _bind_os_runtime_identity(
    native_payload: dict[str, Any],
    conversation_id: str,
) -> None:
    """Attach server-owned conversation and idempotency identities."""
    meta = native_payload.setdefault("meta", {})
    raw_context = meta.get("request_context")
    request_context = (
        dict(raw_context) if isinstance(raw_context, dict) else {}
    )
    request_context["os_conversation_id"] = conversation_id
    request_context["os_submission_idempotency_key"] = str(
        native_payload.get("message_id") or uuid.uuid4(),
    )
    meta["request_context"] = request_context


def _bind_inherited_usage_scope(
    native_payload: dict[str, Any],
    *,
    agent_id: str,
) -> None:
    """Resolve a child HTTP token to the root Task's live UsageMeter."""
    request_context = native_payload.get("meta", {}).get("request_context")
    if not isinstance(request_context, dict):
        return
    raw_scope_id = request_context.get(USAGE_SCOPE_CONTEXT_KEY)
    if not isinstance(raw_scope_id, str) or not raw_scope_id:
        return
    if request_context.get("_spawn_subagent") is not True:
        raise HTTPException(
            status_code=403,
            detail="Task usage scope is restricted to child dispatch",
        )
    try:
        lease = lite_usage_scope_registry.acquire(
            raw_scope_id,
            agent_id=agent_id,
        )
    except UsageScopeUnavailableError as exc:
        raise HTTPException(
            status_code=409,
            detail="Task usage scope is unavailable or expired",
        ) from exc
    request_context["_task_usage_meter"] = lease.meter
    request_context[USAGE_SCOPE_LEASE_CONTEXT_KEY] = lease
    request_context[RECORD_MODEL_USAGE_CONTEXT_KEY] = True


def _release_bound_usage_scope(native_payload: dict[str, Any]) -> None:
    """Release a bound scope when dispatch fails before Runtime owns it."""
    request_context = native_payload.get("meta", {}).get("request_context")
    if not isinstance(request_context, dict):
        return
    close_usage_scope_in_context(request_context)


async def _claim_conversation_artifacts(
    workspace: Any,
    chat_id: str,
    content_parts: list[Any],
) -> None:
    """Validate and bind uploaded artifact receipts to one Chat."""
    try:
        await claim_conversation_artifacts(
            workspace,
            chat_id,
            content_parts,
        )
    except ConversationInputArtifactError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _is_reconnect_request(request_data: Union[AgentRequest, dict]) -> bool:
    """Return whether the chat request asks to attach to a running stream.

    ``AgentRequest`` uses ``extra="allow"`` and has no required fields,
    so FastAPI parses ``{"reconnect": true, ...}`` bodies into an
    ``AgentRequest`` instance — a dict-only check silently classified
    every reconnect as a fresh send and restarted a run with an empty
    input. Check both shapes.
    """
    if isinstance(request_data, dict):
        return request_data.get("reconnect") is True
    return getattr(request_data, "reconnect", None) is True


def _chat_registration_fields(native_payload: dict[str, Any]) -> dict:
    """Return first-class subagent fields from an internal request."""
    request_context = native_payload["meta"].get("request_context")
    if not isinstance(request_context, dict):
        return {}
    if request_context.get("_spawn_subagent") is not True:
        return {}
    return {
        "source": "subagent",
        "parent_session_id": str(
            request_context.get("parent_session_id") or "",
        )
        or None,
        "root_session_id": str(
            request_context.get("root_session_id") or "",
        )
        or None,
    }


def _empty_sse_response() -> StreamingResponse:
    """An SSE response that terminates immediately."""

    async def _empty() -> AsyncGenerator[str, None]:
        return
        yield  # pragma: no cover — makes this an async generator

    return StreamingResponse(
        _empty(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


def _tail_text_file(
    path: Path,
    *,
    lines: int = 200,
    max_bytes: int = 512 * 1024,
) -> str:
    """Read the last N lines from a text file with bounded memory."""
    path = Path(path)
    if not path.exists() or not path.is_file():
        return ""
    try:
        size = path.stat().st_size
        if size == 0:
            return ""
        with open(path, "rb") as f:
            if size <= max_bytes:
                data = f.read()
            else:
                f.seek(max(size - max_bytes, 0))
                data = f.read()
        text = data.decode("utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])
    except Exception:
        logger.exception("Failed to read backend debug log file")
        return ""


@router.post(
    "/chat",
    status_code=200,
    summary="Chat with console (streaming response)",
    description="Agent API Request Format. See runtime.agentscope.io. "
    "Use body.reconnect=true to attach to a running stream.",
)
async def post_console_chat(
    request_data: Union[AgentRequest, dict],
    request: Request,
) -> StreamingResponse:
    """Stream agent response. Run continues in background after disconnect.
    Stop via POST /console/chat/stop. Reconnect with body.reconnect=true.
    """
    workspace = await get_agent_for_request(request)
    console_channel = await workspace.channel_manager.get_channel("console")
    if console_channel is None:
        raise HTTPException(
            status_code=503,
            detail="Channel Console not found",
        )
    try:
        native_payload = _extract_session_and_payload(request_data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    session_id = console_channel.resolve_session_id(
        sender_id=native_payload["sender_id"],
        channel_meta=native_payload["meta"],
    )
    name, first_text = _extract_placeholder_name(
        native_payload["content_parts"],
    )
    chat = await workspace.chat_manager.get_or_create_chat(
        session_id,
        native_payload["sender_id"],
        native_payload["channel_id"],
        name=name,
        **_chat_registration_fields(native_payload),
    )
    _bind_os_runtime_identity(native_payload, chat.id)
    tracker = workspace.task_tracker
    is_reconnect = _is_reconnect_request(request_data)

    if is_reconnect:
        queue = await tracker.attach(chat.id)
        if queue is None:
            # The run finished (or never existed): reply with an
            # immediately-terminated SSE stream so the client's reader
            # completes normally and falls back to the persisted
            # history. Returning a JSON null here left the chat blank.
            return _empty_sse_response()
    else:
        # The web UI allocates a Chat UUID before its first message. Give
        # that explicit placeholder the same first-turn naming behavior as
        # get_or_create_chat, without overwriting a user's renamed Chat.
        if (
            first_text
            and not chat.last_finished_at
            and chat.meta.get("console_placeholder_name") == chat.name
        ):
            renamed = await workspace.chat_manager.patch_chat_if_name_matches(
                chat.id,
                chat.name,
                ChatUpdate(name=name),
            )
            if renamed is not None:
                chat = renamed
        chat = await _persist_pending_project_dirs(
            workspace,
            chat,
            native_payload,
        )
        await _claim_conversation_artifacts(
            workspace,
            chat.id,
            native_payload["content_parts"],
        )
        # Project directories are resolved exactly once, inside
        # ContextVarsSetupHook (from the chat meta persisted above);
        # the router no longer pre-resolves or injects them.
        _bind_inherited_usage_scope(
            native_payload,
            agent_id=workspace.agent_id,
        )
        try:
            queue, is_new_run = await tracker.attach_or_start(
                chat.id,
                native_payload,
                console_channel.stream_one,
                owner=workspace,
                on_finished=workspace.chat_manager.mark_chat_finished,
            )
        except BaseException:
            _release_bound_usage_scope(native_payload)
            raise
        if not is_new_run:
            _release_bound_usage_scope(native_payload)
            await tracker.detach_subscriber(chat.id, queue)
            raise HTTPException(
                status_code=409,
                detail=(
                    "A task is already running for this chat. Wait for it "
                    "to finish or use a different session_id."
                ),
            )

        # Title generation is only needed when starting a new run.
        if first_text and chat.name == name:
            asyncio.create_task(
                generate_and_update_title(
                    workspace=workspace,
                    chat_id=chat.id,
                    user_message=first_text,
                    placeholder_name=name,
                ),
            )

    async def event_generator() -> AsyncGenerator[str, None]:
        # Hold iterator so finally can aclose(); guarantees stream_from_queue's
        # finally (detach_subscriber) on client abort / generator teardown.
        stream_it = tracker.stream_from_queue(queue, chat.id)
        try:
            try:
                async for event_data in stream_it:
                    yield event_data
            except Exception as e:
                logger.exception("Console chat stream error")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            await stream_it.aclose()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@router.post(
    "/chat/stop",
    status_code=200,
    summary="Stop running console chat",
)
async def post_console_chat_stop(
    request: Request,
    chat_id: str = Query(..., description="Chat id (ChatSpec.id) to stop"),
) -> dict:
    """Stop the running chat and its foreground tool calls."""
    logger.debug("[STOP API] Received stop request for chat_id=%s", chat_id)
    workspace = await get_agent_for_request(request)
    observation_id = f"console-stop:{uuid.uuid4()}"

    resolved_chat_id = chat_id
    runtime_session_id: str | None = None
    chat_manager = workspace.chat_manager
    if chat_manager:
        chat = await chat_manager.get_chat(chat_id)
        if chat is None:
            candidate = await chat_manager.get_chat_id_by_session(
                session_id=chat_id,
                channel="console",
            )
            if candidate:
                resolved_chat_id = candidate
                chat = await chat_manager.get_chat(candidate)
                logger.debug(
                    "[STOP API] Resolved session_id=%s to chat_id=%s",
                    chat_id[:12] if len(chat_id) >= 12 else chat_id,
                    candidate,
                )
        if chat is not None:
            runtime_session_id = chat.session_id

    control = getattr(workspace, "invocation_control", None)
    interrupt_command_id: uuid.UUID | None = None
    interrupt_applied = False
    if control is not None and resolved_chat_id:
        try:
            receipt = await control.interrupt_current(
                agent_id=workspace.agent_id,
                conversation_id=resolved_chat_id,
                idempotency_key=observation_id,
            )
            if receipt is not None:
                interrupt_command_id = receipt.command_id
                interrupt_applied = (
                    receipt.status is ControlCommandStatus.APPLIED
                )
        except (QueueCommandConflictError, QueueRevisionConflictError):
            logger.info(
                "[STOP API] invocation changed before interrupt applied "
                "chat_id=%s",
                resolved_chat_id,
            )

    if interrupt_applied:
        await observe_legacy_stop(
            workspace,
            observation_id=observation_id,
            entrypoint="console.stop_api",
            disposition="os_interrupt",
        )
        logger.debug(
            "[STOP API] OS interrupt applied chat_id=%s",
            resolved_chat_id,
        )
        return {"stopped": True}

    tool_cancelled = 0
    app_services = getattr(request.app.state, "app_services", None)
    coordinator = getattr(app_services, "tool_coordinator", None)
    cancel_session = getattr(
        coordinator,
        "cancel_running_for_session",
        None,
    )
    if runtime_session_id and callable(cancel_session):
        # Tool calls have their own task owner. Cancel them before cancelling
        # the producer so subprocess bridges can observe cancel_event and tear
        # down the process tree deterministically.
        tool_cancelled = await cancel_session(
            runtime_session_id,
            agent_id=workspace.agent_id,
            reason=CancelReason.USER,
        )

    logger.debug(
        "[STOP API] Got workspace, calling task_tracker.request_stop...",
    )
    run_stopped = await workspace.task_tracker.request_stop(resolved_chat_id)
    stopped = run_stopped or tool_cancelled > 0
    if interrupt_command_id is not None:
        await control.acknowledge_interrupt(
            interrupt_command_id,
            applied=stopped,
            detail=(
                "compatibility cancellation applied"
                if stopped
                else "runtime binding and compatibility run were unavailable"
            ),
        )

    await observe_legacy_stop(
        workspace,
        observation_id=observation_id,
        entrypoint="console.stop_api",
        disposition=(
            "compatibility_cancelled" if stopped else "no_active_invocation"
        ),
    )

    logger.debug(
        "[STOP API] stop completed: stopped=%s run_stopped=%s tools=%s",
        stopped,
        run_stopped,
        tool_cancelled,
    )
    return {"stopped": stopped}


@router.post("/upload", response_model=dict, summary="Upload file for chat")
async def post_console_upload(
    request: Request,
    file: UploadFile = File(..., description="File to attach"),
) -> dict:
    """Save to console channel media_dir."""

    workspace = await get_agent_for_request(request)
    console_channel = await workspace.channel_manager.get_channel("console")
    if console_channel is None:
        raise HTTPException(
            status_code=503,
            detail="Channel Console not found",
        )
    media_dir = console_channel.media_dir
    media_dir.mkdir(parents=True, exist_ok=True)
    data = await file.read()
    check_upload_size(data)
    safe_name = _safe_filename(file.filename or "file")
    if file.content_type in {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/bmp",
        "image/tiff",
    } or Path(
        safe_name,
    ).suffix.lower() in {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
        ".tiff",
    }:
        from io import BytesIO
        from PIL import Image

        try:
            with Image.open(BytesIO(data)) as image:
                image.verify()
        except (OSError, SyntaxError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail=(
                    "The image is damaged or unsupported. "
                    "Please upload it again."
                ),
            ) from exc
    stored_name = f"{uuid.uuid4().hex}_{safe_name}"

    path = (media_dir / stored_name).resolve()
    await asyncio.to_thread(path.write_bytes, data)
    media_type = (
        file.content_type
        or mimetypes.guess_type(safe_name)[0]
        or "application/octet-stream"
    )
    artifact = await lite_artifact_store(
        Path(workspace.workspace_dir),
    ).put(
        kind="chat.attachment",
        media_type=media_type,
        content=data,
        metadata={"name": safe_name, "source": "chat.upload"},
    )
    evidence = EvidenceRef(
        artifact_id=artifact.artifact_id,
        claim="Uploaded chat attachment",
        producer="qwenpaw.system.chat-upload",
    )
    receipt = await conversation_artifact_receipts(
        Path(workspace.workspace_dir),
    ).create(artifact, evidence)
    return {
        "url": path,
        "file_name": safe_name,
        "size": len(data),
        "artifact_ref": artifact.model_dump(mode="json"),
        "evidence_ref": evidence.model_dump(mode="json"),
        "artifact_receipt": receipt,
    }


@router.get(
    "/debug/backend-logs",
    response_model=dict,
    summary="Read backend daemon logs for debug page",
)
async def get_backend_debug_logs(
    lines: int = Query(
        200,
        ge=20,
        le=MAX_DEBUG_LOG_LINES,
        description="Number of trailing log lines to return",
    ),
) -> dict:
    """Return the tail of the project log file for the debug UI."""
    log_path = LOG_FILE_PATH.resolve()
    try:
        st = log_path.stat()
        return {
            "path": str(log_path),
            "exists": True,
            "lines": lines,
            "updated_at": st.st_mtime,
            "size": st.st_size,
            "content": _tail_text_file(log_path, lines=lines),
        }
    except FileNotFoundError:
        return {
            "path": str(log_path),
            "exists": False,
            "lines": lines,
            "updated_at": None,
            "size": 0,
            "content": "",
        }


@router.get("/push-messages")
async def get_push_messages(
    session_id: str | None = Query(None, description="Optional session id"),
):
    """
    Return pending push messages and ALL approval requests.

    Messages:
    - With session_id: consumed messages for that session
    - Without session_id: recent messages (all sessions, last 60s)

    Approvals:
    - Always returns ALL pending approvals across all sessions
    - Frontend filters by current session_id for display
    - Includes session_id in each approval for filtering
    """
    from ..console_push_store import get_recent, take
    from ..approvals import get_approval_service

    # Get messages (session-specific or global)
    if session_id:
        messages = await take(session_id)
    else:
        messages = await get_recent()

    # Get ALL pending approvals (not filtered by session)
    approval_svc = get_approval_service()
    # pylint: disable=protected-access
    async with approval_svc._lock:
        all_pending = list(approval_svc._pending.values())

    # Serialize approval data with root_session_id for frontend filtering
    approvals_data = [
        {
            "request_id": p.request_id,
            "session_id": p.session_id,
            "root_session_id": p.root_session_id,
            "owner_agent_id": p.owner_agent_id,
            "agent_id": p.agent_id,
            "tool_name": p.tool_name,
            **approval_display_fields(p),
            "severity": p.severity,
            "findings_count": p.findings_count,
            "findings_summary": p.result_summary,
            "tool_params": p.extra.get("tool_call", {}).get("input", {}),
            "source_type": p.extra.get("source_type", "tool_guard"),
            "driver": p.extra.get("driver"),
            "reasoning": p.extra.get("reasoning", ""),
            "created_at": p.created_at,
            "timeout_seconds": p.timeout_seconds,
        }
        for p in all_pending
    ]

    return {"messages": messages, "pending_approvals": approvals_data}


@router.get("/inbox/events")
async def get_inbox_events(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    source_type: str | None = Query(None),
    source_types: list[str] | None = Query(None),
    status: str | None = Query(None),
    agent_id: str | None = Query(None),
    unread_only: bool = Query(False),
):
    from ..inbox_store import query_events

    workspace = await get_agent_for_request(request)
    selected_agent = agent_id or workspace.agent_id
    selected_sources = set(source_types or [])
    if source_type:
        selected_sources.add(source_type)
    legacy_events, _, _ = await query_events(
        limit=5000,
        offset=0,
        source_types=selected_sources or None,
        status=status,
        agent_id=selected_agent,
        unread_only=unread_only,
    )
    all_items = await _inbox_projection_store(workspace).list_items(
        agent_id=selected_agent,
        unread_only=False,
        limit=5000,
        include_handled=True,
    )
    migrated_legacy_ids = {
        legacy_id
        for item in all_items
        if isinstance(
            legacy_id := item.source_payload.get("legacy_event_id"),
            str,
        )
    }
    legacy_events = [
        event
        for event in legacy_events
        if event.get("id") not in migrated_legacy_ids
    ]
    items = [
        item
        for item in all_items
        if not item.handled and (not unread_only or not item.read)
    ]
    projection_events = [
        _legacy_inbox_event(item)
        for item in items
        if not selected_sources or item.source_type in selected_sources
    ]
    if projection_events:
        if status:
            projection_events = [
                event
                for event in projection_events
                if event["status"] == status
            ]
    merged = legacy_events + projection_events
    merged.sort(
        key=lambda event: (event.get("created_at", 0), event.get("id", "")),
        reverse=True,
    )
    total = len(merged)
    unread_count = sum(not bool(event.get("read")) for event in merged)
    events = merged[offset : offset + limit]
    return {
        "events": events,
        "total": total,
        "unread_count": unread_count,
    }


@router.get(
    "/inbox/migration-observation",
    response_model=LegacyInboxDualReadAssessment,
)
async def get_inbox_migration_observation(
    request: Request,
) -> LegacyInboxDualReadAssessment:
    """Return the read-only gate for disabling legacy Inbox dual read."""
    workspace = await get_agent_for_request(request)
    store = SQLiteLegacyInboxObservationStore(
        Path(workspace.workspace_dir) / ".qwenpaw" / "lite" / "inbox.db",
    )
    return await store.assess(agent_id=workspace.agent_id)


@router.post("/inbox/read")
async def post_mark_inbox_read(
    payload: MarkInboxReadRequest,
    request: Request,
):
    from ..inbox_store import mark_all_read, mark_read

    workspace = await get_agent_for_request(request)
    projection = _inbox_projection_store(workspace)
    if payload.all:
        updated = await mark_all_read(agent_id=workspace.agent_id)
        items = await projection.list_items(
            agent_id=workspace.agent_id,
            unread_only=True,
            limit=5000,
        )
    else:
        updated = await mark_read(payload.event_ids)
        items = []
        for raw_item_id in payload.event_ids:
            try:
                item = await projection.get(uuid.UUID(raw_item_id))
            except ValueError:
                continue
            if (
                item is not None
                and item.agent_id == workspace.agent_id
                and not item.read
            ):
                items.append(item)
    for item in items:
        await projection.mark_read(
            item.item_id,
            agent_id=workspace.agent_id,
            expected_revision=item.revision,
        )
        updated += 1
    return {"updated": updated}


@router.delete("/inbox/events/{event_id}")
async def delete_inbox_event(event_id: str, request: Request):
    from ..inbox_store import delete_event
    from ..inbox_trace_store import delete_trace

    workspace = await get_agent_for_request(request)
    projection = _inbox_projection_store(workspace)
    try:
        item = await projection.get(uuid.UUID(event_id))
    except ValueError:
        item = None
    if item is not None and item.agent_id == workspace.agent_id:
        await projection.mark_handled(
            item.item_id,
            agent_id=workspace.agent_id,
            expected_revision=item.revision,
        )
        return {
            "deleted": True,
            "trace_deleted": False,
            "run_id": str(item.run_id) if item.run_id else None,
        }
    deleted, run_id, run_id_still_referenced = await delete_event(event_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="event not found")
    trace_deleted = False
    if run_id and not run_id_still_referenced:
        trace_deleted = await delete_trace(run_id)
    return {
        "deleted": True,
        "trace_deleted": trace_deleted,
        "run_id": run_id,
    }


@router.get("/inbox/traces/{run_id}")
async def get_inbox_trace(run_id: str):
    from ..inbox_trace_store import get_trace

    trace = await get_trace(run_id)
    if trace is None:
        raise HTTPException(
            status_code=404,
            detail="trace not found",
        )
    return trace


# ── Background chat task endpoints ──


def _parse_sse_payload(line: str) -> Optional[Dict[str, Any]]:
    """Parse a single SSE data line into a dict."""
    stripped = line.strip()
    if stripped.startswith("data: "):
        try:
            return json.loads(stripped[6:])
        except (json.JSONDecodeError, ValueError):
            return None
    return None


async def _finalize_background_fork(
    project_dir: str,
    branch: str,
    *,
    scope_id: str,
) -> bool:
    """Finish an in-flight fork commit before publishing a *success* result.

    Cancelling ``asyncio.to_thread`` cannot stop the Git worker. If the
    parent task is cancelled (timeout or manual cancel), re-raise immediately
    so the task API can publish a terminal failure. The Git worker keeps
    running as detached bookkeeping and must not rewrite that failure into
    ``completed``.
    """
    from qwenpaw.agents.fork_project import finalize_fork_worktree_or_fail

    finalizer = asyncio.create_task(
        asyncio.to_thread(
            finalize_fork_worktree_or_fail,
            project_dir,
            branch,
            message=f"fork worker {branch}",
            expected_scope=scope_id or None,
        ),
    )

    def _log_detached(task: asyncio.Task) -> None:
        try:
            task.result()
        except Exception:
            logger.warning(
                "Detached fork finalize failed for %s",
                sanitize_log_value(branch),
                exc_info=True,
            )

    try:
        return await asyncio.shield(finalizer)
    except asyncio.CancelledError:
        if not finalizer.done():
            finalizer.add_done_callback(_log_detached)
        raise


async def _mark_background_fork_failed(
    project_dir: str,
    branch: str,
    *,
    scope_id: str,
    reason: str,
    context: str,
) -> None:
    """Best-effort fork failure bookkeeping for background tasks."""
    if not project_dir or not branch:
        return
    try:
        from qwenpaw.agents.fork_project import mark_fork_failed

        await asyncio.to_thread(
            mark_fork_failed,
            project_dir,
            branch,
            reason=reason,
            expected_scope=scope_id or None,
        )
    except Exception:
        logger.warning(
            "mark_fork_failed after %s failed for %s",
            context,
            sanitize_log_value(branch),
            exc_info=True,
        )


async def _start_durable_chat_task(
    workspace: Any,
    *,
    objective: str,
    chat_id: str,
    session_id: str,
    compatibility_task_id: str,
    execution_contract: ExecutionContract | None = None,
) -> tuple[TaskService, Task]:
    """Create the durable task projection for a legacy chat task."""
    service = task_service_for_workspace(workspace)
    task = await service.create_task(
        objective=objective,
        agent_id=workspace.agent_id,
        source=TaskSource.API,
        acceptance_criteria=(
            execution_contract.acceptance
            if execution_contract is not None
            else ()
        ),
        execution_contract=execution_contract,
        metadata={
            "chat_id": chat_id,
            "session_id": session_id,
            "compatibility_task_id": compatibility_task_id,
        },
    )
    await service.plan_task(
        task.task_id,
        steps=(
            PlanStep(
                title="Agent run",
                objective=objective,
            ),
        ),
    )
    await service.start_task(
        task.task_id,
        runner_id="runner.local-agent",
    )
    return service, task


@router.post(
    "/chat/task",
    status_code=200,
    summary="Submit a background chat task",
)
# pylint: disable-next=too-many-statements
async def post_console_chat_task(
    request_data: dict,
    request: Request,
) -> dict:
    """Run an agent chat as a background task.

    Accepts a raw JSON object (not the shared ``AgentRequest`` model) so
    task-only fields such as ``timeout`` are not validated on the common
    chat envelope. ``timeout`` is resolved in-handler: omitted/null uses
    the server default; invalid values raise HTTP 400.

    Returns a ``task_id`` immediately. Poll status via
    ``GET /console/chat/task/{task_id}``.
    """
    workspace = await get_agent_for_request(request)
    console_channel = await workspace.channel_manager.get_channel("console")
    if console_channel is None:
        raise HTTPException(
            status_code=503,
            detail="Channel Console not found",
        )

    # Single validation path for task timeout — always HTTP 400 on error.
    try:
        effective_timeout = _resolve_effective_stream_task_timeout(
            request_data.get("timeout"),
        )
    except (ValueError, OverflowError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    native_payload = _extract_session_and_payload(request_data)
    task_id = f"task-{uuid.uuid4().hex[:12]}"
    session_id = console_channel.resolve_session_id(
        sender_id=native_payload["sender_id"],
        channel_meta=native_payload["meta"],
    )
    name, first_user_text = _extract_placeholder_name(
        native_payload["content_parts"],
    )
    chat = await workspace.chat_manager.get_or_create_chat(
        session_id,
        native_payload["sender_id"],
        native_payload["channel_id"],
        name=name,
        **_chat_registration_fields(native_payload),
    )
    chat = await _persist_pending_project_dirs(
        workspace,
        chat,
        native_payload,
    )
    await _claim_conversation_artifacts(
        workspace,
        chat.id,
        native_payload["content_parts"],
    )

    fork_project_dir = ""
    fork_worktree_branch = ""
    fork_scope_id = ""
    rc = request_data.get("request_context")
    if isinstance(rc, dict):
        fork_project_dir = str(rc.get("fork_project_dir") or "")
        fork_worktree_branch = str(
            rc.get("fork_worktree_branch") or "",
        )
        fork_scope_id = str(rc.get("fork_scope_id") or "")

    # Project directories are resolved exactly once, inside
    # ContextVarsSetupHook (fork override included); the router no
    # longer pre-resolves or injects them.

    bg = _BackgroundTask(
        status="running",
        started_at=time.time(),
    )
    timed_out = False
    producer_error: Exception | None = None
    producer_cancelled = False
    tracker = workspace.task_tracker

    objective = first_user_text or name
    raw_contract = request_data.get("execution_contract")
    try:
        execution_contract = (
            ExecutionContract.model_validate(raw_contract)
            if raw_contract is not None
            else None
        )
        if (
            execution_contract is not None
            and execution_contract.goal != objective
        ):
            raise ValueError(
                "execution contract goal must match chat objective",
            )
    except (ValidationError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid execution_contract: {exc}",
        ) from exc
    if execution_contract is not None:
        request_context = native_payload["meta"].setdefault(
            "request_context",
            {},
        )
        request_context["execution_contract"] = execution_contract.model_dump(
            mode="json",
        )
    task_service, durable_task = await _start_durable_chat_task(
        workspace,
        objective=objective,
        chat_id=chat.id,
        session_id=session_id,
        compatibility_task_id=task_id,
        execution_contract=execution_contract,
    )
    _bind_inherited_usage_scope(
        native_payload,
        agent_id=workspace.agent_id,
    )

    async def _tracked_stream(payload: dict) -> AsyncGenerator[str, None]:
        """Expose the background run to TaskTracker without hiding failures."""
        nonlocal producer_cancelled, producer_error
        try:
            async for sse_line in console_channel.stream_one(payload):
                yield sse_line
        except asyncio.CancelledError:
            producer_cancelled = True
            raise
        except Exception as exc:
            producer_error = exc
            raise

    try:
        queue, is_new_run = await tracker.attach_or_start(
            chat.id,
            native_payload,
            _tracked_stream,
            owner=workspace,
            on_finished=workspace.chat_manager.mark_chat_finished,
        )
    except BaseException:
        _release_bound_usage_scope(native_payload)
        raise
    if not is_new_run:
        _release_bound_usage_scope(native_payload)
        await tracker.detach_subscriber(chat.id, queue)
        await task_service.cancel_task(durable_task.task_id)
        raise HTTPException(
            status_code=409,
            detail=(
                "A task is already running for this chat. Wait for it to "
                "finish or use a different session_id."
            ),
        )

    # pylint: disable-next=too-many-branches
    async def _run() -> None:
        last_response: Optional[Dict[str, Any]] = None
        finalize_started = False
        try:
            async for sse_line in tracker.stream_from_queue(queue, chat.id):
                parsed = _parse_sse_payload(sse_line)
                if parsed and parsed.get("type") != "turn_usage":
                    last_response = parsed

            # ``stream_from_queue`` intentionally consumes cancellation so an
            # aborted SSE client does not leak it.  This background consumer,
            # however, owns the tracked run and must preserve task
            # cancellation.
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                raise asyncio.CancelledError

            if producer_cancelled:
                raise asyncio.CancelledError
            if producer_error is not None:
                raise producer_error

            # Fork subagents: commit dirty worktree so branch tips are
            # mergeable before exposing a completed task result.
            if fork_project_dir and fork_worktree_branch:
                finalize_started = True
                try:
                    finalized = await _finalize_background_fork(
                        fork_project_dir,
                        fork_worktree_branch,
                        scope_id=fork_scope_id,
                    )
                except Exception:
                    logger.warning(
                        "Background fork finalize failed for %s (%s)",
                        sanitize_log_value(fork_worktree_branch),
                        sanitize_log_value(fork_project_dir),
                        exc_info=True,
                    )
                    await _mark_background_fork_failed(
                        fork_project_dir,
                        fork_worktree_branch,
                        scope_id=fork_scope_id,
                        reason="Fork finalization raised an exception",
                        context="finalize error",
                    )
                    finalized = False
                if not finalized:
                    await task_service.fail_task(
                        durable_task.task_id,
                        error_summary="ForkFinalizationError",
                    )
                    _finish_background_task_with_error(
                        bg,
                        {"message": "Failed to finalize fork worktree"},
                    )
                    return
        except asyncio.CancelledError:
            cancel_error = _background_task_cancel_error(
                timed_out=timed_out,
                timeout_seconds=effective_timeout,
            )
            _finish_background_task_with_error(bg, cancel_error)
            if is_new_run:
                await tracker.request_stop(chat.id)
            await asyncio.shield(
                task_service.cancel_task(durable_task.task_id),
            )
            # In-flight Git finalize is detached bookkeeping; do not race
            # it with mark_fork_failed or let it flip this result later.
            if not finalize_started:
                await _mark_background_fork_failed(
                    fork_project_dir,
                    fork_worktree_branch,
                    scope_id=fork_scope_id,
                    reason=str(cancel_error["message"]),
                    context="cancel",
                )
            return
        except Exception as exc:
            _finish_background_task_with_error(
                bg,
                {"message": str(exc)},
            )
            await task_service.fail_task(
                durable_task.task_id,
                error_summary=type(exc).__name__,
            )
            await _mark_background_fork_failed(
                fork_project_dir,
                fork_worktree_branch,
                scope_id=fork_scope_id,
                reason=str(exc),
                context="task error",
            )
            return

        bg.status = "finished"
        bg.finished_at = time.time()
        await task_service.complete_task(durable_task.task_id)
        if last_response is not None:
            bg.result = {
                "status": "completed",
                "session_id": session_id,
                **last_response,
            }
        else:
            bg.result = {
                "status": "completed",
                "session_id": session_id,
                "output": [],
            }

    atask = asyncio.create_task(_run())
    bg.asyncio_task = atask

    async def _timeout_guard() -> None:
        nonlocal timed_out
        try:
            await asyncio.sleep(effective_timeout)
        except asyncio.CancelledError:
            return
        if not atask.done():
            timed_out = True
            atask.cancel()

    guard_task = asyncio.create_task(_timeout_guard())

    def _stop_timeout_guard(_task: asyncio.Task) -> None:
        if not guard_task.done():
            guard_task.cancel()

    atask.add_done_callback(_stop_timeout_guard)

    async with _bg_lock:
        _bg_tasks[task_id] = bg

    return {"task_id": task_id, "timeout": effective_timeout}


@router.get(
    "/chat/task/{task_id}",
    status_code=200,
    summary="Check background chat task status",
)
async def get_console_chat_task(task_id: str) -> dict:
    """Return the current status of a background chat task."""
    async with _bg_lock:
        bg = _bg_tasks.get(task_id)
    if bg is None:
        raise HTTPException(
            status_code=404,
            detail=f"Task not found: {task_id}",
        )
    response: Dict[str, Any] = {"status": bg.status}
    if bg.started_at is not None:
        response["started_at"] = bg.started_at
    if bg.status == "finished" and bg.result is not None:
        response["result"] = bg.result
    return response
