# -*- coding: utf-8 -*-
"""Agent Kanban — PawApp backend.

A Kanban board where issues are created, assigned to agents, and
auto-run through the assigned agent via ``ctx.chat``. Agents can query
their own assigned issues through the ``list_my_kanban_issues`` tool.

Persistence: a shared JSON file (``<app_dir>/data/issues.json``) so both
the HTTP API and the agent tool read/write the exact same data.
"""
import asyncio
import dataclasses
import json
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path

try:  # POSIX advisory file locking (macOS/Linux) for cross-process safety.
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback (single-process).
    fcntl = None
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from qwenpaw.pawapp import PawApp, get_ctx
from qwenpaw.pawapp.task import SSEChannel

logger = logging.getLogger(__name__)

# ── Storage (shared by HTTP routes and agent tools) ──────────────────
_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_DATA_FILE = _DATA_DIR / "issues.json"
_LOCK_FILE = _DATA_DIR / ".issues.lock"
_LOCK = asyncio.Lock()
# issue_id -> the in-flight agent run task, so /stop can cancel it.
_RUNNING: Dict[str, "asyncio.Task"] = {}

# Per-issue realtime SSE channels for streaming agent output to the UI.
_CHANNELS: Dict[str, "SSEChannel"] = {}

# In-memory trace buffer for running issues (written to disk on finish).
_LIVE_TRACE: Dict[str, List[Dict[str, str]]] = {}

VALID_STATUS = ["backlog", "todo", "in_progress", "review", "done"]
STATUS_LABEL = {
    "backlog": "待规划",
    "todo": "等待调度",
    "in_progress": "进行中",
    "review": "审核中",
    "done": "已完成",
}


def _read_all() -> List[Dict[str, Any]]:
    """Load the board (synchronous).

    Returns ``[]`` only when the file genuinely does not exist. If the
    file exists but cannot be parsed (e.g. a partial read racing
    another process' write), retry briefly and then raise -- never
    silently return ``[]``, because callers append to the result and
    would otherwise overwrite the whole board with a single issue.

    NOTE: This is a synchronous function (uses ``time.sleep``).
    Async callers must use ``asyncio.to_thread(_read_all)`` to
    avoid blocking the event loop.
    """
    if not _DATA_FILE.exists():
        return []
    last_err: Optional[Exception] = None
    for _ in range(5):
        try:
            text = _DATA_FILE.read_text(encoding="utf-8")
            if not text.strip():
                return []
            return json.loads(text)
        except FileNotFoundError:
            return []
        except (json.JSONDecodeError, ValueError) as e:  # noqa: PERF203
            last_err = e
            time.sleep(0.05)
    raise RuntimeError(
        f"issues.json unreadable: {last_err}",
    )


def _write_all(issues: List[Dict[str, Any]]) -> None:
    """Persist the board atomically (temp file + ``os.replace``).

    Atomic replace guarantees every reader sees either the old or the new
    complete file — never a truncated one — even when other backend
    processes read concurrently.
    """
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(issues, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(
        dir=str(_DATA_DIR),
        prefix=".issues.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _DATA_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class _Txn:
    """Serialize a read-modify-write across coroutines AND processes.

    ``_LOCK`` guards coroutines in this event loop; the ``fcntl`` advisory
    lock on ``.issues.lock`` guards other ``qwenpaw app`` processes sharing
    the same app directory (all read/write the same ``data/issues.json``).
    Without it, concurrent writes lose updates.
    """

    def __init__(self) -> None:
        self._fh = None

    async def __aenter__(self) -> "_Txn":
        await _LOCK.acquire()
        if fcntl is None:
            return self
        try:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            self._fh = open(  # pylint: disable=consider-using-with
                _LOCK_FILE,
                "a+",
                encoding="utf-8",
            )
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except BaseException:
            if self._fh is not None:
                self._fh.close()
                self._fh = None
            _LOCK.release()
            raise
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        try:
            if fcntl is not None and self._fh is not None:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                self._fh.close()
                self._fh = None
        finally:
            _LOCK.release()
        return False


def _txn() -> "_Txn":
    """Cross-process + cross-coroutine lock for read-modify-write blocks."""
    return _Txn()


def _now() -> float:
    return time.time()


def _find(
    issues: List[Dict[str, Any]],
    issue_id: str,
) -> Optional[Dict[str, Any]]:
    for i in issues:
        if i.get("id") == issue_id:
            return i
    return None


def _agent_has_running(
    agent_id: str,
    issues: List[Dict[str, Any]],
) -> bool:
    """Check whether *agent_id* owns an in_progress issue."""
    for iss in issues:
        if (
            iss.get("assignee") == agent_id
            and iss.get("status") == "in_progress"
        ):
            iid = iss.get("id", "")
            task = _RUNNING.get(iid)
            if task is not None and not task.done():
                return True
    return False


# ── Schemas ──────────────────────────────────────────────────────────
class IssueCreate(BaseModel):
    title: str
    description: str = ""
    status: str = "backlog"
    assignee: str = ""


class IssuePatch(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    assignee: Optional[str] = None


# ── HTTP router ──────────────────────────────────────────────────────
router = APIRouter()


@router.get("/issues")
async def list_issues() -> Dict[str, Any]:
    """List every issue on the board."""
    issues = await asyncio.to_thread(_read_all)
    return {"issues": issues}


@router.post("/issues")
async def create_issue(
    body: IssueCreate,
    ctx=Depends(get_ctx),
) -> Dict[str, Any]:
    """Create a new issue.

    Creating directly into ``todo`` requires a non-empty assignee.
    If the assigned agent is idle, auto-dispatches immediately.
    """
    status = body.status if body.status in VALID_STATUS else "backlog"
    assignee = body.assignee or ""

    if status == "todo" and not assignee:
        raise HTTPException(
            status_code=400,
            detail="Creating in todo requires an assignee",
        )

    trigger_agent: Optional[str] = None
    async with _txn():
        issues = _read_all()
        issue = {
            "id": uuid.uuid4().hex[:8],
            "title": body.title.strip() or "Untitled",
            "description": body.description,
            "status": status,
            "assignee": assignee,
            "result": "",
            "log": [],
            "created_at": _now(),
            "updated_at": _now(),
        }
        issues.append(issue)
        _write_all(issues)

        if (
            status == "todo"
            and assignee
            and not _agent_has_running(assignee, issues)
        ):
            trigger_agent = assignee

    if trigger_agent:
        _LAST_CTX.setdefault(trigger_agent, ctx)
        asyncio.get_event_loop().call_soon(
            lambda aid=trigger_agent: asyncio.ensure_future(
                _try_dispatch_next(aid),
            ),
        )
    return issue


@router.patch("/issues/{issue_id:path}")
async def patch_issue(
    issue_id: str,
    body: IssuePatch,
    ctx=Depends(get_ctx),
) -> Dict[str, Any]:
    """Update an issue's title/description/status/assignee.

    Rules enforced:
    - Moving to ``todo`` requires a non-empty assignee.
    - Setting an assignee on a ``backlog`` issue auto-promotes
      it to ``todo``; if that agent is idle the issue is
      auto-dispatched to ``in_progress``.
    """
    trigger_agent: Optional[str] = None
    async with _txn():
        issues = _read_all()
        issue = _find(issues, issue_id)
        if issue is None:
            raise HTTPException(
                status_code=404,
                detail="Issue not found",
            )
        if body.title is not None:
            issue["title"] = body.title
        if body.description is not None:
            issue["description"] = body.description

        # Apply assignee first so the status check below sees it.
        old_assignee = issue.get("assignee") or ""
        if body.assignee is not None:
            issue["assignee"] = body.assignee

        new_assignee = issue.get("assignee") or ""

        if body.status is not None:
            if body.status not in VALID_STATUS:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid status",
                )
            if body.status == "todo" and not new_assignee:
                raise HTTPException(
                    status_code=400,
                    detail="Moving to todo requires an assignee",
                )
            issue["status"] = body.status
            if body.status == "backlog":
                issue["result"] = ""
                _LIVE_TRACE.pop(issue_id, None)

        # Auto-promote: assigning an agent to a backlog issue
        # moves it to todo automatically.
        if (
            not old_assignee
            and new_assignee
            and issue.get("status") == "backlog"
        ):
            issue["status"] = "todo"
            issue.setdefault("log", []).append(
                {
                    "ts": _now(),
                    "event": "auto_promoted_to_todo",
                },
            )

        # If the issue just became todo with an assignee and
        # that agent is idle, schedule auto-dispatch after the
        # txn releases the lock.
        if (
            issue.get("status") == "todo"
            and new_assignee
            and not _agent_has_running(new_assignee, issues)
        ):
            trigger_agent = new_assignee

        issue["updated_at"] = _now()
        _write_all(issues)

    if trigger_agent:
        _LAST_CTX.setdefault(trigger_agent, ctx)
        asyncio.get_event_loop().call_soon(
            lambda aid=trigger_agent: asyncio.ensure_future(
                _try_dispatch_next(aid),
            ),
        )
    return issue


@router.delete("/issues/{issue_id:path}")
async def delete_issue(issue_id: str) -> Dict[str, Any]:
    """Remove an issue from the board."""
    async with _txn():
        issues = _read_all()
        if _find(issues, issue_id) is None:
            raise HTTPException(status_code=404, detail="Issue not found")
        issues = [i for i in issues if i.get("id") != issue_id]
        _write_all(issues)
    return {"ok": True}


async def _execute_run(
    ctx: Any,
    issue_id: str,
    prompt: str,
    assignee: str = "",
) -> None:
    """Background worker: run the agent and persist the outcome.

    Detached from the HTTP request so ``/stop`` can cancel it.
    On ``CancelledError`` (user pressed stop) the status/log is
    handled by the stop handler, so we re-raise without writing
    a result.

    After completion (success or failure), auto-dispatches the
    next queued issue for the same *assignee* via
    ``_try_dispatch_next``.
    """
    # Shallow-copy ctx so we don't mutate the cached _LAST_CTX
    # instance, and set agent_id to the assignee so the correct
    # tool execution policy (approval_level) is applied.
    if assignee:
        ctx = dataclasses.replace(ctx, agent_id=assignee)

    result_text = ""
    last_ev: Any = None
    error = ""
    _LIVE_TRACE[issue_id] = []
    trace = _LIVE_TRACE[issue_id]
    _seen_tool_ids: set = set()

    def _ch() -> Any:
        return _CHANNELS.get(issue_id)

    try:
        chunks: List[Any] = []
        logger.info(
            "[kanban] _execute_run started for %s (agent=%s)",
            issue_id, assignee,
        )
        async for ev in ctx.chat_stream(
            prompt,
            session_id=issue_id,
        ):
            logger.debug(
                "[kanban] ev type=%s delta=%s",
                getattr(ev, "type", "?"),
                getattr(ev, "delta", "?"),
            )
            # 1) Text delta (stream only, not stored in trace)
            if getattr(ev, "delta", False):
                delta = getattr(ev, "text", None)
                if delta:
                    ch = _ch()
                    if ch is not None:
                        await ch.send_event(
                            {"type": "delta", "text": str(delta)},
                        )
                continue
            chunks.append(ev)
            # 2) Tool call / output (Message with type
            #    plugin_call / plugin_call_output / function_call
            #    / function_call_output).
            raw_type = getattr(ev, "type", None)
            if hasattr(raw_type, "value"):
                raw_type = raw_type.value
            msg_type = str(raw_type) if raw_type else ""
            if not any(
                k in msg_type
                for k in (
                    "plugin_call",
                    "function_call",
                    "mcp_tool",
                )
            ):
                continue
            action = (
                "output" if "output" in msg_type else "call"
            )
            # Dedup: only record once per message id + action.
            ev_id = getattr(ev, "id", None) or ""
            dedup_key = f"{ev_id}:{action}"
            if dedup_key in _seen_tool_ids:
                continue
            _seen_tool_ids.add(dedup_key)
            # Extract tool name from content[0].data.name
            name = ""
            content = getattr(ev, "content", None)
            if isinstance(content, list):
                for blk in content:
                    data = getattr(blk, "data", None)
                    if data is None:
                        continue
                    if isinstance(data, dict):
                        name = data.get("name", "")
                    else:
                        name = getattr(data, "name", "") or ""
                    if name:
                        break
            if not name:
                name = getattr(ev, "name", "") or ""
            if name:
                trace.append(
                    {"t": "tool", "a": action, "n": name},
                )
                ch = _ch()
                if ch is not None:
                    await ch.send_event(
                        {
                            "type": "tool",
                            "action": action,
                            "name": name,
                        },
                    )
        result_text = ChatReply(chunks).text
        logger.info(
            "[kanban] stream done for %s: %d chunks, "
            "%d trace entries, result=%d chars",
            issue_id, len(chunks), len(trace),
            len(result_text),
        )
    except asyncio.CancelledError:  # pylint: disable=try-except-raise
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("[kanban] _execute_run error for %s", issue_id)
        error = str(e)

    async with _txn():
        issues = _read_all()
        issue = _find(issues, issue_id)
        if issue is None:
            ch = _ch()
            if ch is not None:
                ch.close()
            return
        _LIVE_TRACE.pop(issue_id, None)
        if error:
            issue["status"] = "todo"
            issue["result"] = f"执行失败: {error}"
            issue.setdefault("log", []).append(
                {
                    "ts": _now(),
                    "event": "run_failed",
                    "error": error,
                },
            )
        else:
            issue["status"] = "review"
            issue["result"] = result_text or "(agent 未返回文本)"
            issue.setdefault("log", []).append(
                {"ts": _now(), "event": "run_completed"},
            )
        issue["updated_at"] = _now()
        _write_all(issues)

    ch = _ch()
    if ch is not None:
        if error:
            await ch.send_event(
                {"type": "error", "message": error},
            )
        else:
            await ch.send_event(
                {"type": "done", "text": result_text or ""},
            )
        ch.close()

    # Auto-dispatch the next queued issue for this agent.
    if assignee:
        await _try_dispatch_next(assignee)


def _launch_run(
    ctx: Any,
    issue_id: str,
    prompt: str,
    assignee: str,
) -> "asyncio.Task":
    """Create SSE channel + background task for a single run."""
    old_ch = _CHANNELS.pop(issue_id, None)
    if old_ch is not None:
        old_ch.close()
    _CHANNELS[issue_id] = SSEChannel()

    old = _RUNNING.pop(issue_id, None)
    if old is not None and not old.done():
        old.cancel()

    task = asyncio.create_task(
        _execute_run(ctx, issue_id, prompt, assignee),
    )
    _RUNNING[issue_id] = task

    def _cleanup(
        t: "asyncio.Task",
        _id: str = issue_id,
    ) -> None:
        if _RUNNING.get(_id) is t:
            _RUNNING.pop(_id, None)

    task.add_done_callback(_cleanup)
    return task


# Stored reference to ctx from the most recent run so that
# _try_dispatch_next can create a fresh run without an HTTP
# request.  Safe because PawAppContext is lightweight and
# the workspace registry it holds is a long-lived singleton.
_LAST_CTX: Dict[str, Any] = {}


async def _try_dispatch_next(
    agent_id: str,
    exclude_id: str = "",
) -> None:
    """Pick the oldest todo issue for *agent_id* and run it.

    Called after a run finishes or is stopped.  Does nothing if
    the agent already has an in_progress task or no todo issues.
    *exclude_id* skips a just-stopped issue so it is not
    immediately re-dispatched.
    """
    ctx = _LAST_CTX.get(agent_id)
    if ctx is None:
        logger.debug(
            "No cached ctx for agent %s; skip auto-dispatch",
            agent_id,
        )
        return

    async with _txn():
        issues = _read_all()
        if _agent_has_running(agent_id, issues):
            return
        candidate = None
        for iss in issues:
            if (
                iss.get("assignee") == agent_id
                and iss.get("status") == "todo"
                and iss.get("id") != exclude_id
            ):
                candidate = iss
                break
        if candidate is None:
            return
        candidate["status"] = "in_progress"
        candidate["result"] = ""
        candidate["updated_at"] = _now()
        candidate.setdefault("log", []).append(
            {
                "ts": _now(),
                "event": "auto_dispatched",
                "agent": agent_id,
            },
        )
        _write_all(issues)
        iid = candidate["id"]
        title = candidate["title"]
        desc = candidate.get("description") or "(无描述)"

    prompt = (
        "你被指派处理以下看板任务(Issue):\n"
        f"标题: {title}\n"
        f"描述: {desc}\n\n"
        "请完成该任务，并用简洁的中文汇报你的处理结果。"
    )
    logger.info("Auto-dispatching issue %s to agent %s", iid, agent_id)
    _launch_run(ctx, iid, prompt, agent_id)


@router.post("/issues/{issue_id:path}/run")
async def run_issue(
    issue_id: str,
    ctx=Depends(get_ctx),
) -> Dict[str, Any]:
    """Dispatch the issue to its assigned agent (non-blocking).

    If the agent is busy, the issue is queued as ``todo`` and will
    be auto-dispatched when the agent becomes idle.
    """
    async with _txn():
        issues = _read_all()
        issue = _find(issues, issue_id)
        if issue is None:
            raise HTTPException(
                status_code=404,
                detail="Issue not found",
            )
        assignee = issue.get("assignee") or ctx.agent_id or "default"

        if _agent_has_running(assignee, issues):
            issue["status"] = "todo"
            issue["updated_at"] = _now()
            issue.setdefault("log", []).append(
                {
                    "ts": _now(),
                    "event": "queued",
                    "agent": assignee,
                },
            )
            _write_all(issues)
            _LAST_CTX.setdefault(assignee, ctx)
            return issue

        issue["status"] = "in_progress"
        issue["result"] = ""
        issue["updated_at"] = _now()
        issue.setdefault("log", []).append(
            {
                "ts": _now(),
                "event": "run_started",
                "agent": assignee,
            },
        )
        _write_all(issues)
        title = issue["title"]
        description = issue.get("description") or "(无描述)"

    prompt = (
        "你被指派处理以下看板任务(Issue):\n"
        f"标题: {title}\n"
        f"描述: {description}\n\n"
        "请完成该任务，并用简洁的中文汇报你的处理结果。"
    )

    _LAST_CTX[assignee] = ctx
    _launch_run(ctx, issue_id, prompt, assignee)
    return issue


@router.get("/issues/{issue_id:path}/stream")
async def stream_issue(issue_id: str) -> StreamingResponse:
    """SSE stream of the agent's realtime output for an issue.

    The channel is created by ``run_issue`` before the background task starts,
    so deltas emitted before the browser connects are buffered and replayed.
    """
    # Replay in-memory trace so late-joining clients see history.
    replay: List[Dict[str, Any]] = []
    live = _LIVE_TRACE.get(issue_id)
    if live:
        for entry in list(live):
            if entry.get("t") == "d":
                replay.append(
                    {"type": "delta", "text": entry.get("v", "")},
                )
            elif entry.get("t") == "tool":
                replay.append(
                    {
                        "type": "tool",
                        "action": entry.get("a", "call"),
                        "name": entry.get("n", ""),
                    },
                )

    ch = _CHANNELS.get(issue_id)
    if ch is None:
        ch = SSEChannel()
        _CHANNELS[issue_id] = ch

    async def _gen():
        try:
            for evt in replay:
                yield (
                    f"data: {json.dumps(evt, ensure_ascii=False)}"
                    "\n\n"
                )
            async for chunk in ch:
                yield chunk
        finally:
            if _CHANNELS.get(issue_id) is ch:
                _CHANNELS.pop(issue_id, None)

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/issues/{issue_id:path}/stop")
async def stop_issue(issue_id: str) -> Dict[str, Any]:
    """Stop a running issue: cancel its background task and reset status."""
    task = _RUNNING.pop(issue_id, None)
    if task is not None and not task.done():
        task.cancel()
    ch = _CHANNELS.pop(issue_id, None)
    if ch is not None:
        ch.close()
    agent_id: Optional[str] = None
    async with _txn():
        issues = _read_all()
        issue = _find(issues, issue_id)
        if issue is None:
            raise HTTPException(
                status_code=404,
                detail="Issue not found",
            )
        if issue.get("status") == "in_progress":
            issue["status"] = "backlog"
            issue["result"] = ""
            issue["updated_at"] = _now()
            issue.setdefault("log", []).append(
                {"ts": _now(), "event": "run_stopped"},
            )
            _write_all(issues)
            _LIVE_TRACE.pop(issue_id, None)
            agent_id = issue.get("assignee")

    if agent_id:
        await _try_dispatch_next(agent_id)
    return issue


@router.get("/approvals")
async def list_kanban_approvals() -> Dict[str, Any]:
    """Return pending approvals for all in_progress kanban issues.

    Queries the global ApprovalService for approvals whose
    ``session_id`` matches ``pawapp:agent-kanban:<issue_id>``.
    """
    try:
        from qwenpaw.app.approvals import get_approval_service
        from qwenpaw.app.approvals.display import (
            approval_display_fields,
        )
    except ImportError:
        return {"approvals": {}}

    svc = get_approval_service()
    issues = await asyncio.to_thread(_read_all)
    running_ids = {
        iss["id"] for iss in issues if iss.get("status") == "in_progress"
    }
    if not running_ids:
        return {"approvals": {}}

    # pylint: disable=protected-access
    result: Dict[str, List[Dict[str, Any]]] = {}
    async with svc._lock:
        for p in svc._pending.values():
            if p.status != "pending":
                continue
            sid = p.session_id or ""
            for iid in running_ids:
                if sid == iid or sid.endswith(f":{iid}"):
                    result.setdefault(iid, []).append(
                        {
                            "request_id": p.request_id,
                            "session_id": p.session_id,
                            "root_session_id": (p.root_session_id),
                            "tool_name": p.tool_name,
                            "agent_id": p.agent_id,
                            "severity": p.severity,
                            **approval_display_fields(p),
                            "created_at": p.created_at,
                        },
                    )
    return {"approvals": result}


@router.post("/approvals/{request_id}/approve")
async def approve_kanban(request_id: str) -> Dict[str, Any]:
    """Approve a pending tool execution from the kanban UI.

    Looks up the pending approval by *request_id* and resolves
    it using its own ``root_session_id``, so the frontend does
    not need to know the session topology.
    """
    try:
        from qwenpaw.app.approvals import get_approval_service
        from qwenpaw.security.tool_guard.approval import (
            ApprovalDecision,
        )
    except ImportError:
        raise HTTPException(
            status_code=501,
            detail="Approval service unavailable",
        )

    svc = get_approval_service()
    pending = await svc.get_request(request_id)
    if pending is None:
        raise HTTPException(
            status_code=404,
            detail="Approval request not found",
        )
    await svc.resolve_request(
        request_id,
        ApprovalDecision.APPROVED,
    )
    return {"ok": True, "tool_name": pending.tool_name}


@router.post("/approvals/{request_id}/deny")
async def deny_kanban(request_id: str) -> Dict[str, Any]:
    """Deny a pending tool execution from the kanban UI."""
    try:
        from qwenpaw.app.approvals import get_approval_service
        from qwenpaw.security.tool_guard.approval import (
            ApprovalDecision,
        )
    except ImportError:
        raise HTTPException(
            status_code=501,
            detail="Approval service unavailable",
        )

    svc = get_approval_service()
    pending = await svc.get_request(request_id)
    if pending is None:
        raise HTTPException(
            status_code=404,
            detail="Approval request not found",
        )
    await svc.resolve_request(
        request_id,
        ApprovalDecision.DENIED,
    )
    return {"ok": True, "tool_name": pending.tool_name}


@router.get("/queue/{agent_id:path}")
async def get_agent_queue(agent_id: str) -> Dict[str, Any]:
    """Return the todo queue and running status for *agent_id*."""
    issues = await asyncio.to_thread(_read_all)
    running = None
    queue: List[Dict[str, Any]] = []
    for iss in issues:
        if iss.get("assignee") != agent_id:
            continue
        if iss.get("status") == "in_progress":
            running = {
                "id": iss["id"],
                "title": iss.get("title", ""),
            }
        elif iss.get("status") == "todo":
            queue.append(
                {
                    "id": iss["id"],
                    "title": iss.get("title", ""),
                },
            )
    return {
        "agent_id": agent_id,
        "running": running,
        "queue": queue,
    }


# ── PawApp definition + agent-facing tools ───────────────────────────
app = PawApp(name="Agent Kanban", app_id="agent-kanban")
app.include_router(router)


@app.tool(
    "list_my_kanban_issues",
    description="列出 Agent Kanban 看板中指派给当前 agent 的任务(issues)，含标题、状态与描述。",
    icon="📋",
)
async def list_my_kanban_issues() -> str:
    """Return the Kanban issues assigned to the calling agent."""
    try:
        from qwenpaw.app.agent_context import get_current_agent_id

        agent_id = get_current_agent_id() or "default"
    except Exception:  # noqa: BLE001
        agent_id = "default"

    mine = [i for i in _read_all() if i.get("assignee") == agent_id]
    if not mine:
        return f"当前没有指派给你(agent={agent_id})的看板任务。"

    lines = [f"指派给你(agent={agent_id})的看板任务共 {len(mine)} 个:"]
    for i in mine:
        label = STATUS_LABEL.get(i.get("status"), i.get("status"))
        lines.append(
            f"- (#{i.get('id')}) [{label}] {i.get('title')} — "
            f"{i.get('description') or '无描述'}",
        )
    return "\n".join(lines)


@app.tool(
    "complete_kanban_issue",
    description="将指定的看板 issue 标记为已完成(done)。参数 issue_id 为 issue 的短 id。",
    icon="✅",
)
async def complete_kanban_issue(issue_id: str) -> str:
    """Mark a Kanban issue as done (callable by the agent)."""
    async with _txn():
        issues = _read_all()
        issue = _find(issues, issue_id)
        if issue is None:
            return f"未找到 issue {issue_id}。"
        issue["status"] = "done"
        issue["updated_at"] = _now()
        issue.setdefault("log", []).append(
            {"ts": _now(), "event": "completed_by_agent"},
        )
        _write_all(issues)
        return f"已将 issue #{issue_id}（{issue['title']}）标记为已完成。"


# The 'plugin' variable is what PluginLoader looks for.
plugin = app
