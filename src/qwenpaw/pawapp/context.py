# -*- coding: utf-8 -*-
"""PawAppContext — The ``ctx`` object that PawApp developers interact with.

Provides access to QwenPaw capabilities via thin delegation:
- ctx.chat() / ctx.chat_stream() → Workspace.stream_query()
- ctx.storage.get/set/search → SafeJSONSession (namespaced)
- ctx.tools.invoke() → ToolCoordinator
- ctx.file.read/write → LocalWorkspace + FileGuard
- ctx.notify() → ChannelManager
- ctx.ui.push/confirm → UIBridge (SSE + ApprovalService)
- ctx.settings.get() → PluginRegistry tool config
- ctx.toast() → frontend notification via bridge
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

logger = logging.getLogger(__name__)


# ─── Sub-objects (properties return these) ──────────────────────────


class AppStorage:
    """Namespaced KV storage for a PawApp."""

    def __init__(self, session: Any, namespace: str):
        self._session = session
        self._namespace = namespace

    async def get(self, key: str, *, default: Any = None) -> Any:
        """Get a value from app-namespaced storage."""
        try:
            state = await self._session.get_session_state_dict(
                session_id=self._namespace,
                allow_not_exist=True,
            )
            return state.get(key, default)
        except Exception:
            return default

    async def set(self, key: str, value: Any) -> None:
        """Set a value in app-namespaced storage."""
        await self._session.update_session_state(
            session_id=self._namespace,
            key=key,
            value=value,
            create_if_not_exist=True,
        )

    async def delete(self, key: str) -> None:
        """Delete a key from storage."""
        await self._session.update_session_state(
            session_id=self._namespace,
            key=key,
            value=None,
            create_if_not_exist=False,
        )

    async def keys(self) -> List[str]:
        """List all keys in this app's namespace."""
        try:
            state = await self._session.get_session_state_dict(
                session_id=self._namespace,
                allow_not_exist=True,
            )
            return list(state.keys())
        except Exception:
            return []

    async def clear_namespace(self) -> None:
        """Delete all data in this app's namespace."""
        try:
            await self._session.delete_session(session_id=self._namespace)
        except Exception:
            pass


class ToolProxy:
    """Proxy for invoking registered tools."""

    def __init__(self, tool_coordinator: Any):
        self._coordinator = tool_coordinator

    async def invoke(
        self,
        name: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Invoke a registered tool by name."""
        if self._coordinator is None:
            raise RuntimeError("ToolCoordinator not available")
        return await self._coordinator.execute(name, params or {})


class FileProxy:
    """Proxy for file operations.

    TODO: integrate with FileGuard for sandboxed access control.
    Currently performs raw filesystem I/O without permission checks.
    """

    async def read(self, path: str) -> str:
        """Read a file's content (no sandbox check yet)."""
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"File not found: {path}")
        return p.read_text(encoding="utf-8")

    async def write(self, path: str, data: str) -> None:
        """Write content to a file (no sandbox check yet)."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data, encoding="utf-8")


class UIBridge:
    """Agent→UI realtime communication via SSE."""

    def __init__(
        self,
        sse_channel: Any = None,
        approval_coordinator: Any = None,
    ):
        self._channel = sse_channel
        self._approval = approval_coordinator

    async def push(self, event_type: str, data: Any = None) -> None:
        """Non-blocking push: send event to frontend UI in realtime."""
        if self._channel is None:
            logger.warning("UIBridge.push called but no SSE channel available")
            return
        await self._channel.send_event(
            {
                "type": "pawapp:ui_event",
                "event": event_type,
                "data": data,
            },
        )

    async def confirm(
        self,
        message: str,
        *,
        data: Any = None,
        timeout: int = 300,
    ) -> Dict[str, Any]:
        """Blocking wait: pause until frontend user responds.

        Uses ApprovalService's asyncio.Future mechanism.
        """
        if self._channel is None or self._approval is None:
            raise RuntimeError("UIBridge not connected to SSE/Approval")

        import uuid

        request_id = str(uuid.uuid4())
        # Send confirm request to frontend via SSE
        await self._channel.send_event(
            {
                "type": "pawapp:confirm_request",
                "request_id": request_id,
                "message": message,
                "data": data,
            },
        )
        # Wait for user response (via approval service)
        try:
            decision = await self._approval.wait_for_approval(
                request_id,
                timeout,
            )
            return {"action": "approve", "data": decision}
        except Exception:
            return {"action": "timeout", "data": None}


class AppSettings:
    """Access app-specific configuration (from manifest settings)."""

    def __init__(
        self,
        plugin_registry: Any,
        app_id: str,
        agent_id: str = "default",
    ):
        self._registry = plugin_registry
        self._app_id = app_id
        self._agent_id = agent_id

    def get(self, key: str, *, default: Any = None) -> Any:
        """Get a setting value."""
        if self._registry is None:
            return default
        config = self._registry.get_tool_config(self._app_id, self._agent_id)
        if config:
            return config.get(key, default)
        return default


# ─── Main Context Class ─────────────────────────────────────────────


@dataclass
class PawAppContext:
    """The ``ctx`` object — PawApp developer's gateway to QwenPaw.

    Created per-request by ``get_ctx`` dependency injection.
    """

    app_id: str
    agent_id: str = "default"

    # Injected services (set by deps.py)
    _workspace_registry: Any = field(default=None, repr=False)
    _app_services: Any = field(default=None, repr=False)
    _plugin_registry: Any = field(default=None, repr=False)
    _session: Any = field(default=None, repr=False)
    _sse_channel: Any = field(default=None, repr=False)

    # ─── Chat ───────────────────────────────────────────────────────

    async def chat(
        self,
        message: str,
        *,
        skill: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Any:
        """Send a message to the Agent and get a reply.

        Delegates to Workspace.stream_query().
        ``session_id`` isolates the conversation; defaults to
        ``pawapp:{app_id}`` when omitted.
        """
        workspace = await self._get_workspace()
        if workspace is None:
            raise RuntimeError("No workspace available for chat")

        chunks: List[Any] = []
        async for event in self._stream_query(
            workspace,
            message,
            skill,
            session_id=session_id,
        ):
            chunks.append(event)

        return ChatReply(chunks=chunks)

    async def chat_stream(
        self,
        message: str,
        *,
        skill: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> AsyncIterator[Any]:
        """Stream chat responses (async generator).

        ``session_id`` isolates the conversation; defaults to
        ``pawapp:{app_id}`` when omitted.
        """
        workspace = await self._get_workspace()
        if workspace is None:
            raise RuntimeError(
                "No workspace available for chat_stream",
            )

        async for event in self._stream_query(
            workspace,
            message,
            skill,
            session_id=session_id,
        ):
            yield event

    async def _get_workspace(self) -> Any:
        """Get the workspace for the current agent."""
        if self._workspace_registry is None:
            return None
        try:
            return await self._workspace_registry.get_agent(self.agent_id)
        except Exception:
            return None

    async def _stream_query(
        self,
        workspace: Any,
        message: str,
        skill: Optional[str],
        *,
        session_id: Optional[str] = None,
    ) -> AsyncIterator[Any]:
        """Internal: delegate to workspace's stream_query.

        ``session_id`` overrides the default ``pawapp:{app_id}``
        session key, allowing callers to isolate conversations
        (e.g. per-issue in Kanban).
        """
        # pylint: disable=unused-argument
        if hasattr(workspace, "stream_query"):
            from ..schemas import AgentRequest

            sid = session_id or f"pawapp:{self.app_id}"
            request = AgentRequest(
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": message},
                        ],
                    },
                ],
                session_id=sid,
                user_id=self.agent_id or "default",
                agent_id=self.agent_id or "default",
            )
            async for event in workspace.stream_query(request):
                yield event
        else:
            # Fallback: try direct agent call
            logger.warning("Workspace has no stream_query; using fallback")
            yield {
                "type": "text",
                "content": f"[PawApp ctx.chat fallback] {message}",
            }

    # ─── Cached sub-objects ────────────────────────────────────────

    def __post_init__(self) -> None:
        coordinator = None
        if self._app_services:
            coordinator = self._app_services.tool_coordinator
        approval = None
        if self._app_services:
            approval = self._app_services.approval_coordinator

        self._storage = AppStorage(
            session=self._session,
            namespace=f"pawapp:{self.app_id}",
        )
        self._tools = ToolProxy(
            tool_coordinator=coordinator,
        )
        self._file = FileProxy()
        self._ui = UIBridge(
            sse_channel=self._sse_channel,
            approval_coordinator=approval,
        )
        self._settings = AppSettings(
            plugin_registry=self._plugin_registry,
            app_id=self.app_id,
            agent_id=self.agent_id,
        )

    @property
    def storage(self) -> AppStorage:
        """App-namespaced KV storage."""
        return self._storage

    @property
    def tools(self) -> ToolProxy:
        """Invoke registered tools."""
        return self._tools

    @property
    def file(self) -> FileProxy:
        """File read/write operations."""
        return self._file

    @property
    def ui(self) -> UIBridge:
        """Agent-to-UI realtime communication."""
        return self._ui

    # ─── Notify ─────────────────────────────────────────────────────

    async def notify(
        self,
        *,
        channels: Optional[List[str]] = None,
        title: str = "",
        body: str = "",
    ) -> None:
        """Send multi-channel notification."""
        # pylint: disable=unused-argument
        # Will delegate to ChannelManager when available
        logger.info(
            "PawApp notify: channels=%s title=%s",
            channels,
            title,
        )

    # ─── Toast ──────────────────────────────────────────────────────

    async def toast(self, message: str, *, kind: str = "info") -> None:
        """Show a frontend toast notification."""
        if self._sse_channel:
            await self._sse_channel.send_event(
                {
                    "type": "pawapp:toast",
                    "message": message,
                    "kind": kind,
                },
            )

    @property
    def settings(self) -> AppSettings:
        """App configuration (from manifest settings)."""
        return self._settings

    @property
    def user(self) -> Dict[str, Any]:
        """Current user information.

        TODO: populate from auth / session once user identity is
        available in the request pipeline.
        """
        return {
            "id": self.agent_id or "default",
            "timezone": "UTC",
            "locale": "en-US",
        }

    @property
    def config(self) -> Dict[str, Any]:
        """Current configuration (active model, etc.).

        TODO: read active model from workspace / plugin registry.
        """
        return {"active_model": "qwen-max"}


class ChatReply:
    """Wrapper around chat response chunks."""

    def __init__(self, chunks: List[Any]):
        self._chunks = chunks

    @property
    def text(self) -> str:
        """Extract assistant text from the streamed chunks.

        The runtime yields Pydantic objects (``AgentResponse`` /
        ``Message`` / ``TextContent``) rather than plain dicts. Prefer the
        final ``AgentResponse.output``; fall back to completed messages,
        then streaming text deltas, then legacy dict/str chunks.
        """
        # pylint: disable=too-many-branches

        def _content_text(content_list: Any) -> str:
            parts: List[str] = []
            for block in content_list or []:
                if getattr(block, "delta", False):
                    continue  # skip streaming deltas (avoid double count)
                t = getattr(block, "text", None)
                if t is None and isinstance(block, dict):
                    t = block.get("text")
                if t:
                    parts.append(str(t))
            return "".join(parts)

        # 1) Last AgentResponse (.output list of messages)
        final_response = None
        for chunk in self._chunks:
            out = getattr(chunk, "output", None)
            if isinstance(out, list):
                final_response = chunk
        if final_response is not None:
            joined = "".join(
                _content_text(getattr(msg, "content", []))
                for msg in final_response.output
            ).strip()
            if joined:
                logger.debug("ChatReply: resolved via AgentResponse.output")
                return joined
            err = getattr(final_response, "error", None)
            if err:
                logger.debug("ChatReply: resolved via AgentResponse.error")
                return str(err)

        # 2) Completed Message objects (non-delta)
        msg_texts = []
        for chunk in self._chunks:
            if getattr(chunk, "output", None) is not None:
                continue
            content = getattr(chunk, "content", None)
            if isinstance(content, list):
                msg_texts.append(_content_text(content))
        joined = "".join(msg_texts).strip()
        if joined:
            logger.debug("ChatReply: resolved via Message objects")
            return joined

        # 3) Streaming text deltas
        delta_texts = [
            str(chunk.text)
            for chunk in self._chunks
            if getattr(chunk, "delta", False) and getattr(chunk, "text", None)
        ]
        if delta_texts:
            logger.debug("ChatReply: resolved via streaming deltas")
            return "".join(delta_texts).strip()

        # 4) Legacy dict/str chunks
        logger.debug("ChatReply: falling back to legacy dict/str")
        texts = []
        for chunk in self._chunks:
            if isinstance(chunk, dict):
                content = chunk.get(
                    "content",
                    chunk.get("text", ""),
                )
                if content:
                    texts.append(str(content))
            elif isinstance(chunk, str):
                texts.append(chunk)
        return "".join(texts)

    @property
    def chunks(self) -> List[Any]:
        """Raw response chunks."""
        return self._chunks
