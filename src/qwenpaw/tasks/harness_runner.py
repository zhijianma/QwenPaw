# -*- coding: utf-8 -*-
"""Adapt built-in Harness backends to the stable Task Runner contract."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Awaitable

from ..harnesses.events import HarnessEvent, HarnessEventKind
from ..kernel.models import (
    CostAccountingMode,
    Run,
    RunnerPreflightRequest,
    RunnerPreflightResult,
    RunnerSignal,
    RuntimeContext,
    TaskOrder,
)
from ..kernel.ports import ArtifactStore
from .artifacts import StoredArtifactEmitter
from .context import legacy_request_context, runtime_context_from_order

WorkspaceResolver = Callable[[str], Awaitable[Any]]
ArtifactStoreFactory = Callable[[Path], ArtifactStore]


class HarnessRunnerError(RuntimeError):
    """Raised when a Harness stream cannot reach a valid terminal event."""


class HarnessSignalProjector:
    """Project provider-neutral Harness events into durable Task signals."""

    def __init__(self, source: str) -> None:
        self._source = source
        self._responding = False
        self._completed = False
        self._text_parts: list[str] = []

    @property
    def completed_text(self) -> str:
        """Return the complete visible assistant response."""
        return "".join(self._text_parts)

    @property
    def completed(self) -> bool:
        """Return whether the backend emitted its terminal marker."""
        return self._completed

    def project(self, event: HarnessEvent) -> tuple[RunnerSignal, ...]:
        """Translate one normalized event without sequencing it."""
        if self._completed:
            raise HarnessRunnerError("Harness emitted data after completion")
        if event.kind is HarnessEventKind.TEXT_DELTA:
            return self._text(event)
        if event.kind is HarnessEventKind.REASONING_DELTA:
            return (
                RunnerSignal(
                    event_type="runner.reasoning.delta",
                    source=self._source,
                    payload={
                        "text": event.text,
                        "item_id": event.item_id,
                    },
                ),
            )
        if event.kind is HarnessEventKind.TOOL_STARTED:
            return (
                RunnerSignal(
                    event_type="tool.started",
                    source=self._source,
                    payload=self._tool_payload(event),
                ),
            )
        if event.kind is HarnessEventKind.TOOL_PROGRESS:
            return (
                RunnerSignal(
                    event_type="tool.progress",
                    source=self._source,
                    payload=self._tool_payload(event),
                ),
            )
        if event.kind is HarnessEventKind.TOOL_COMPLETED:
            return (
                RunnerSignal(
                    event_type="tool.completed",
                    source=self._source,
                    payload=self._tool_payload(event),
                ),
            )
        if event.kind is HarnessEventKind.ERROR:
            raise HarnessRunnerError(event.text or "Harness turn failed")
        if event.kind is HarnessEventKind.CANCELLED:
            raise asyncio.CancelledError("Harness turn was cancelled")
        if event.kind is HarnessEventKind.COMPLETED:
            self._completed = True
            text = self.completed_text
            return (
                (
                    RunnerSignal(
                        event_type="conversation.assistant.completed",
                        source=self._source,
                        payload={"role": "assistant", "text": text},
                    ),
                )
                if text
                else ()
            )
        raise HarnessRunnerError(f"unsupported Harness event: {event.kind}")

    def _text(self, event: HarnessEvent) -> tuple[RunnerSignal, ...]:
        if not event.text:
            return ()
        self._text_parts.append(event.text)
        signals: list[RunnerSignal] = []
        if not self._responding:
            self._responding = True
            signals.append(
                RunnerSignal(
                    event_type="runner.responding",
                    source=self._source,
                ),
            )
        signals.append(
            RunnerSignal(
                event_type="conversation.assistant.delta",
                source=self._source,
                payload={"role": "assistant", "text": event.text},
            ),
        )
        return tuple(signals)

    @staticmethod
    def _tool_payload(event: HarnessEvent) -> dict[str, Any]:
        if not event.item_id:
            raise HarnessRunnerError("Harness tool event has no call identity")
        if (
            event.kind
            in {
                HarnessEventKind.TOOL_STARTED,
                HarnessEventKind.TOOL_COMPLETED,
            }
            and not event.tool_name
        ):
            raise HarnessRunnerError("Harness tool event has no tool name")
        payload = {
            **event.data,
            "call_id": event.item_id,
            "name": event.tool_name,
            "text": event.text,
        }
        if event.kind is HarnessEventKind.TOOL_STARTED:
            payload["usage_accounted"] = False
        return payload


class HarnessTaskRunner:
    """Run Codex or Qoder through the same contextual Task Runner port."""

    def __init__(
        self,
        runner_id: str,
        backend: str,
        resolve_workspace: WorkspaceResolver,
        artifact_store_factory: ArtifactStoreFactory | None = None,
    ) -> None:
        self._runner_id = runner_id
        self._backend = backend
        self._resolve_workspace = resolve_workspace
        self._artifact_store_factory = artifact_store_factory

    @property
    def runner_id(self) -> str:
        """Return the stable catalog capability identity."""
        return self._runner_id

    @property
    def cost_accounting(self) -> CostAccountingMode:
        """Harness providers do not yet expose trusted price telemetry."""
        return CostAccountingMode.UNKNOWN

    async def health_check(self) -> bool:
        """Validate the stateless adapter without starting a provider."""
        return True

    async def preflight(
        self,
        request: RunnerPreflightRequest,
    ) -> RunnerPreflightResult:
        """Describe Harness execution without opening its backend."""
        if request.runner_id != self.runner_id:
            raise ValueError("runner preflight identity mismatch")
        return RunnerPreflightResult(
            runner_id=self.runner_id,
            slot=request.slot,
            registry_generation=request.registry_generation,
            contextual=True,
            cost_accounting=self.cost_accounting,
        )

    async def execute(
        self,
        order: TaskOrder,
        run: Run,
    ) -> AsyncIterator[RunnerSignal]:
        """Adapt direct legacy calls to the typed runtime entrypoint."""
        context = runtime_context_from_order(order, run)
        async for signal in self.execute_context(order, run, context):
            yield signal

    async def execute_context(
        self,
        order: TaskOrder,
        run: Run,
        context: RuntimeContext,
    ) -> AsyncIterator[RunnerSignal]:
        """Run one external agent turn through the shared Task envelope."""
        if order.task_id != run.task_id or context.run_id != run.run_id:
            raise ValueError("Harness runtime context does not match run")
        workspace = await self._resolve_workspace(context.agent_id)
        runtime = getattr(workspace, "harness_runtime", None)
        if runtime is None:
            raise RuntimeError("Harness runtime unavailable")

        yield RunnerSignal(
            event_type="conversation.user",
            source=self.runner_id,
            payload={"role": "user", "text": order.objective},
        )
        yield RunnerSignal(
            event_type="runner.dispatched",
            source=self.runner_id,
            payload={
                "runtime": self._backend,
                "slot": "harness.runner",
                "approval_policy": context.approval_level.value,
            },
        )
        yield RunnerSignal(
            event_type="runner.generating",
            source=self.runner_id,
        )

        settings = self._settings(workspace, context)
        projector = HarnessSignalProjector(self.runner_id)
        async for event in runtime.task_events(
            backend=self._backend,
            session_id=context.session_id,
            prompt=order.objective,
            cwd=Path(context.project_dir).resolve(strict=False),
            settings=settings,
        ):
            context.cancellation.raise_if_cancelled()
            for signal in projector.project(event):
                yield signal
        if not projector.completed:
            raise HarnessRunnerError(
                f"{self._backend} ended without a terminal event",
            )

        if projector.completed_text:
            emitter = context.artifact_emitter
            if self._artifact_store_factory is not None:
                emitter = StoredArtifactEmitter(
                    self._artifact_store_factory(Path(context.project_dir)),
                    producer=self.runner_id,
                )
            yield await emitter.emit(
                kind="agent.response",
                media_type="text/markdown",
                content=projector.completed_text.encode("utf-8"),
                name="task-result.md",
                evidence_claim=f"{self._backend} final response",
                metadata={
                    "task_id": str(order.task_id),
                    "backend": self._backend,
                    "delivery_disposition": "result_projection",
                },
            )

    def _settings(
        self,
        workspace: Any,
        context: RuntimeContext,
    ) -> dict[str, Any]:
        config = getattr(workspace, "config", None)
        settings = (
            dict(getattr(config, "backend_settings", {}) or {})
            if getattr(config, "backend", None) == self._backend
            else {}
        )
        request_context = legacy_request_context(
            context,
            share_usage_scope=False,
        )
        request_context.update(
            {
                "agent_id": context.agent_id,
                "session_id": context.session_id,
                "user_id": "qwenpaw-task-workbench",
                "channel": "console",
                "cancellation_reason": (
                    context.cancellation.reason
                    or "QwenPaw Task execution cancelled"
                ),
            },
        )
        settings["_request_context"] = request_context
        return settings


__all__ = [
    "HarnessRunnerError",
    "HarnessSignalProjector",
    "HarnessTaskRunner",
]
