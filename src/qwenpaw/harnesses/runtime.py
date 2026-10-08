# -*- coding: utf-8 -*-
"""Workspace-scoped third-party agent lifecycle and translation."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..kernel import (
    ActionStatus,
    EnvironmentResolution,
    HarnessRecoveryContextCheckpoint,
    InvocationScope,
    SubmissionStatus,
    TurnSubmissionRequest,
)
from ..runtime.actions import (
    RuntimeActionRecorder,
    lite_action_store,
    model_step_action_evidence_digest,
    model_step_committed_action_items,
)
from ..runtime.harness_recovery import (
    build_harness_recovery_checkpoint,
    lite_harness_recovery_context_store,
)
from ..schemas import (
    AgentResponse,
    ContentType,
    MessageType,
    RunStatus,
)
from ..utils.daily_telemetry import record_agent_activity
from .actions import HARNESS_ACTION_TRACKER_KEY, HarnessActionTracker
from .base import HarnessAdapter
from .capabilities import HarnessCapabilityResolver
from .events import (
    HarnessAttachment,
    HarnessAttachmentKind,
    HarnessEvent,
    HarnessEventKind,
    HarnessHistoryKind,
    HarnessProvider,
)
from .registry import (
    PROVIDER_CATALOG,
    adapter_config_key,
    create_adapter,
    get_provider,
)
from .session import HarnessSessionBridge
from .streaming import TextStream, ToolStream

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _HarnessInvocationBinding:
    """Resources pinned while one Harness turn owns an OS invocation."""

    control: Any
    interaction_service: Any
    interrupt: Any
    lease: Any
    invocation_id: uuid.UUID
    capability_lease: Any = None


class HarnessRuntime:
    """Own adapters for one workspace and expose QwenPaw envelopes."""

    def __init__(
        self,
        workspace_dir: Path,
        session: Any = None,
        agent_id: str = "default",
        workspace: Any = None,
        environment_manager: Any = None,
    ) -> None:
        self._workspace_dir = workspace_dir
        self._state_dir = workspace_dir / "harnesses"
        self._agent_id = agent_id
        self._workspace = workspace
        self._environment_manager = environment_manager
        self._adapters: dict[str, HarnessAdapter] = {}
        self._adapter_keys: dict[str, tuple[Any, ...]] = {}
        self._adapter_lock = asyncio.Lock()
        self._session_bridge = (
            HarnessSessionBridge(session) if session is not None else None
        )
        self._capability_resolver = HarnessCapabilityResolver(
            workspace_dir,
            workspace,
        )

    async def providers(
        self,
        provider_settings: dict[str, dict[str, Any]] | None = None,
    ) -> list[HarnessProvider]:
        """Return the third-party agent catalog with live provider status."""
        result: list[HarnessProvider] = []
        for item in PROVIDER_CATALOG:
            provider_id = item.id
            if item.coming_soon:
                result.append(
                    HarnessProvider(
                        id=provider_id,
                        name=item.name,
                        available=False,
                        coming_soon=True,
                    ),
                )
                continue
            adapter = await self.adapter(
                provider_id,
                (provider_settings or {}).get(provider_id),
            )
            provider = await adapter.status()
            provider.capabilities = item.capabilities
            result.append(provider)
        return result

    async def adapter(
        self,
        provider_id: str,
        settings: dict[str, Any] | None = None,
    ) -> HarnessAdapter:
        """Return an adapter matching the current provider configuration."""
        next_key = adapter_config_key(provider_id, settings)
        async with self._adapter_lock:
            adapter = self._adapters.get(provider_id)
            current_key = self._adapter_keys.get(provider_id)
            if adapter is not None and (
                current_key is None or current_key == next_key
            ):
                return adapter
            if adapter is not None:
                await adapter.stop()
            created = create_adapter(provider_id, self._state_dir, settings)
            self._adapter_keys[provider_id] = next_key
            self._adapters[provider_id] = created
            return created

    async def stream(
        self,
        *,
        backend: str,
        request: Any,
        cwd: Path,
        settings: dict[str, Any] | None = None,
    ) -> AsyncGenerator[Any, None]:
        """Run one Harness turn through the shared invocation lifecycle."""
        settings = dict(settings or {})
        lifecycle = await self._bind_invocation_control(
            backend=backend,
            request=request,
            settings=settings,
        )
        if lifecycle is None:
            async for item in self._stream_uncontrolled(
                backend=backend,
                request=request,
                cwd=cwd,
                settings=settings,
            ):
                yield item
            return

        terminal = SubmissionStatus.SUCCEEDED
        try:
            async for item in self._stream_uncontrolled(
                backend=backend,
                request=request,
                cwd=cwd,
                settings=settings,
            ):
                item_status = getattr(item, "status", None)
                if item_status is RunStatus.Failed:
                    terminal = SubmissionStatus.FAILED
                elif item_status is RunStatus.Cancelled:
                    terminal = SubmissionStatus.INTERRUPTED
                yield item
        except asyncio.CancelledError:
            terminal = SubmissionStatus.INTERRUPTED
            raise
        except BaseException:
            terminal = SubmissionStatus.FAILED
            raise
        finally:
            await self._drain_invocation_cleanup(lifecycle, terminal)

    async def task_events(
        self,
        *,
        backend: str,
        session_id: str,
        prompt: str,
        cwd: Path,
        settings: dict[str, Any] | None = None,
    ) -> AsyncGenerator[HarnessEvent, None]:
        """Run one Task-owned turn without creating a second Chat lease."""
        settings = dict(settings or {})
        request_context = dict(settings.get("_request_context") or {})
        settings[
            "_runtime_capabilities"
        ] = await self._capability_resolver.resolve(request_context)
        adapter = await self.adapter(backend, settings)
        try:
            async for event in adapter.run_turn(
                session_id=session_id,
                prompt=prompt,
                cwd=cwd,
                settings=settings,
                attachments=None,
            ):
                yield event
        except asyncio.CancelledError:
            reason = str(
                request_context.get("cancellation_reason")
                or "QwenPaw Task execution cancelled",
            )
            await asyncio.shield(
                adapter.cancel_turn(session_id, reason=reason),
            )
            raise

    async def _bind_invocation_control(
        self,
        *,
        backend: str,
        request: Any,
        settings: dict[str, Any],
    ) -> _HarnessInvocationBinding | None:
        """Open the shared OS lifecycle when the request carries its IDs."""
        request_context = {
            **dict(getattr(request, "request_context", None) or {}),
            **dict(settings.get("_request_context") or {}),
        }
        control = getattr(self._workspace, "invocation_control", None)
        conversation_id = request_context.get("os_conversation_id")
        idempotency_key = request_context.get(
            "os_submission_idempotency_key",
        )
        if control is None or not conversation_id or not idempotency_key:
            return None

        invocation_id = uuid.uuid4()
        prompt, _ = self._content_from_request(request)
        raw_priority = request_context.get("os_submission_priority", 20)
        priority = (
            raw_priority
            if isinstance(raw_priority, int)
            and not isinstance(raw_priority, bool)
            else 20
        )
        input_messages = getattr(request, "input", None) or []
        latest = input_messages[-1] if input_messages else None
        artifact_refs = tuple(getattr(latest, "artifact_refs", None) or ())
        submission = TurnSubmissionRequest(
            agent_id=self._agent_id,
            conversation_id=str(conversation_id),
            priority=priority,
            content=prompt or "[non-text user input]",
            artifact_refs=artifact_refs,
            request_context={
                "channel": str(
                    getattr(request, "channel", "") or "",
                ),
                "harness_backend": backend,
            },
            idempotency_key=str(idempotency_key),
        )
        raw_submission_id = request_context.get("os_submission_id")
        if raw_submission_id:
            try:
                submission_id = uuid.UUID(str(raw_submission_id))
            except ValueError as exc:
                raise ValueError(
                    "invalid prequeued submission identity",
                ) from exc
            lease = await control.begin_submitted_turn(
                submission_id,
                invocation_id=invocation_id,
                agent_id=self._agent_id,
                conversation_id=str(conversation_id),
            )
        else:
            lease = await control.begin_turn(
                submission,
                invocation_id=invocation_id,
            )
        interaction_service = getattr(
            self._workspace,
            "interaction_service",
            None,
        )
        request_context.update(
            {
                "os_invocation_id": str(invocation_id),
                "os_submission_id": str(
                    lease.submission.submission_id,
                ),
                "os_correlation_id": str(
                    lease.submission.correlation_id,
                ),
                "_interaction_service": interaction_service,
            },
        )
        settings["_request_context"] = request_context
        task = asyncio.current_task()
        if task is None:
            await control.finish_turn(lease, SubmissionStatus.FAILED)
            raise RuntimeError("harness invocation has no owning task")

        session_id = str(getattr(request, "session_id", "") or "default")
        try:
            capability_lease = await self._open_action_tracker(
                backend=backend,
                request_context=request_context,
                invocation_id=invocation_id,
                conversation_id=str(conversation_id),
                session_id=session_id,
            )
            settings["_request_context"] = request_context
        except BaseException:
            await control.finish_turn(lease, SubmissionStatus.FAILED)
            raise

        async def cancel_harness_turn() -> int:
            adapter = await self.adapter(backend, settings)
            cancelled = await adapter.cancel_turn(
                session_id,
                reason="QwenPaw invocation interrupted",
            )
            return int(cancelled)

        try:
            binding = await control.bind_interrupt(
                invocation_id,
                task,
                lease=lease,
                agent_id=self._agent_id,
                conversation_id=str(conversation_id),
                cancel_children=cancel_harness_turn,
            )
        except BaseException:
            if capability_lease is not None:
                await capability_lease.close()
            await control.finish_turn(lease, SubmissionStatus.FAILED)
            raise
        return _HarnessInvocationBinding(
            control=control,
            interaction_service=interaction_service,
            interrupt=binding,
            lease=lease,
            invocation_id=invocation_id,
            capability_lease=capability_lease,
        )

    async def _open_action_tracker(
        self,
        *,
        backend: str,
        request_context: dict[str, Any],
        invocation_id: uuid.UUID,
        conversation_id: str,
        session_id: str,
    ) -> HarnessRecoveryContextCheckpoint | None:
        """Pin one generation and expose its Harness Action bridge."""
        registry = getattr(self._workspace, "capability_registry", None)
        if registry is None or not hasattr(registry, "pin"):
            return None
        capability_lease = await registry.pin()
        try:
            scope = InvocationScope(
                invocation_id=invocation_id,
                agent_id=self._agent_id,
                conversation_id=conversation_id,
                session_id=session_id,
                root_agent_id=str(
                    request_context.get("root_agent_id") or self._agent_id,
                ),
                root_session_id=str(
                    request_context.get("root_session_id") or session_id,
                ),
                workspace_dir=str(self._workspace_dir),
                registry_generation=capability_lease.generation,
            )
            recorder = RuntimeActionRecorder(
                scope,
                lite_action_store(self._workspace_dir),
            )
            request_context[HARNESS_ACTION_TRACKER_KEY] = HarnessActionTracker(
                backend=backend,
                scope=scope,
                recorder=recorder,
            )
            request_context[
                "os_registry_generation"
            ] = capability_lease.generation
            return capability_lease
        except BaseException:
            await capability_lease.close()
            raise

    async def _drain_invocation_cleanup(
        self,
        lifecycle: _HarnessInvocationBinding,
        terminal: SubmissionStatus,
    ) -> None:
        """Finish durable cleanup even when cancellation is still pending."""
        cleanup = asyncio.create_task(
            self._finish_invocation_control(lifecycle, terminal),
        )
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
        await cleanup

    @staticmethod
    async def _finish_invocation_control(
        lifecycle: _HarnessInvocationBinding,
        terminal: SubmissionStatus,
    ) -> None:
        """Commit Harness cleanup before releasing its pinned invocation."""
        try:
            await lifecycle.interrupt.close()
            if lifecycle.interaction_service is not None:
                await lifecycle.interaction_service.cancel_invocation(
                    lifecycle.invocation_id,
                    detail=f"invocation finished as {terminal.value}",
                    include_non_blocking=False,
                )
            await lifecycle.control.finish_turn(lifecycle.lease, terminal)
        finally:
            if lifecycle.capability_lease is not None:
                await lifecycle.capability_lease.close()

    # pylint: disable-next=too-many-branches,too-many-statements
    async def _stream_uncontrolled(
        self,
        *,
        backend: str,
        request: Any,
        cwd: Path,
        settings: dict[str, Any] | None = None,
    ) -> AsyncGenerator[Any, None]:
        """Translate one Harness turn after lifecycle ownership is bound."""
        settings = dict(settings or {})
        request_context = dict(settings.get("_request_context") or {})
        settings["_request_context"] = request_context
        action_tracker = request_context.get(HARNESS_ACTION_TRACKER_KEY)
        if not isinstance(action_tracker, HarnessActionTracker):
            action_tracker = None
        settings[
            "_runtime_capabilities"
        ] = await self._capability_resolver.resolve(request_context)
        session_id = str(getattr(request, "session_id", "") or "default")
        prompt, attachments = self._content_from_request(request)
        command, arguments = (
            self._parse_command(prompt) if not attachments else ("", "")
        )
        response = AgentResponse(
            id=f"response_{uuid.uuid4().hex}",
            output=[],
            status=RunStatus.Created,
            created_at=datetime.now(timezone.utc).isoformat(
                timespec="seconds",
            ),
        )
        response.object = "response"
        response.session_id = session_id
        sequence = 0

        def tagged(value: Any) -> Any:
            nonlocal sequence
            sequence += 1
            value.sequence_number = sequence
            return value

        yield tagged(response.model_copy(deep=True))
        response.status = RunStatus.InProgress
        yield tagged(response.model_copy(deep=True))

        text_stream = TextStream(response)
        tool_stream = ToolStream(response)
        error_text = ""
        cancelled = False
        task_cancelled = False
        adapter: HarnessAdapter | None = None

        try:
            if command in {"new", "clear"}:
                adapter = await self.adapter(backend, settings)
                await adapter.reset_session(session_id)
                events = [
                    HarnessEvent(
                        kind=HarnessEventKind.TEXT_DELTA,
                        text="Started a fresh conversation.",
                    ),
                    HarnessEvent(kind=HarnessEventKind.COMPLETED),
                ]
                event_stream = self._iter_events(events)
            elif command:
                environment = await self._resolve_environment(
                    backend=backend,
                    cwd=cwd,
                    settings=settings,
                    request_context=request_context,
                )
                if action_tracker is not None and environment is not None:
                    action_tracker.bind_environment(environment)
                provider = get_provider(backend)
                supported = {
                    item.name for item in provider.capabilities.commands
                }
                if command not in supported:
                    raise ValueError(
                        f"Unsupported {provider.name} command: /{command}",
                    )
                adapter = await self.adapter(backend, settings)
                events = await adapter.run_command(
                    session_id=session_id,
                    command=command,
                    arguments=arguments,
                    cwd=cwd,
                    settings=settings,
                )
                event_stream = self._iter_events(events)
            else:
                environment = await self._resolve_environment(
                    backend=backend,
                    cwd=cwd,
                    settings=settings,
                    request_context=request_context,
                )
                if action_tracker is not None and environment is not None:
                    action_tracker.bind_environment(environment)
                adapter = await self.adapter(backend, settings)
                await record_agent_activity()
                event_stream = adapter.run_turn(
                    session_id=session_id,
                    prompt=prompt,
                    cwd=cwd,
                    settings=settings,
                    attachments=attachments,
                )
            async for event in event_stream:
                if event.kind == HarnessEventKind.TEXT_DELTA:
                    for item in text_stream.push(
                        MessageType.MESSAGE,
                        event.text,
                    ):
                        yield tagged(item)
                elif event.kind == HarnessEventKind.REASONING_DELTA:
                    for item in text_stream.push(
                        MessageType.REASONING,
                        event.text,
                    ):
                        yield tagged(item)
                elif event.kind == HarnessEventKind.TOOL_STARTED:
                    if action_tracker is not None:
                        await action_tracker.begin_event(event)
                    for item in text_stream.finish():
                        yield tagged(item)
                    for item in tool_stream.start(event):
                        yield tagged(item)
                elif event.kind == HarnessEventKind.TOOL_PROGRESS:
                    for item in tool_stream.progress(event):
                        yield tagged(item)
                elif event.kind == HarnessEventKind.TOOL_COMPLETED:
                    if action_tracker is not None:
                        await action_tracker.complete_event(event)
                    for item in tool_stream.complete(event):
                        yield tagged(item)
                elif event.kind == HarnessEventKind.ERROR:
                    error_text = event.text
                elif event.kind == HarnessEventKind.CANCELLED:
                    cancelled = True
        except asyncio.CancelledError:
            cancelled = True
            task_cancelled = True
        except Exception as exc:
            error_text = str(exc)

        if action_tracker is not None:
            pending_status = (
                ActionStatus.CANCELLED if cancelled else ActionStatus.UNKNOWN
            )
            pending_error = (
                "harness_cancelled"
                if cancelled
                else (
                    "harness_turn_error"
                    if error_text
                    else "provider_completion_missing"
                )
            )
            try:
                await action_tracker.finalize_pending(
                    pending_status,
                    pending_error,
                )
            except Exception as exc:
                error_text = error_text or str(exc)

        for item in text_stream.finish():
            yield tagged(item)

        clear_history = command in {"new", "clear"}
        if clear_history and response.output:
            response.output[-1].metadata = {
                **dict(response.output[-1].metadata or {}),
                "clear_history": True,
            }

        if error_text:
            response.status = RunStatus.Failed
            response.error = {"code": "harness_error", "message": error_text}
        elif cancelled:
            response.status = RunStatus.Cancelled
        else:
            response.status = RunStatus.Completed
        response.completed_at = datetime.now(timezone.utc).isoformat(
            timespec="seconds",
        )
        if self._session_bridge is not None and clear_history:
            try:
                await self._session_bridge.clear(
                    session_id=session_id,
                    user_id=str(
                        getattr(request, "user_id", "") or session_id,
                    ),
                    channel=str(getattr(request, "channel", "") or ""),
                )
            except Exception:
                logger.warning(
                    "Failed to clear third-party session %s",
                    session_id,
                    exc_info=True,
                )
        elif self._session_bridge is not None:
            try:
                await self._session_bridge.append_turn(
                    request=request,
                    response=response,
                    backend=backend,
                    committed_items=(
                        action_tracker.committed_items
                        if action_tracker is not None
                        else None
                    ),
                )
                if (
                    error_text
                    and not cancelled
                    and action_tracker is not None
                    and action_tracker.committed_items
                    and adapter is not None
                ):
                    checkpoint = await self._checkpoint_recovery_context(
                        backend=backend,
                        adapter=adapter,
                        action_tracker=action_tracker,
                        request=request,
                        request_context=request_context,
                    )
                    if checkpoint is not None:
                        response.metadata = {
                            **dict(response.metadata or {}),
                            "harness_recovery_checkpoint_id": str(
                                checkpoint.checkpoint_id,
                            ),
                        }
            except Exception:
                logger.warning(
                    "Failed to persist third-party session %s",
                    session_id,
                    exc_info=True,
                )
                if (
                    action_tracker is not None
                    and action_tracker.committed_items
                ):
                    response.status = RunStatus.Failed
                    response.error = {
                        "code": "harness_context_commit_failed",
                        "message": (
                            "Harness actions completed, but their context "
                            "could not be committed. Do not retry actions."
                        ),
                    }
        if task_cancelled:
            raise asyncio.CancelledError
        yield tagged(response)

    # pylint: disable-next=too-many-return-statements
    async def _checkpoint_recovery_context(
        self,
        *,
        backend: str,
        adapter: HarnessAdapter,
        action_tracker: HarnessActionTracker,
        request: Any,
        request_context: dict[str, Any],
    ) -> Any:
        """Persist admission only after all independent evidence agrees."""
        if self._session_bridge is None:
            return None
        try:
            invocation_id = uuid.UUID(
                str(request_context["os_invocation_id"]),
            )
            conversation_id = str(
                request_context["os_conversation_id"],
            )
            source_submission_id = uuid.UUID(
                str(request_context["os_submission_id"]),
            )
            session_id = str(
                getattr(request, "session_id", "") or "default",
            )
            user_id = str(
                getattr(request, "user_id", "") or session_id,
            )
            channel = str(getattr(request, "channel", "") or "")
            records = await lite_action_store(
                self._workspace_dir,
            ).scan_for_conversation(conversation_id)
            expected_items = model_step_committed_action_items(
                records,
                invocation_id,
            )
            evidence_digest = model_step_action_evidence_digest(
                records,
                invocation_id,
            )
            if expected_items is None or evidence_digest is None:
                return None
            tracked = sorted(
                item.model_dump_json()
                for item in action_tracker.committed_items.values()
            )
            expected = sorted(
                item.model_dump_json() for item in expected_items
            )
            if expected != tracked:
                return None
            provider_context_id = adapter.recovery_context_id(session_id)
            if provider_context_id is None:
                return None
            history = await adapter.history(session_id)
            provider_item_ids = {
                item.item_id
                for item in history
                if item.kind is HarnessHistoryKind.TOOL_OUTPUT
                and item.item_id
            }
            session_items = await self._session_bridge.committed_items(
                session_id=session_id,
                user_id=user_id,
                channel=channel,
            )
            checkpoint = build_harness_recovery_checkpoint(
                invocation_id=invocation_id,
                conversation_id=conversation_id,
                source_submission_id=source_submission_id,
                backend=backend,
                provider_context_id=provider_context_id,
                provider_item_ids=provider_item_ids,
                action_evidence_digest=evidence_digest,
                expected_items=expected_items,
                session_items=session_items,
            )
            if checkpoint is None:
                return None
            return await lite_harness_recovery_context_store(
                self._workspace_dir,
            ).save(checkpoint)
        except Exception:  # pylint: disable=broad-except
            logger.exception(
                "Failed to checkpoint Harness recovery context",
            )
            return None

    async def _resolve_environment(
        self,
        *,
        backend: str,
        cwd: Path,
        settings: dict[str, Any],
        request_context: dict[str, Any],
    ) -> EnvironmentResolution | None:
        """Resolve a controlled Harness launch before provider dispatch."""
        raw_invocation_id = request_context.get("os_invocation_id")
        if raw_invocation_id is None:
            return None
        if self._environment_manager is None:
            raise RuntimeError(
                "controlled Harness invocation has no environment manager",
            )
        try:
            invocation_id = uuid.UUID(str(raw_invocation_id))
        except ValueError as exc:
            raise ValueError(
                "invalid Harness environment invocation identity",
            ) from exc
        conversation_id = request_context.get("os_conversation_id")
        return await self._environment_manager.resolve(
            backend,
            cwd,
            settings,
            invocation_id=invocation_id,
            conversation_id=(
                str(conversation_id) if conversation_id is not None else None
            ),
        )

    async def stop(self) -> None:
        """Stop every initialized adapter."""
        async with self._adapter_lock:
            for adapter in tuple(self._adapters.values()):
                await adapter.stop()
            self._adapters.clear()
            self._adapter_keys.clear()

    async def hydrate_session(
        self,
        *,
        backend: str,
        session_id: str,
        user_id: str,
        channel: str,
        settings: dict[str, Any] | None = None,
    ) -> None:
        """Recover an unmaterialized provider thread into QwenPaw."""
        if self._session_bridge is None:
            return
        if await self._session_bridge.has_history(
            session_id=session_id,
            user_id=user_id,
            channel=channel,
        ):
            return
        adapter = await self.adapter(backend, settings)
        history = await adapter.history(session_id)
        await self._session_bridge.hydrate(
            session_id=session_id,
            user_id=user_id,
            channel=channel,
            backend=backend,
            history=history,
        )

    @staticmethod
    def _content_from_request(
        request: Any,
    ) -> tuple[str, list[HarnessAttachment]]:
        parts: list[str] = []
        attachments: list[HarnessAttachment] = []
        for message in getattr(request, "input", None) or []:
            for content in getattr(message, "content", None) or []:
                if isinstance(content, str):
                    parts.append(content)
                    continue
                text = getattr(content, "text", None)
                if text:
                    parts.append(str(text))
                    continue
                content_type = getattr(content, "type", None)
                if content_type == ContentType.IMAGE:
                    raw_path = getattr(content, "image_url", None)
                    if raw_path:
                        attachments.append(
                            HarnessAttachment(
                                kind=HarnessAttachmentKind.IMAGE,
                                path=Path(str(raw_path)).expanduser(),
                                name=Path(str(raw_path)).name,
                            ),
                        )
                    continue
                path_field = None
                if content_type == ContentType.FILE:
                    path_field = "file_url"
                elif content_type == ContentType.AUDIO:
                    path_field = "data"
                elif content_type == ContentType.VIDEO:
                    path_field = "video_url"
                raw_path = (
                    getattr(content, path_field, None) if path_field else None
                )
                if raw_path:
                    attachments.append(
                        HarnessAttachment(
                            kind=HarnessAttachmentKind.FILE,
                            path=Path(str(raw_path)).expanduser(),
                            name=str(
                                getattr(content, "filename", None)
                                or Path(str(raw_path)).name,
                            ),
                        ),
                    )
        prompt = "\n".join(parts).strip()
        if not prompt and not attachments:
            raise ValueError(
                "Third-party agent requests require text or attachments",
            )
        return prompt, attachments

    @staticmethod
    def _parse_command(prompt: str) -> tuple[str, str]:
        if not prompt.startswith("/"):
            return "", ""
        first_line = prompt.splitlines()[0].strip()
        command, _, arguments = first_line[1:].partition(" ")
        return command.lower(), arguments.strip()

    @staticmethod
    async def _iter_events(
        events: list[Any],
    ) -> AsyncGenerator[Any, None]:
        for event in events:
            yield event


__all__ = ["HarnessRuntime"]
