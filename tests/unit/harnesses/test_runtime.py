# -*- coding: utf-8 -*-
"""Tests for provider-neutral third-party agent routing."""

# pylint: disable=protected-access

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

import pytest

from qwenpaw.app.chats.session import SafeJSONSession
from qwenpaw.capabilities import GenerationRegistry
from qwenpaw.harnesses.base import HarnessAdapter
from qwenpaw.harnesses.events import (
    HarnessAttachment,
    HarnessAttachmentKind,
    HarnessEvent,
    HarnessEventKind,
    HarnessHistoryItem,
    HarnessHistoryKind,
    HarnessProvider,
)
from qwenpaw.harnesses.runtime import HarnessRuntime
from qwenpaw.interactions import InteractionService
from qwenpaw.invocation_control import (
    InvocationControlService,
    SQLiteInvocationControl,
)
from qwenpaw.kernel import (
    COMMITTED_ACTION_ITEM_METADATA_KEY,
    ActionKind,
    ActionStatus,
    CommittedActionItem,
    ControlCommandStatus,
    SideEffectStatus,
    SubmissionStatus,
    ToolEffect,
    TurnSubmissionRequest,
)
from qwenpaw.runtime.actions import lite_action_store
from qwenpaw.runtime.environments import FilesystemEnvironmentStore
from qwenpaw.runtime.harness_environments import (
    RuntimeHarnessEnvironmentManager,
)
from qwenpaw.runtime.harness_recovery import (
    lite_harness_recovery_context_store,
)
from qwenpaw.schemas import (
    AgentRequest,
    FileContent,
    ImageContent,
    Message,
    MessageType,
    Role,
    TextContent,
)


class FakeAdapter(HarnessAdapter):
    """Emit one deterministic response for envelope assertions."""

    def __init__(self) -> None:
        self.stopped = False
        self.prompt = ""
        self.attachments: list[HarnessAttachment] = []

    async def status(self) -> HarnessProvider:
        return HarnessProvider(
            id="codex",
            name="Codex",
            available=True,
            installed=True,
            authenticated=True,
        )

    async def start_login(self, device_code: bool = False) -> dict:
        return {"device_code": device_code}

    async def logout(self) -> None:
        return None

    async def run_turn(  # pylint: disable=invalid-overridden-method
        self,
        *,
        session_id: str,
        prompt: str,
        cwd: Path,
        settings: dict,
        attachments: list[HarnessAttachment] | None = None,
    ) -> AsyncIterator[HarnessEvent]:
        assert session_id == "chat-1"
        assert cwd.is_absolute()
        self.prompt = prompt
        self.attachments = attachments or []
        yield HarnessEvent(
            kind=HarnessEventKind.TEXT_DELTA,
            text="Fixed",
        )
        yield HarnessEvent(kind=HarnessEventKind.COMPLETED)

    async def stop(self) -> None:
        self.stopped = True


class ToolAdapter(FakeAdapter):
    """Emit interleaved reasoning, tool progress, and assistant text."""

    async def run_turn(  # pylint: disable=invalid-overridden-method
        self,
        *,
        session_id: str,
        prompt: str,
        cwd: Path,
        settings: dict,
        attachments: list[HarnessAttachment] | None = None,
    ) -> AsyncIterator[HarnessEvent]:
        del attachments
        yield HarnessEvent(
            kind=HarnessEventKind.REASONING_DELTA,
            text="Checking",
            item_id="reason-1",
        )
        yield HarnessEvent(
            kind=HarnessEventKind.TOOL_STARTED,
            item_id="tool-1",
            tool_name="shell",
            data={
                "arguments": {"command": "pytest -q"},
                "provider_type": "commandExecution",
            },
        )
        yield HarnessEvent(
            kind=HarnessEventKind.TOOL_PROGRESS,
            item_id="tool-1",
            text="1 passed",
        )
        yield HarnessEvent(
            kind=HarnessEventKind.TOOL_COMPLETED,
            item_id="tool-1",
            tool_name="shell",
            text="1 passed",
            data={
                "arguments": {"command": "pytest -q"},
                "provider_type": "commandExecution",
                "exit_code": 0,
            },
        )
        yield HarnessEvent(
            kind=HarnessEventKind.TEXT_DELTA,
            text="Done",
        )
        yield HarnessEvent(kind=HarnessEventKind.COMPLETED)


class IncompleteToolAdapter(FakeAdapter):
    """End a remote action without a provider completion event."""

    async def run_turn(  # pylint: disable=invalid-overridden-method
        self,
        *,
        session_id: str,
        prompt: str,
        cwd: Path,
        settings: dict,
        attachments: list[HarnessAttachment] | None = None,
    ) -> AsyncIterator[HarnessEvent]:
        del session_id, prompt, cwd, settings, attachments
        yield HarnessEvent(
            kind=HarnessEventKind.TOOL_STARTED,
            item_id="tool-incomplete",
            tool_name="shell",
            data={
                "arguments": {"command": "long-running-command"},
                "provider_type": "commandExecution",
            },
        )
        yield HarnessEvent(
            kind=HarnessEventKind.ERROR,
            text="provider connection lost",
        )


class RecoverableInterruptedToolAdapter(ToolAdapter):
    """Expose matching provider history after an interrupted turn."""

    async def run_turn(  # pylint: disable=invalid-overridden-method
        self,
        *,
        session_id: str,
        prompt: str,
        cwd: Path,
        settings: dict,
        attachments: list[HarnessAttachment] | None = None,
    ) -> AsyncIterator[HarnessEvent]:
        async for event in super().run_turn(
            session_id=session_id,
            prompt=prompt,
            cwd=cwd,
            settings=settings,
            attachments=attachments,
        ):
            if event.kind is not HarnessEventKind.COMPLETED:
                yield event
        yield HarnessEvent(
            kind=HarnessEventKind.ERROR,
            text="provider stream disconnected",
        )

    def recovery_context_id(self, session_id: str) -> str | None:
        assert session_id == "chat-1"
        return "provider-thread-private"

    async def history(self, session_id: str) -> list[HarnessHistoryItem]:
        assert session_id == "chat-1"
        return [
            HarnessHistoryItem(
                kind=HarnessHistoryKind.TOOL_OUTPUT,
                item_id="tool-1",
                tool_name="shell",
                text="1 passed",
            ),
        ]


class FailingSession:
    """Fail only when a materialized Harness turn is committed."""

    async def get_session_state_dict(self, *_args, **_kwargs) -> dict:
        return {}

    async def save_session_state(self, **_kwargs) -> None:
        raise OSError("session store unavailable")


class CommandAdapter(FakeAdapter):
    """Record a provider-owned command without starting a normal turn."""

    def __init__(self) -> None:
        super().__init__()
        self.command = ""
        self.reset_session_id = ""

    async def run_command(
        self,
        *,
        session_id: str,
        command: str,
        arguments: str,
        cwd: Path,
        settings: dict,
    ) -> list[HarnessEvent]:
        del session_id, arguments, cwd, settings
        self.command = command
        return [
            HarnessEvent(
                kind=HarnessEventKind.TEXT_DELTA,
                text="Compacted",
            ),
            HarnessEvent(kind=HarnessEventKind.COMPLETED),
        ]

    async def reset_session(self, session_id: str) -> None:
        self.reset_session_id = session_id


class BlockingAdapter(FakeAdapter):
    """Keep one external turn alive until OS Interrupt reaches it."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    async def run_turn(  # pylint: disable=invalid-overridden-method
        self,
        *,
        session_id: str,
        prompt: str,
        cwd: Path,
        settings: dict,
        attachments: list[HarnessAttachment] | None = None,
    ) -> AsyncIterator[HarnessEvent]:
        del session_id, prompt, cwd, settings, attachments
        self.started.set()
        await self.release.wait()
        yield HarnessEvent(kind=HarnessEventKind.COMPLETED)

    async def cancel_turn(
        self,
        session_id: str,
        *,
        reason: str,
    ) -> bool:
        del session_id, reason
        self.cancelled = True
        self.release.set()
        return True


class EnvironmentCheckingAdapter(FakeAdapter):
    """Assert immutable environment evidence precedes provider execution."""

    def __init__(self, workspace_dir: Path) -> None:
        super().__init__()
        self._environment_root = (
            workspace_dir / ".qwenpaw" / "lite" / "environments"
        )
        self.environment_seen = False

    async def run_turn(  # pylint: disable=invalid-overridden-method
        self,
        *,
        session_id: str,
        prompt: str,
        cwd: Path,
        settings: dict,
        attachments: list[HarnessAttachment] | None = None,
    ) -> AsyncIterator[HarnessEvent]:
        self.environment_seen = bool(
            list(self._environment_root.rglob("environment.json")),
        )
        async for event in super().run_turn(
            session_id=session_id,
            prompt=prompt,
            cwd=cwd,
            settings=settings,
            attachments=attachments,
        ):
            yield event


def _environment_manager(tmp_path: Path) -> RuntimeHarnessEnvironmentManager:
    return RuntimeHarnessEnvironmentManager(
        FilesystemEnvironmentStore(tmp_path),
    )


@pytest.mark.asyncio
async def test_runtime_recreates_adapter_when_binary_changes(
    tmp_path: Path,
) -> None:
    runtime = HarnessRuntime(tmp_path)

    with patch(
        "qwenpaw.harnesses.runtime.create_adapter",
        side_effect=lambda *_args, **_kwargs: FakeAdapter(),
    ):
        first = await runtime.adapter("codex", {"binary": "/first/codex"})
        reused = await runtime.adapter("codex", {"binary": "/first/codex"})
        second = await runtime.adapter("codex", {"binary": "/second/codex"})

    assert reused is first
    assert second is not first
    assert first.stopped is True


@pytest.mark.asyncio
async def test_runtime_emits_qwenpaw_envelopes(tmp_path: Path) -> None:
    runtime = HarnessRuntime(tmp_path)
    adapter = FakeAdapter()
    runtime._adapters["codex"] = adapter
    request = AgentRequest(
        session_id="chat-1",
        input=[
            Message(
                role=Role.USER,
                content=[TextContent(text="Fix it")],
            ),
        ],
    )

    output = [
        item
        async for item in runtime.stream(
            backend="codex",
            request=request,
            cwd=tmp_path.resolve(),
        )
    ]

    assert [item.object for item in output] == [
        "response",
        "response",
        "message",
        "content",
        "message",
        "response",
    ]
    assert output[3].text == "Fixed"
    assert output[-1].status == "completed"
    assert adapter.prompt == "Fix it"
    assert adapter.attachments == []


@pytest.mark.asyncio
async def test_runtime_forwards_dropped_image_and_file(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "screenshot.png"
    file_path = tmp_path / "requirements.txt"
    adapter = FakeAdapter()
    runtime = HarnessRuntime(tmp_path)
    runtime._adapters["codex"] = adapter
    request = AgentRequest(
        session_id="chat-1",
        input=[
            Message(
                role=Role.USER,
                content=[
                    TextContent(text="Inspect these"),
                    ImageContent(image_url=str(image_path)),
                    FileContent(
                        filename="requirements.txt",
                        file_url=str(file_path),
                    ),
                ],
            ),
        ],
    )

    output = [
        item
        async for item in runtime.stream(
            backend="codex",
            request=request,
            cwd=tmp_path.resolve(),
        )
    ]

    assert output[-1].status == "completed"
    assert adapter.prompt == "Inspect these"
    assert [item.kind for item in adapter.attachments] == [
        HarnessAttachmentKind.IMAGE,
        HarnessAttachmentKind.FILE,
    ]
    assert [item.path for item in adapter.attachments] == [
        image_path,
        file_path,
    ]
    assert adapter.attachments[1].name == "requirements.txt"


@pytest.mark.asyncio
async def test_runtime_allows_attachment_only_turn(tmp_path: Path) -> None:
    image_path = tmp_path / "screenshot.png"
    adapter = FakeAdapter()
    runtime = HarnessRuntime(tmp_path)
    runtime._adapters["codex"] = adapter
    request = AgentRequest(
        session_id="chat-1",
        input=[
            Message(
                role=Role.USER,
                content=[ImageContent(image_url=str(image_path))],
            ),
        ],
    )

    output = [
        item
        async for item in runtime.stream(
            backend="codex",
            request=request,
            cwd=tmp_path.resolve(),
        )
    ]

    assert output[-1].status == "completed"
    assert adapter.prompt == ""
    assert adapter.attachments[0].path == image_path


@pytest.mark.asyncio
async def test_runtime_emits_reasoning_and_native_tool_envelopes(
    tmp_path: Path,
) -> None:
    runtime = HarnessRuntime(tmp_path)
    runtime._adapters["codex"] = ToolAdapter()
    request = AgentRequest(
        session_id="chat-1",
        input=[
            Message(
                role=Role.USER,
                content=[TextContent(text="Fix it")],
            ),
        ],
    )

    output = [
        item
        async for item in runtime.stream(
            backend="codex",
            request=request,
            cwd=tmp_path.resolve(),
        )
    ]
    final_response = output[-1]
    output_types = [message.type for message in final_response.output]

    assert output_types == [
        MessageType.REASONING,
        MessageType.PLUGIN_CALL,
        MessageType.PLUGIN_CALL_OUTPUT,
        MessageType.MESSAGE,
    ]
    tool_call = final_response.output[1].content[0].data
    tool_output = final_response.output[2].content[0].data
    assert tool_call["name"] == "shell"
    assert tool_call["arguments"] == '{"command": "pytest -q"}'
    assert tool_output["output"] == "1 passed"
    assert tool_output["exit_code"] == 0
    assert any(
        getattr(item, "type", None) == MessageType.REASONING for item in output
    )


@pytest.mark.asyncio
async def test_runtime_routes_declared_provider_command(
    tmp_path: Path,
) -> None:
    runtime = HarnessRuntime(tmp_path)
    adapter = CommandAdapter()
    runtime._adapters["codex"] = adapter
    request = AgentRequest(
        session_id="chat-1",
        input=[
            Message(
                role=Role.USER,
                content=[TextContent(text="/compact")],
            ),
        ],
    )

    output = [
        item
        async for item in runtime.stream(
            backend="codex",
            request=request,
            cwd=tmp_path.resolve(),
        )
    ]

    assert adapter.command == "compact"
    assert output[-1].status == "completed"
    assert output[-1].output[-1].content[0].text == "Compacted"


@pytest.mark.asyncio
async def test_runtime_handles_host_clear_for_every_backend(
    tmp_path: Path,
) -> None:
    runtime = HarnessRuntime(tmp_path)
    adapter = CommandAdapter()
    runtime._adapters["codex"] = adapter
    request = AgentRequest(
        session_id="chat-1",
        input=[
            Message(
                role=Role.USER,
                content=[TextContent(text="/clear")],
            ),
        ],
    )

    output = [
        item
        async for item in runtime.stream(
            backend="codex",
            request=request,
            cwd=tmp_path.resolve(),
        )
    ]

    assert adapter.reset_session_id == "chat-1"
    assert output[-1].output[-1].metadata["clear_history"] is True


def _os_request() -> AgentRequest:
    return AgentRequest(
        session_id="chat-1",
        channel="console",
        request_context={
            "os_conversation_id": "chat-spec-1",
            "os_submission_idempotency_key": "message-1",
        },
        input=[
            Message(
                role=Role.USER,
                content=[TextContent(text="Run through Harness")],
            ),
        ],
    )


def _prequeued_os_request(submission_id: str) -> AgentRequest:
    request = _os_request()
    request.request_context["os_submission_id"] = submission_id
    return request


@pytest.mark.asyncio
async def test_harness_turn_uses_os_invocation_lifecycle(
    tmp_path: Path,
) -> None:
    control = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        invocation_control=control,
        interaction_service=InteractionService(
            tmp_path / "interactions.sqlite3",
        ),
    )
    runtime = HarnessRuntime(
        tmp_path,
        workspace=workspace,
        environment_manager=_environment_manager(tmp_path),
    )
    runtime._adapters["codex"] = FakeAdapter()

    output = [
        item
        async for item in runtime.stream(
            backend="codex",
            request=_os_request(),
            cwd=tmp_path.resolve(),
        )
    ]
    queue = await control.read_queue(
        agent_id="default",
        conversation_id="chat-spec-1",
    )

    assert output[-1].status == "completed"
    assert queue.active_submission_id is None
    assert queue.submissions == ()


@pytest.mark.asyncio
async def test_controlled_harness_records_environment_before_dispatch(
    tmp_path: Path,
) -> None:
    control = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        invocation_control=control,
        interaction_service=InteractionService(
            tmp_path / "interactions.sqlite3",
        ),
    )
    runtime = HarnessRuntime(
        tmp_path,
        workspace=workspace,
        environment_manager=_environment_manager(tmp_path),
    )
    adapter = EnvironmentCheckingAdapter(tmp_path)
    runtime._adapters["codex"] = adapter

    output = [
        item
        async for item in runtime.stream(
            backend="codex",
            request=_os_request(),
            cwd=tmp_path.resolve(),
        )
    ]

    assert output[-1].status == "completed"
    assert adapter.environment_seen is True


@pytest.mark.asyncio
async def test_controlled_harness_records_remote_action_evidence(
    tmp_path: Path,
) -> None:
    control = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    registry = GenerationRegistry()
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        invocation_control=control,
        interaction_service=InteractionService(
            tmp_path / "interactions.sqlite3",
        ),
        capability_registry=registry,
    )
    session = SafeJSONSession(str(tmp_path / "sessions"))
    runtime = HarnessRuntime(
        tmp_path,
        session=session,
        workspace=workspace,
        environment_manager=_environment_manager(tmp_path),
    )
    runtime._adapters["codex"] = ToolAdapter()

    output = [
        item
        async for item in runtime.stream(
            backend="codex",
            request=_os_request(),
            cwd=tmp_path.resolve(),
        )
    ]
    records = await lite_action_store(tmp_path).list_for_conversation(
        "chat-spec-1",
    )

    assert output[-1].status == "completed"
    assert len(records) == 1
    record = records[0]
    assert record.request.kind is ActionKind.HARNESS_REMOTE
    assert record.request.capability_id == "qwenpaw.system.harness.codex"
    assert record.request.action_name == "shell"
    assert record.request.effect is ToolEffect.PROCESS
    assert record.request.registry_generation == registry.generation
    assert record.request.environment_ref is not None
    assert record.request.redacted_arguments == {
        "command": "[CONTENT OMITTED]",
    }
    assert record.result is not None
    assert record.result.status is ActionStatus.SUCCEEDED
    assert record.result.side_effect_status is SideEffectStatus.SUCCEEDED
    persisted = await session.get_session_state_dict(
        "chat-1",
        "chat-1",
        "console",
    )
    context = persisted["agent"]["state"]["context"]
    result_block = next(
        block
        for message in context
        for block in message.get("content", [])
        if block.get("type") == "tool_result"
    )
    committed_item = CommittedActionItem.model_validate(
        result_block["metadata"][COMMITTED_ACTION_ITEM_METADATA_KEY],
    )
    assert committed_item.action_id == record.request.action_id
    assert committed_item.observation_digest == (
        record.result.observation_digest
    )


@pytest.mark.asyncio
async def test_controlled_harness_marks_unfinished_remote_action_unknown(
    tmp_path: Path,
) -> None:
    control = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        invocation_control=control,
        interaction_service=InteractionService(
            tmp_path / "interactions.sqlite3",
        ),
        capability_registry=GenerationRegistry(),
    )
    runtime = HarnessRuntime(
        tmp_path,
        workspace=workspace,
        environment_manager=_environment_manager(tmp_path),
    )
    runtime._adapters["codex"] = IncompleteToolAdapter()

    output = [
        item
        async for item in runtime.stream(
            backend="codex",
            request=_os_request(),
            cwd=tmp_path.resolve(),
        )
    ]
    [record] = await lite_action_store(
        tmp_path,
    ).list_for_conversation("chat-spec-1")

    assert output[-1].status == "failed"
    assert record.result is not None
    assert record.result.status is ActionStatus.UNKNOWN
    assert record.result.error_code == "harness_turn_error"
    assert record.result.side_effect_status is SideEffectStatus.UNCERTAIN


@pytest.mark.asyncio
async def test_harness_action_fails_closed_when_context_commit_fails(
    tmp_path: Path,
) -> None:
    control = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        invocation_control=control,
        interaction_service=InteractionService(
            tmp_path / "interactions.sqlite3",
        ),
        capability_registry=GenerationRegistry(),
    )
    runtime = HarnessRuntime(
        tmp_path,
        session=FailingSession(),
        workspace=workspace,
        environment_manager=_environment_manager(tmp_path),
    )
    runtime._adapters["codex"] = ToolAdapter()

    output = [
        item
        async for item in runtime.stream(
            backend="codex",
            request=_os_request(),
            cwd=tmp_path.resolve(),
        )
    ]
    [record] = await lite_action_store(
        tmp_path,
    ).list_for_conversation("chat-spec-1")

    assert output[-1].status == "failed"
    assert output[-1].error["code"] == "harness_context_commit_failed"
    assert record.result is not None
    assert record.result.status is ActionStatus.SUCCEEDED
    queue = await control.read_queue(
        agent_id="default",
        conversation_id="chat-spec-1",
    )
    assert queue.active_submission_id is None


@pytest.mark.asyncio
async def test_interrupted_harness_persists_recovery_admission(
    tmp_path: Path,
) -> None:
    control = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        invocation_control=control,
        interaction_service=InteractionService(
            tmp_path / "interactions.sqlite3",
        ),
        capability_registry=GenerationRegistry(),
    )
    session = SafeJSONSession(str(tmp_path / "sessions"))
    runtime = HarnessRuntime(
        tmp_path,
        session=session,
        workspace=workspace,
        environment_manager=_environment_manager(tmp_path),
    )
    runtime._adapters["codex"] = RecoverableInterruptedToolAdapter()

    output = [
        item
        async for item in runtime.stream(
            backend="codex",
            request=_os_request(),
            cwd=tmp_path.resolve(),
        )
    ]
    [record] = await lite_action_store(
        tmp_path,
    ).list_for_conversation("chat-spec-1")

    assert output[-1].status == "failed"
    checkpoint_id = output[-1].metadata[
        "harness_recovery_checkpoint_id"
    ]
    checkpoint = await lite_harness_recovery_context_store(tmp_path).load(
        UUID(checkpoint_id),
    )
    assert checkpoint.backend == "codex"
    assert checkpoint.action_count == 1
    assert checkpoint.action_evidence_digest
    assert record.result is not None
    assert "provider-thread-private" not in checkpoint.model_dump_json()


@pytest.mark.asyncio
async def test_unsatisfied_environment_prevents_harness_dispatch(
    tmp_path: Path,
) -> None:
    control = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        invocation_control=control,
        interaction_service=InteractionService(
            tmp_path / "interactions.sqlite3",
        ),
    )
    runtime = HarnessRuntime(
        tmp_path,
        workspace=workspace,
        environment_manager=_environment_manager(tmp_path),
    )
    adapter = FakeAdapter()
    runtime._adapters["codex"] = adapter

    output = [
        item
        async for item in runtime.stream(
            backend="codex",
            request=_os_request(),
            cwd=tmp_path.resolve(),
            settings={"sandbox": "imaginary"},
        )
    ]

    assert output[-1].status == "failed"
    assert "environment.harness.sandbox.unknown" in str(
        output[-1].error,
    )
    assert adapter.prompt == ""
    queue = await control.read_queue(
        agent_id="default",
        conversation_id="chat-spec-1",
    )
    assert queue.active_submission_id is None


@pytest.mark.asyncio
async def test_harness_turn_adopts_prequeued_submission(
    tmp_path: Path,
) -> None:
    control = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    receipt = await control.enqueue_turn(
        TurnSubmissionRequest(
            agent_id="default",
            conversation_id="chat-spec-1",
            content="Run through Harness",
            idempotency_key="message-1",
        ),
    )
    assert receipt.submission_id is not None
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        invocation_control=control,
        interaction_service=InteractionService(
            tmp_path / "interactions.sqlite3",
        ),
    )
    runtime = HarnessRuntime(
        tmp_path,
        workspace=workspace,
        environment_manager=_environment_manager(tmp_path),
    )
    runtime._adapters["codex"] = FakeAdapter()

    output = [
        item
        async for item in runtime.stream(
            backend="codex",
            request=_prequeued_os_request(str(receipt.submission_id)),
            cwd=tmp_path.resolve(),
        )
    ]
    stored = await control.get_submission(receipt.submission_id)

    assert output[-1].status == "completed"
    assert stored is not None
    assert stored.submission_id == receipt.submission_id
    assert stored.status is SubmissionStatus.SUCCEEDED
    queue = await control.read_queue(
        agent_id="default",
        conversation_id="chat-spec-1",
    )
    assert queue.active_submission_id is None
    assert queue.submissions == ()


@pytest.mark.asyncio
async def test_os_interrupt_cancels_active_harness_turn(
    tmp_path: Path,
) -> None:
    control = InvocationControlService(
        store=SQLiteInvocationControl(tmp_path / "control.sqlite3"),
    )
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        invocation_control=control,
        interaction_service=InteractionService(
            tmp_path / "interactions.sqlite3",
        ),
    )
    runtime = HarnessRuntime(
        tmp_path,
        workspace=workspace,
        environment_manager=_environment_manager(tmp_path),
    )
    adapter = BlockingAdapter()
    runtime._adapters["codex"] = adapter

    async def consume() -> list[object]:
        return [
            item
            async for item in runtime.stream(
                backend="codex",
                request=_os_request(),
                cwd=tmp_path.resolve(),
            )
        ]

    task = asyncio.create_task(consume())
    await asyncio.wait_for(adapter.started.wait(), timeout=2)
    active_queue = await control.read_queue(
        agent_id="default",
        conversation_id="chat-spec-1",
    )
    assert active_queue.active_submission_id is not None
    receipt = await control.interrupt_current(
        agent_id="default",
        conversation_id="chat-spec-1",
        idempotency_key="interrupt-harness-1",
    )

    assert receipt is not None
    assert receipt.status is ControlCommandStatus.APPLIED
    assert adapter.cancelled is True
    with pytest.raises(asyncio.CancelledError):
        await task
    queue = await control.read_queue(
        agent_id="default",
        conversation_id="chat-spec-1",
    )
    assert queue.active_submission_id is None
    assert queue.submissions == ()


@pytest.mark.asyncio
async def test_task_event_stream_cancels_exact_harness_turn(
    tmp_path: Path,
) -> None:
    runtime = HarnessRuntime(tmp_path)
    adapter = BlockingAdapter()
    runtime._adapters["codex"] = adapter

    async def consume() -> list[HarnessEvent]:
        return [
            event
            async for event in runtime.task_events(
                backend="codex",
                session_id="task-session-1",
                prompt="Run through Task Runner",
                cwd=tmp_path.resolve(),
                settings={
                    "_request_context": {
                        "cancellation_reason": "Task cancelled by user",
                    },
                },
            )
        ]

    task = asyncio.create_task(consume())
    await asyncio.wait_for(adapter.started.wait(), timeout=2)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert adapter.cancelled is True
