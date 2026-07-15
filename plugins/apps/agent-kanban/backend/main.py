# -*- coding: utf-8 -*-
"""Agent Kanban — PawApp backend.

A Kanban board where issues are created, assigned to agents, and
auto-run through the assigned agent via ``ctx.chat``. Agents can query
their own assigned issues through the ``list_my_kanban_issues`` tool.

Persistence: a shared JSON file (``<app_dir>/data/issues.json``) so both
the HTTP API and the agent tool read/write the exact same data.
"""
import asyncio
import json
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
from qwenpaw.pawapp.context import ChatReply
from qwenpaw.pawapp.task import SSEChannel

# ── Storage (shared by HTTP routes and agent tools) ──────────────────
_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_DATA_FILE = _DATA_DIR / "issues.json"
_LOCK_FILE = _DATA_DIR / ".issues.lock"
_LOCK = asyncio.Lock()
# issue_id -> the in-flight agent run task, so /stop can cancel it.
_RUNNING: Dict[str, "asyncio.Task"] = {}

# Per-issue realtime SSE channels for streaming agent output to the UI.
_CHANNELS: Dict[str, "SSEChannel"] = {}

VALID_STATUS = ["backlog", "todo", "in_progress", "review", "done"]
STATUS_LABEL = {
    "backlog": "待规划",
    "todo": "待办",
    "in_progress": "进行中",
    "review": "审核中",
    "done": "已完成",
}


def _read_all() -> List[Dict[str, Any]]:
    """Load the board.

    Returns ``[]`` only when the file genuinely does not exist. If the file
    exists but cannot be parsed (e.g. a partial read racing another
    process' write), retry briefly and then raise — never silently return
    ``[]``, because callers append to the result and would otherwise
    overwrite the whole board with a single issue.
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
    raise RuntimeError(f"issues.json unreadable: {last_err}")


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
        except Exception:
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
    return {"issues": _read_all()}


@router.post("/issues")
async def create_issue(body: IssueCreate) -> Dict[str, Any]:
    """Create a new issue in the given (or backlog) column."""
    status = body.status if body.status in VALID_STATUS else "backlog"
    async with _txn():
        issues = _read_all()
        issue = {
            "id": uuid.uuid4().hex[:8],
            "title": body.title.strip() or "Untitled",
            "description": body.description,
            "status": status,
            "assignee": body.assignee,
            "result": "",
            "log": [],
            "created_at": _now(),
            "updated_at": _now(),
        }
        issues.append(issue)
        _write_all(issues)
    return issue


@router.patch("/issues/{issue_id:path}")
async def patch_issue(
    issue_id: str,
    body: IssuePatch,
) -> Dict[str, Any]:
    """Update an issue's title/description/status/assignee."""
    async with _txn():
        issues = _read_all()
        issue = _find(issues, issue_id)
        if issue is None:
            raise HTTPException(status_code=404, detail="Issue not found")
        if body.title is not None:
            issue["title"] = body.title
        if body.description is not None:
            issue["description"] = body.description
        if body.status is not None:
            if body.status not in VALID_STATUS:
                raise HTTPException(status_code=400, detail="Invalid status")
            issue["status"] = body.status
        if body.assignee is not None:
            issue["assignee"] = body.assignee
        issue["updated_at"] = _now()
        _write_all(issues)
    return _find(_read_all(), issue_id) or {"ok": True}


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


async def _execute_run(ctx: Any, issue_id: str, prompt: str) -> None:
    """Background worker: run the agent and persist the outcome.

    Detached from the HTTP request so ``/stop`` can cancel it. On
    ``CancelledError`` (user pressed stop) the status/log is handled by
    the stop handler, so we re-raise without writing a result.
    """
    result_text = ""
    error = ""
    ch = _CHANNELS.get(issue_id)
    try:
        # Stream the agent output so the UI can render it in realtime: push
        # each text delta over the SSE channel while accumulating the full
        # reply (ChatReply handles both delta and final-response chunks).
        chunks: List[Any] = []
        async for ev in ctx.chat_stream(prompt):
            chunks.append(ev)
            if getattr(ev, "delta", False):
                delta = getattr(ev, "text", None)
                if delta and ch is not None:
                    await ch.send_event({"type": "delta", "text": str(delta)})
        result_text = ChatReply(chunks).text
    except asyncio.CancelledError:  # pylint: disable=try-except-raise
        raise
    except Exception as e:  # noqa: BLE001
        error = str(e)

    async with _txn():
        issues = _read_all()
        issue = _find(issues, issue_id)
        if issue is None:
            if ch is not None:
                ch.close()
            return
        if error:
            issue["status"] = "todo"
            issue["result"] = f"执行失败: {error}"
            issue.setdefault("log", []).append(
                {"ts": _now(), "event": "run_failed", "error": error},
            )
        else:
            issue["status"] = "review"
            issue["result"] = result_text or "(agent 未返回文本)"
            issue.setdefault("log", []).append(
                {"ts": _now(), "event": "run_completed"},
            )
        issue["updated_at"] = _now()
        _write_all(issues)

    # Signal the realtime stream that the run finished, then close it.
    if ch is not None:
        if error:
            await ch.send_event({"type": "error", "message": error})
        else:
            await ch.send_event({"type": "done", "text": result_text or ""})
        ch.close()


@router.post("/issues/{issue_id:path}/run")
async def run_issue(issue_id: str, ctx=Depends(get_ctx)) -> Dict[str, Any]:
    """Dispatch the issue to its assigned agent via ctx.chat (non-blocking).

    The agent runs in a background task (tracked in ``_RUNNING``) so the
    request returns immediately with ``in_progress``; pollers see the
    running state and a ``/stop`` call can cancel the task.
    """
    async with _txn():
        issues = _read_all()
        issue = _find(issues, issue_id)
        if issue is None:
            raise HTTPException(status_code=404, detail="Issue not found")
        assignee = issue.get("assignee") or ctx.agent_id or "default"
        # Mark in-progress first so pollers see the running state.
        issue["status"] = "in_progress"
        issue["updated_at"] = _now()
        issue.setdefault("log", []).append(
            {"ts": _now(), "event": "run_started", "agent": assignee},
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

    # Fresh realtime channel for this run (replaces any stale one). Created
    # before the task starts so early deltas are buffered until the UI
    # connects.
    old_ch = _CHANNELS.pop(issue_id, None)
    if old_ch is not None:
        old_ch.close()
    _CHANNELS[issue_id] = SSEChannel()

    # Cancel any stale task for the same issue, then launch a fresh one.
    old = _RUNNING.pop(issue_id, None)
    if old is not None and not old.done():
        old.cancel()
    task = asyncio.create_task(_execute_run(ctx, issue_id, prompt))
    _RUNNING[issue_id] = task

    def _cleanup(t: "asyncio.Task", _id: str = issue_id) -> None:
        if _RUNNING.get(_id) is t:
            _RUNNING.pop(_id, None)

    task.add_done_callback(_cleanup)
    return _find(_read_all(), issue_id) or {
        "ok": True,
        "status": "in_progress",
    }


@router.get("/issues/{issue_id:path}/stream")
async def stream_issue(issue_id: str) -> StreamingResponse:
    """SSE stream of the agent's realtime output for an issue.

    The channel is created by ``run_issue`` before the background task starts,
    so deltas emitted before the browser connects are buffered and replayed.
    """
    ch = _CHANNELS.get(issue_id)
    if ch is None:
        ch = SSEChannel()
        _CHANNELS[issue_id] = ch

    async def _gen():
        try:
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
    async with _txn():
        issues = _read_all()
        issue = _find(issues, issue_id)
        if issue is None:
            raise HTTPException(status_code=404, detail="Issue not found")
        if issue.get("status") == "in_progress":
            issue["status"] = "todo"
            issue["updated_at"] = _now()
            issue.setdefault("log", []).append(
                {"ts": _now(), "event": "run_stopped"},
            )
            _write_all(issues)
    return _find(_read_all(), issue_id) or {"ok": True}


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
