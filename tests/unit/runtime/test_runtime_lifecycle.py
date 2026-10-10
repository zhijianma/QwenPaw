# -*- coding: utf-8 -*-
"""Tests for deterministic Runtime resource finalization."""

# pylint: disable=protected-access

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from agentscope.message import Msg, TextBlock

from qwenpaw.capabilities import GenerationRegistry
from qwenpaw.invocation_control import (
    InvocationControlService,
    SQLiteInvocationControl,
)
from qwenpaw.kernel import (
    CapabilitySelectionOverrides,
    InvocationScope,
    SubmissionStatus,
    TurnSubmissionRequest,
)
from qwenpaw.schemas import Message
from qwenpaw.runtime.phases import Phase
from qwenpaw.runtime.runtime import Runtime


class _Closable:
    def __init__(
        self,
        name: str,
        events: list[str],
        *,
        fail: bool = False,
    ) -> None:
        self.name = name
        self.events = events
        self.fail = fail

    async def close(self) -> None:
        self.events.append(self.name)
        if self.fail:
            raise RuntimeError(f"{self.name} failed")


class _BlockingClosable(_Closable):
    def __init__(
        self,
        name: str,
        events: list[str],
        entered: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        super().__init__(name, events)
        self.entered = entered
        self.release = release

    async def close(self) -> None:
        self.events.append(self.name)
        self.entered.set()
        await self.release.wait()


class _LifecycleRuntime(Runtime):
    def __init__(
        self,
        events: list[str],
        *,
        finally_error: BaseException | None = None,
    ) -> None:
        self.events = events
        self.finally_error = finally_error

    async def _run_hook_phase(self, _ctx, phase):
        assert phase is Phase.FINALLY
        self.events.append("finally")
        if self.finally_error is not None:
            raise self.finally_error
        return None


class _AssemblyProbeRuntime(Runtime):
    async def _run_hook_phase(self, _ctx, _phase):
        return None


def _context(
    events: list[str],
    *,
    failing_session: str = "",
):
    sessions = {
        key: _Closable(
            label,
            events,
            fail=key == failing_session,
        )
        for key, label in (
            ("memory_session", "memory"),
            ("driver_session", "driver"),
            ("command_session", "command"),
            ("agent_mode_session", "mode"),
            ("stop_gate_session", "gate"),
            ("hook_session", "hook"),
        )
    }
    return SimpleNamespace(
        agent=_Closable("agent", events),
        extras=sessions,
        session_id="session",
    )


@pytest.mark.asyncio
async def test_runtime_forwards_latest_profile_capability_selection(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    class _ProbeFactory:
        def __init__(self, _registry) -> None:
            pass

        async def open(self, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("stop after assembly input")

    monkeypatch.setattr(
        "qwenpaw.runtime.runtime.RuntimeAssemblyFactory",
        _ProbeFactory,
    )
    overrides = CapabilitySelectionOverrides(
        driver_provider_id="example.drivers.provider",
    )
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        config=SimpleNamespace(capability_selection=overrides),
        capability_registry=GenerationRegistry(),
    )
    runtime = _AssemblyProbeRuntime(
        workspace=workspace,
        app_services=SimpleNamespace(),
    )
    request = SimpleNamespace(
        session_id="chat-session",
        user_id="local-user",
        agent_id="default",
        root_session_id="",
        root_agent_id="",
        input=[],
        request_context={},
        channel="console",
    )

    with pytest.raises(RuntimeError, match="stop after assembly input"):
        async for _ in runtime.run(request):
            pass

    assert captured["selection_overrides"] is overrides
    assert captured["registry_generation"] is None


@pytest.mark.asyncio
async def test_runtime_forwards_internal_task_causal_identity(
    monkeypatch,
    tmp_path: Path,
) -> None:
    invocation_id = uuid4()
    correlation_id = uuid4()
    captured: dict[str, object] = {}

    class _ProbeFactory:
        def __init__(self, _registry) -> None:
            pass

        async def open(self, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("stop after assembly input")

    monkeypatch.setattr(
        "qwenpaw.runtime.runtime.RuntimeAssemblyFactory",
        _ProbeFactory,
    )
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
        config=SimpleNamespace(capability_selection=None),
        capability_registry=GenerationRegistry(),
    )
    runtime = _AssemblyProbeRuntime(
        workspace=workspace,
        app_services=SimpleNamespace(),
    )
    request = SimpleNamespace(
        session_id="task-session",
        user_id="local-user",
        agent_id="default",
        root_session_id="",
        root_agent_id="",
        input=[],
        request_context={
            "durable_task": True,
            "_task_approval_broker": object(),
            "os_registry_generation": 12,
            "os_invocation_id": str(invocation_id),
            "os_correlation_id": str(correlation_id),
        },
        channel="console",
    )

    with pytest.raises(RuntimeError, match="stop after assembly input"):
        async for _ in runtime.run(request):
            pass

    assert captured["invocation_id"] == invocation_id
    assert captured["correlation_id"] == correlation_id
    assert captured["registry_generation"] == 12


@pytest.mark.asyncio
async def test_finally_runs_before_provider_sessions_close() -> None:
    events: list[str] = []
    runtime = _LifecycleRuntime(events)

    await runtime._finalize_runtime(
        _context(events),
        _Closable("assembly", events),
    )

    assert events == [
        "agent",
        "finally",
        "memory",
        "driver",
        "command",
        "mode",
        "gate",
        "hook",
        "assembly",
    ]


@pytest.mark.asyncio
async def test_close_failure_does_not_leak_later_resources() -> None:
    events: list[str] = []
    runtime = _LifecycleRuntime(events)

    await runtime._finalize_runtime(
        _context(events, failing_session="driver_session"),
        _Closable("assembly", events),
    )

    assert events[-5:] == ["command", "mode", "gate", "hook", "assembly"]


@pytest.mark.asyncio
async def test_finally_error_propagates_after_resources_close() -> None:
    events: list[str] = []
    runtime = _LifecycleRuntime(
        events,
        finally_error=RuntimeError("finally failed"),
    )

    with pytest.raises(RuntimeError, match="finally failed"):
        await runtime._finalize_runtime(
            _context(events),
            _Closable("assembly", events),
        )

    assert events[-1] == "assembly"


@pytest.mark.asyncio
async def test_runtime_terminal_state_cancels_open_interactions() -> None:
    events: list[str] = []
    runtime = _LifecycleRuntime(events)
    context = _context(events)
    invocation_id = uuid4()
    lease = SimpleNamespace(
        steering=SimpleNamespace(invocation_id=invocation_id),
    )
    interactions = SimpleNamespace(cancel_invocation=AsyncMock())
    control = SimpleNamespace(finish_turn=AsyncMock())
    context.extras.update(
        {
            "interaction_service": interactions,
            "invocation_control_lease": lease,
            "invocation_control_service": control,
            "invocation_terminal_status": SubmissionStatus.SUCCEEDED,
        },
    )

    await runtime._finalize_runtime(context, _Closable("assembly", events))

    interactions.cancel_invocation.assert_awaited_once_with(
        invocation_id,
        detail="invocation finished as succeeded",
        include_non_blocking=False,
        preserve_conversation_continuations=True,
    )
    control.finish_turn.assert_awaited_once_with(
        lease,
        SubmissionStatus.SUCCEEDED,
    )


@pytest.mark.asyncio
async def test_successful_runtime_retains_deferred_interaction() -> None:
    invocation_id = uuid4()
    interactions = SimpleNamespace(cancel_invocation=AsyncMock())
    context = SimpleNamespace(
        extras={"interaction_service": interactions},
        session_id="session",
    )

    await Runtime._cancel_open_interactions(
        context,
        invocation_id,
        SubmissionStatus.SUCCEEDED,
    )

    interactions.cancel_invocation.assert_awaited_once_with(
        invocation_id,
        detail="invocation finished as succeeded",
        include_non_blocking=False,
        preserve_conversation_continuations=True,
    )


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_leak_provider_sessions() -> None:
    events: list[str] = []
    entered = asyncio.Event()
    release = asyncio.Event()
    runtime = _LifecycleRuntime(events)
    context = _context(events)
    context.extras["memory_session"] = _BlockingClosable(
        "memory",
        events,
        entered,
        release,
    )
    finalization = asyncio.create_task(
        runtime._finalize_runtime(
            context,
            _Closable("assembly", events),
        ),
    )
    await entered.wait()

    finalization.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await finalization
    assert events == [
        "agent",
        "finally",
        "memory",
        "driver",
        "command",
        "mode",
        "gate",
        "hook",
        "assembly",
    ]


@pytest.mark.asyncio
async def test_cancel_phase_drains_after_task_recancellation() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class _CancelSession:
        async def run_isolated(self, phase, *, timeout_seconds):
            assert phase.value == "on_cancel"
            assert timeout_seconds > 0
            entered.set()
            await release.wait()
            return ("example.failed-hook",)

    context = SimpleNamespace(
        extras={"hook_session": _CancelSession()},
    )
    runtime = Runtime(
        workspace=SimpleNamespace(),
        app_services=SimpleNamespace(),
    )
    task = asyncio.create_task(runtime._run_cancel_phase(context))
    await entered.wait()

    task.cancel()
    release.set()
    await task

    assert context.extras["cancel_hook_failures"] == ("example.failed-hook",)


@pytest.mark.asyncio
async def test_cancel_phase_infrastructure_failure_cannot_block_host() -> None:
    class _BrokenCancelSession:
        async def run_isolated(self, _phase, *, timeout_seconds):
            assert timeout_seconds > 0
            raise RuntimeError("router failed")

    context = SimpleNamespace(
        extras={"hook_session": _BrokenCancelSession()},
        session_id="chat-a",
    )
    runtime = Runtime(
        workspace=SimpleNamespace(),
        app_services=SimpleNamespace(),
    )

    await runtime._run_cancel_phase(context)

    assert context.extras["cancel_hook_failures"] == ("hook.provider",)


@pytest.mark.asyncio
async def test_partial_provider_open_rolls_back_every_session() -> None:
    events: list[str] = []

    class _CommandSession(_Closable):
        def __init__(self, provider_id: str, *, valid: bool) -> None:
            super().__init__(provider_id, events)
            self.provider_id = provider_id
            self.allows_dynamic_fallback = False
            self.valid = valid

        def list_commands(self):
            return ()

        async def dispatch(self, _request):
            return None

        async def fallback(self, _request):
            return None

    class _InvalidCommandSession(_Closable):
        def __init__(self, provider_id: str) -> None:
            super().__init__(provider_id, events)
            self.provider_id = provider_id
            self.allows_dynamic_fallback = False

        def list_commands(self):
            return ()

        async def fallback(self, _request):
            return None

    class _Provider:
        def __init__(self, provider_id: str, *, valid: bool) -> None:
            self.provider_id = provider_id
            self.valid = valid

        async def open(self, _scope, _host):
            if self.valid:
                return _CommandSession(self.provider_id, valid=True)
            return _InvalidCommandSession(self.provider_id)

    providers = {
        "example.commands.good": _Provider(
            "example.commands.good",
            valid=True,
        ),
        "example.commands.bad": _Provider(
            "example.commands.bad",
            valid=False,
        ),
    }
    selection = SimpleNamespace(
        command_provider_ids=tuple(providers),
    )
    assembly = SimpleNamespace(
        scope=SimpleNamespace(selection=selection),
        require=lambda provider_id, _slot: providers[provider_id],
    )
    context = SimpleNamespace(
        workspace=SimpleNamespace(
            plugins=SimpleNamespace(
                slash_command_registry=object(),
            ),
        ),
    )

    with pytest.raises(TypeError, match="without dispatch"):
        await _LifecycleRuntime(events)._open_command_session(
            context,
            assembly,
        )

    assert events == ["example.commands.bad", "example.commands.good"]


@pytest.mark.asyncio
async def test_control_plane_uses_chat_spec_not_runtime_session(
    tmp_path: Path,
) -> None:
    service = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    context = SimpleNamespace(
        agent_id="default",
        session_id="console:user-1",
        invocation_scope=InvocationScope(
            agent_id="default",
            conversation_id="chat-spec-1",
            session_id="console:user-1",
            root_agent_id="default",
            root_session_id="console:user-1",
            workspace_dir=str(tmp_path),
            registry_generation=1,
        ),
        input_msgs=[
            Msg(
                name="user",
                role="user",
                content=[TextBlock(text="hello")],
            ),
        ],
        extras={},
    )
    request = SimpleNamespace(
        request_context={
            "os_conversation_id": "forged-chat",
            "os_submission_idempotency_key": "message-1",
        },
        input=[Message(id="message-1")],
        channel="console",
    )

    await Runtime._open_invocation_control(
        context,
        request,
        uuid4(),
        service,
    )

    lease = context.extras["invocation_control_lease"]
    assert lease.submission.conversation_id == "chat-spec-1"
    assert "session_id" not in lease.submission.model_dump()
    await context.extras["interrupt_session"].close()


@pytest.mark.asyncio
async def test_interrupt_revokes_interactions_before_tools_and_runtime(
    tmp_path: Path,
) -> None:
    invocation_id = uuid4()
    events: list[str] = []
    captured: dict[str, object] = {}
    lease = SimpleNamespace(
        steering=SimpleNamespace(invocation_id=invocation_id),
    )

    async def cancel_interactions(*args, **kwargs):
        assert args == (invocation_id,)
        assert kwargs == {
            "detail": "invocation interrupt requested",
            "include_non_blocking": False,
        }
        events.append("interactions")

    async def cancel_tools(*args, **kwargs):
        assert args == ("console:user-1",)
        assert kwargs["agent_id"] == "default"
        events.append("tools")
        return 2

    class _Control:
        async def begin_turn(self, _request, *, invocation_id):
            assert invocation_id == lease.steering.invocation_id
            return lease

        async def bind_interrupt(self, *_args, **kwargs):
            captured["cancel_children"] = kwargs["cancel_children"]
            return _Closable("interrupt", events)

    context = SimpleNamespace(
        agent_id="default",
        session_id="console:user-1",
        invocation_scope=InvocationScope(
            agent_id="default",
            conversation_id="chat-spec-1",
            session_id="console:user-1",
            root_agent_id="default",
            root_session_id="console:user-1",
            workspace_dir=str(tmp_path),
            registry_generation=1,
        ),
        input_msgs=[
            Msg(
                name="user",
                role="user",
                content=[TextBlock(text="hello")],
            ),
        ],
        app_services=SimpleNamespace(
            tool_coordinator=SimpleNamespace(
                cancel_running_for_session=cancel_tools,
            ),
        ),
        extras={
            "interaction_service": SimpleNamespace(
                cancel_invocation=cancel_interactions,
            ),
        },
    )
    request = SimpleNamespace(
        request_context={
            "os_submission_idempotency_key": "message-1",
        },
        input=[Message(id="message-1")],
        channel="console",
    )

    await Runtime._open_invocation_control(
        context,
        request,
        invocation_id,
        _Control(),
    )
    cancel_children = captured["cancel_children"]
    assert callable(cancel_children)

    cancelled = await cancel_children()

    assert cancelled == 2
    assert events == ["interactions", "tools"]


@pytest.mark.asyncio
async def test_control_plane_adopts_prequeued_submission(
    tmp_path: Path,
) -> None:
    service = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    receipt = await service.enqueue_turn(
        TurnSubmissionRequest(
            agent_id="default",
            conversation_id="chat-spec-1",
            content="hello",
            idempotency_key="message-1",
        ),
    )
    assert receipt.submission_id is not None
    context = SimpleNamespace(
        agent_id="default",
        session_id="console:user-1",
        invocation_scope=InvocationScope(
            agent_id="default",
            conversation_id="chat-spec-1",
            session_id="console:user-1",
            root_agent_id="default",
            root_session_id="console:user-1",
            workspace_dir=str(tmp_path),
            registry_generation=1,
        ),
        input_msgs=[
            Msg(
                name="user",
                role="user",
                content=[TextBlock(text="hello")],
            ),
        ],
        extras={},
    )
    request = SimpleNamespace(
        request_context={
            "os_conversation_id": "forged-chat",
            "os_submission_idempotency_key": "message-1",
            "os_submission_id": str(receipt.submission_id),
        },
        input=[Message(id="message-1")],
        channel="console",
    )

    await Runtime._open_invocation_control(
        context,
        request,
        uuid4(),
        service,
    )

    lease = context.extras["invocation_control_lease"]
    assert lease.submission.submission_id == receipt.submission_id
    assert lease.submission.status is SubmissionStatus.RUNNING
    stored = await service.get_submission(receipt.submission_id)
    assert stored is not None
    assert stored.submission_id == receipt.submission_id
    await context.extras["interrupt_session"].close()
    await service.finish_turn(lease, SubmissionStatus.SUCCEEDED)
