# -*- coding: utf-8 -*-
"""Tests for the pinned Codex SDK compatibility audit."""

from __future__ import annotations

from email.message import Message
from pathlib import Path
from zipfile import ZipFile

from scripts.audit_codex_sdk_wheel import audit_wheel


def _write_wheel(
    path: Path,
    *,
    async_handler: bool,
    awaitable_handler: bool,
    accept_default: bool,
    wake_waiter: bool,
) -> None:
    async_parameter = ", approval_handler=None" if async_handler else ""
    handler_result = "Awaitable[dict]" if awaitable_handler else "dict"
    default_decision = "accept" if accept_default else "decline"
    client = f"""
ApprovalHandler = Callable[[str, dict | None], {handler_result}]
class CodexClient:
    def __init__(self, config=None, approval_handler=None):
        pass
    def _default_approval_handler(self, method, params):
        return {{"decision": "{default_decision}"}}
"""
    async_client = f"""
class AsyncCodexClient:
    def __init__(self, config=None{async_parameter}):
        pass
    async def next_turn_notification(self, turn_id):
        return await self._call_sync(lambda: None)
"""
    api = f"""
class AsyncCodex:
    def __init__(self, config=None{async_parameter}):
        pass
    async def thread_start(self): pass
    async def thread_resume(self): pass
    async def thread_fork(self): pass
    async def login_chatgpt(self): pass
    async def models(self): pass
class AsyncThread:
    async def read(self): pass
class AsyncTurnHandle:
    async def stream(self):
        try:
            yield None
        finally:
            self.unregister_turn_notifications()
            self.interrupt()
    async def steer(self): pass
    async def interrupt(self): pass
"""
    wake_statement = (
        "self.queue.put(RuntimeError())" if wake_waiter else "pass"
    )
    router = f"""
class MessageRouter:
    def unregister_turn(self, turn_id):
        {wake_statement}
"""
    metadata = Message()
    metadata["Name"] = "openai-codex"
    metadata["Version"] = "test"
    with ZipFile(path, "w") as wheel:
        wheel.writestr("openai_codex/client.py", client)
        wheel.writestr("openai_codex/async_client.py", async_client)
        wheel.writestr("openai_codex/api.py", api)
        wheel.writestr("openai_codex/_message_router.py", router)
        wheel.writestr(
            "openai_codex-test.dist-info/METADATA",
            metadata.as_bytes(),
        )


def test_rejects_default_accept_and_missing_deferred_handler(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "unsafe.whl"
    _write_wheel(
        wheel,
        async_handler=False,
        awaitable_handler=False,
        accept_default=True,
        wake_waiter=False,
    )

    report = audit_wheel(wheel)

    assert report["safe_for_qwenpaw"] is False
    assert len(report["wheel_sha256"]) == 64
    assert report["blockers"] == [
        "APPROVAL_DEFAULT_NOT_FAIL_CLOSED",
        "ASYNC_APPROVAL_HANDLER_UNAVAILABLE",
        "DEFERRED_APPROVAL_UNSUPPORTED",
        "ASYNC_STREAM_CANCELLATION_LEAK_RISK",
    ]


def test_accepts_complete_async_fail_closed_contract(tmp_path: Path) -> None:
    wheel = tmp_path / "safe.whl"
    _write_wheel(
        wheel,
        async_handler=True,
        awaitable_handler=True,
        accept_default=False,
        wake_waiter=True,
    )

    report = audit_wheel(wheel)

    assert report["safe_for_qwenpaw"] is True
    assert report["blockers"] == []
    assert all(report["operations"].values())
