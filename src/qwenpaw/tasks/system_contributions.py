# -*- coding: utf-8 -*-
"""Built-in Lite capabilities published through the shared catalog."""

from __future__ import annotations

import base64
import binascii
import mimetypes
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from ..delivery.channel import (
    SYSTEM_CHANNEL_DELIVERY_ID as _SYSTEM_CHANNEL_DELIVERY_ID,
)
from ..delivery.inbox import (
    SYSTEM_INBOX_DELIVERY_ID as _SYSTEM_INBOX_DELIVERY_ID,
)
from ..kernel.models import (
    CapabilityBundle,
    CapabilityContribution,
    CapabilityProviderKind,
    CostAccountingMode,
    JsonObject,
    PlanStep,
    Proposal,
    RestartPolicy,
    Run,
    RunnerSignal,
    RuntimeContext,
    SensorContext,
    TaskOrder,
)
from ..kernel.ports import ArtifactStore
from ..runtime.strategy_directives import (
    console_strategy_parameters,
    console_strategy_request_context,
)
from ..schemas import TextContent
from .artifacts import StoredArtifactEmitter
from .console_stream import ConsoleStreamCapture
from .context import legacy_request_context, runtime_context_from_order
from .harness_runner import HarnessTaskRunner
from .renderers import SafeArtifactRenderer
from .usage_scope import close_usage_scope_in_context

SYSTEM_PROVIDER_ID = "qwenpaw.system.tasks"
SYSTEM_CONSOLE_RUNNER_ID = f"{SYSTEM_PROVIDER_ID}.console-agent"
SYSTEM_CODEX_RUNNER_ID = f"{SYSTEM_PROVIDER_ID}.codex-harness"
SYSTEM_QODER_RUNNER_ID = f"{SYSTEM_PROVIDER_ID}.qoder-harness"
SYSTEM_BASIC_PLANNER_ID = f"{SYSTEM_PROVIDER_ID}.basic-planner"
SYSTEM_DEFAULT_STRATEGY_ID = f"{SYSTEM_PROVIDER_ID}.default-strategy"
SYSTEM_CODING_STRATEGY_ID = f"{SYSTEM_PROVIDER_ID}.coding-strategy"
SYSTEM_GOAL_STRATEGY_ID = f"{SYSTEM_PROVIDER_ID}.goal-strategy"
SYSTEM_MISSION_STRATEGY_ID = f"{SYSTEM_PROVIDER_ID}.mission-strategy"
SYSTEM_PROACTIVE_MEMORY_SENSOR_ID = (
    f"{SYSTEM_PROVIDER_ID}.proactive-memory-sensor"
)
SYSTEM_SAFE_ARTIFACT_RENDERER_ID = (
    f"{SYSTEM_PROVIDER_ID}.safe-artifact-renderer"
)
SYSTEM_LOCAL_SCHEDULER_ID = f"{SYSTEM_PROVIDER_ID}.local-durable-scheduler"
SYSTEM_CHANNEL_DELIVERY_ID = _SYSTEM_CHANNEL_DELIVERY_ID
SYSTEM_INBOX_DELIVERY_ID = _SYSTEM_INBOX_DELIVERY_ID
# Encodes [1, "promotion", "promotion", "promotion"] without secrets.
_PROMOTION_CHANNEL_ADDRESS = (
    "WzEsInByb21vdGlvbiIsInByb21vdGlvbiIsInByb21vdGlvbiJd"
)


class WorkspaceLike(Protocol):
    """Small workspace surface required by the Console Agent adapter."""

    agent_id: str
    workspace_dir: str | Path
    channel_manager: Any


WorkspaceResolver = Callable[[str], Awaitable[WorkspaceLike]]
ArtifactStoreFactory = Callable[[Path], ArtifactStore]


def _decode_inline_media(
    part: dict[str, Any],
) -> tuple[bytes, str] | None:
    """Decode supported inline media without persisting it in the Ledger."""
    part_type = str(part.get("type") or "")
    raw = ""
    media_type = ""
    if part_type == "image":
        raw = str(part.get("image_url") or "")
    elif part_type == "video":
        raw = str(part.get("video_url") or "")
    elif part_type == "audio":
        raw = str(part.get("data") or "")
        media_type = f"audio/{part.get('format') or 'mpeg'}"
    elif part_type == "file":
        raw = str(part.get("file_data") or part.get("file_url") or "")
        media_type = str(
            part.get("media_type") or "application/octet-stream",
        )
    if not raw:
        return None
    encoded = raw
    if raw.startswith("data:"):
        header, separator, encoded = raw.partition(",")
        if not separator or ";base64" not in header:
            return None
        media_type = header[5:].split(";", maxsplit=1)[0] or media_type
    elif part_type != "audio" and "file_data" not in part:
        return None
    try:
        content = base64.b64decode(
            "".join(encoded.split()),
            validate=True,
        )
    except (binascii.Error, ValueError):
        return None
    if not content or not media_type:
        return None
    return content, media_type


def _media_name(
    part: dict[str, Any],
    media_type: str,
    index: int,
) -> str:
    """Return a safe, deterministic display name for assistant media."""
    raw_name = str(part.get("filename") or "").replace("\\", "/")
    name = raw_name.rsplit("/", maxsplit=1)[-1].strip()
    if name not in {"", ".", ".."}:
        return name
    extension = mimetypes.guess_extension(media_type) or ""
    return f"assistant-media-{index}{extension}"


async def _completed_response_signals(
    capture: ConsoleStreamCapture,
    context: RuntimeContext,
) -> tuple[RunnerSignal, ...]:
    """Externalize inline media and commit one public response event."""
    if not capture.completed_content:
        return ()
    artifact_signals: list[RunnerSignal] = []
    content: list[dict[str, Any]] = []
    artifact_refs = []
    evidence_refs = []
    for index, part in enumerate(capture.completed_content, start=1):
        decoded = _decode_inline_media(part)
        if decoded is None:
            content.append(dict(part))
            continue
        media, media_type = decoded
        name = _media_name(part, media_type, index)
        artifact_signal = await context.artifact_emitter.emit(
            kind=f"conversation.{part['type']}",
            media_type=media_type,
            content=media,
            name=name,
            evidence_claim="Assistant public media",
            metadata={
                "delivery_disposition": "embedded",
                "delivery_content_type": str(part["type"]),
            },
        )
        artifact_signals.append(artifact_signal)
        artifact = artifact_signal.artifact_refs[0]
        artifact_refs.append(artifact)
        evidence_refs.extend(artifact_signal.evidence_refs)
        content.append(
            {
                "type": str(part["type"]),
                "artifact_id": str(artifact.artifact_id),
                "filename": name,
            },
        )
    completed = RunnerSignal(
        event_type="conversation.assistant.completed",
        source=SYSTEM_CONSOLE_RUNNER_ID,
        payload={
            "role": "assistant",
            "text": capture.completed_text,
            "content": content,
        },
        artifact_refs=tuple(artifact_refs),
        evidence_refs=tuple(evidence_refs),
    )
    return (*artifact_signals, completed)


class BasicTaskPlanner:
    """Deterministic built-in planner used until a richer planner is chosen."""

    planner_id = SYSTEM_BASIC_PLANNER_ID

    async def health_check(self) -> bool:
        """Report that the stateless planner is ready."""
        return True

    async def plan(self, order: TaskOrder) -> tuple[PlanStep, ...]:
        """Create one executable step without inventing hidden work."""
        return (
            PlanStep(
                title="Execute task objective",
                objective=order.objective,
            ),
        )


class ProactiveMemorySensor:
    """Adapt bounded proactive cognition output to the Sensor contract."""

    sensor_id = SYSTEM_PROACTIVE_MEMORY_SENSOR_ID

    async def health_check(self) -> bool:
        """Report that the stateless adapter is ready."""
        return True

    async def propose(self) -> tuple[Proposal, ...]:
        """Return no work when the host supplies no cognition trigger."""
        return ()

    async def propose_context(
        self,
        context: SensorContext,
    ) -> tuple[Proposal, ...]:
        """Validate proposals supplied by the built-in cognition source."""
        raw_proposals = context.trigger_payload.get("proposals", [])
        if not isinstance(raw_proposals, list):
            raise ValueError("proactive proposals must be a JSON array")
        return tuple(
            Proposal.model_validate(raw_proposal)
            for raw_proposal in raw_proposals
        )


class DefaultRuntimeStrategy:
    """Preserve the existing Agent runtime behavior through one slot."""

    strategy_id = SYSTEM_DEFAULT_STRATEGY_ID

    async def health_check(self) -> bool:
        """Report that the stateless default strategy is ready."""
        return True

    async def prepare(
        self,
        _context: RuntimeContext,
        _order: TaskOrder,
    ) -> JsonObject:
        """Select the existing default Agent mode without extra mutation."""
        return {"mode": "default"}


class CodingRuntimeStrategy:
    """Enable Coding Mode for this task attempt without changing config."""

    strategy_id = SYSTEM_CODING_STRATEGY_ID

    async def health_check(self) -> bool:
        """Report that the stateless strategy is ready."""
        return True

    async def prepare(
        self,
        _context: RuntimeContext,
        _order: TaskOrder,
    ) -> JsonObject:
        """Request the existing Coding Mode for one runtime invocation."""
        return {"mode": "coding", "activation": "request"}


class GoalRuntimeStrategy:
    """Activate the existing Goal Mode through its public command."""

    strategy_id = SYSTEM_GOAL_STRATEGY_ID

    async def health_check(self) -> bool:
        """Report that the stateless strategy is ready."""
        return True

    async def prepare(
        self,
        _context: RuntimeContext,
        _order: TaskOrder,
    ) -> JsonObject:
        """Return a bounded Goal Mode activation directive."""
        return {
            "mode": "goal",
            "activation": "command",
            "command": "goal",
        }


class MissionRuntimeStrategy:
    """Activate the existing Mission Mode through its public command."""

    strategy_id = SYSTEM_MISSION_STRATEGY_ID

    async def health_check(self) -> bool:
        """Report that the stateless strategy is ready."""
        return True

    async def prepare(
        self,
        _context: RuntimeContext,
        _order: TaskOrder,
    ) -> JsonObject:
        """Return a bounded Mission Mode activation directive."""
        return {
            "mode": "mission",
            "activation": "command",
            "command": "mission",
        }


class ConsoleAgentRunner:
    """Adapt the existing Console Agent stream to the shared runner port."""

    runner_id = SYSTEM_CONSOLE_RUNNER_ID
    cost_accounting = CostAccountingMode.UNKNOWN

    def __init__(
        self,
        resolve_workspace: WorkspaceResolver,
        artifact_store_factory: ArtifactStoreFactory | None = None,
    ) -> None:
        self._resolve_workspace = resolve_workspace
        self._artifact_store_factory = artifact_store_factory

    async def health_check(self) -> bool:
        """Validate construction without starting an Agent session."""
        return True

    async def execute(
        self,
        order: TaskOrder,
        run: Run,
    ):
        """Adapt direct legacy calls to the typed runtime entrypoint."""
        context = runtime_context_from_order(order, run)
        async for signal in self.execute_context(order, run, context):
            yield signal

    async def execute_context(
        self,
        order: TaskOrder,
        run: Run,
        context: RuntimeContext,
    ):
        """Run one task through Console with immutable typed context."""
        del run
        workspace = await self._resolve_workspace(context.agent_id)
        console_channel = await workspace.channel_manager.get_channel(
            "console",
        )
        if console_channel is None:
            raise RuntimeError("Console runtime unavailable")

        yield RunnerSignal(
            event_type="conversation.user",
            payload={"role": "user", "text": order.objective},
        )
        yield RunnerSignal(
            event_type="runner.dispatched",
            payload={
                "runtime": "console-agent",
                "approval_policy": context.approval_level.value,
            },
        )
        yield RunnerSignal(event_type="runner.generating")

        request_context = legacy_request_context(context)
        strategy_parameters = console_strategy_parameters(context.strategy)
        request_context.update(
            console_strategy_request_context(context.strategy),
        )
        objective = order.objective
        activation = (
            strategy_parameters.get("activation")
            if strategy_parameters is not None
            else None
        )
        command = (
            strategy_parameters.get("command")
            if strategy_parameters is not None
            else None
        )
        if activation == "command" and command in {"goal", "mission"}:
            objective = f"/{command} {objective}"
        payload = {
            "channel_id": "console",
            "sender_id": "qwenpaw-task-workbench",
            "content_parts": [TextContent(text=objective)],
            "message_metadata": {"task_id": str(order.task_id)},
            "meta": {
                "session_id": context.session_id,
                "user_id": "qwenpaw-task-workbench",
                "request_context": request_context,
            },
        }
        capture = ConsoleStreamCapture()
        try:
            async for line in console_channel.stream_one(payload):
                for signal in capture.feed(line):
                    yield signal
        finally:
            close_usage_scope_in_context(request_context)
            cancellation = context.cancellation
            if cancellation is not None and cancellation.cancelled:
                from ..app.approvals import get_approval_service

                approval_service = get_approval_service()
                await approval_service.cancel_all_pending_by_root_session(
                    context.session_id,
                )
        for signal in capture.finish():
            yield signal

        for signal in await _completed_response_signals(capture, context):
            yield signal

        if not capture.completed_text:
            return
        emitter = context.artifact_emitter
        if self._artifact_store_factory is not None:
            emitter = StoredArtifactEmitter(
                self._artifact_store_factory(Path(context.project_dir)),
                producer=self.runner_id,
            )
        yield await emitter.emit(
            kind="agent.response",
            media_type="text/markdown",
            content=capture.completed_text.encode("utf-8"),
            name="task-result.md",
            evidence_claim="Agent final response",
            metadata={
                "task_id": str(order.task_id),
                "delivery_disposition": "result_projection",
            },
        )


SYSTEM_CAPABILITY_BUNDLE = CapabilityBundle(
    provider_id=SYSTEM_PROVIDER_ID,
    provider_kind=CapabilityProviderKind.SYSTEM,
    version="1.7.0",
    restart_policy=RestartPolicy.HOT,
    contributions=(
        CapabilityContribution(
            contribution_id="basic-planner",
            slot="planner",
            entrypoint=("qwenpaw.tasks.system_contributions:BasicTaskPlanner"),
        ),
        CapabilityContribution(
            contribution_id="proactive-memory-sensor",
            slot="sensor",
            entrypoint=(
                "qwenpaw.tasks.system_contributions:ProactiveMemorySensor"
            ),
        ),
        CapabilityContribution(
            contribution_id="console-agent",
            slot="runner",
            entrypoint=(
                "qwenpaw.tasks.system_contributions:ConsoleAgentRunner"
            ),
        ),
        CapabilityContribution(
            contribution_id="codex-harness",
            slot="harness.runner",
            entrypoint=("qwenpaw.tasks.harness_runner:HarnessTaskRunner"),
            metadata={"label": "Codex", "backend": "codex"},
        ),
        CapabilityContribution(
            contribution_id="qoder-harness",
            slot="harness.runner",
            entrypoint=("qwenpaw.tasks.harness_runner:HarnessTaskRunner"),
            metadata={"label": "Qoder", "backend": "qoder"},
        ),
        CapabilityContribution(
            contribution_id="default-strategy",
            slot="strategy",
            entrypoint=(
                "qwenpaw.tasks.system_contributions:DefaultRuntimeStrategy"
            ),
            metadata={"label": "Default", "mode": "default"},
        ),
        CapabilityContribution(
            contribution_id="safe-artifact-renderer",
            slot="artifact.renderer",
            entrypoint=(
                "qwenpaw.tasks.system_contributions:SafeArtifactRenderer"
            ),
        ),
        CapabilityContribution(
            contribution_id="coding-strategy",
            slot="strategy",
            entrypoint=(
                "qwenpaw.tasks.system_contributions:CodingRuntimeStrategy"
            ),
            metadata={"label": "Coding", "mode": "coding"},
        ),
        CapabilityContribution(
            contribution_id="goal-strategy",
            slot="strategy",
            entrypoint=(
                "qwenpaw.tasks.system_contributions:GoalRuntimeStrategy"
            ),
            metadata={"label": "Goal", "mode": "goal"},
        ),
        CapabilityContribution(
            contribution_id="mission-strategy",
            slot="strategy",
            entrypoint=(
                "qwenpaw.tasks.system_contributions:MissionRuntimeStrategy"
            ),
            metadata={"label": "Mission", "mode": "mission"},
        ),
        CapabilityContribution(
            contribution_id="local-durable-scheduler",
            slot="scheduler.provider",
            entrypoint=("qwenpaw.scheduling:HostSchedulerProvider"),
            metadata={"label": "Local durable scheduler"},
        ),
        CapabilityContribution(
            contribution_id="channel-delivery",
            slot="delivery.adapter",
            entrypoint=(
                "qwenpaw.delivery.channel:SystemChannelDeliveryAdapter"
            ),
            metadata={
                "label": "QwenPaw channel delivery",
                "delivery_addresses": [_PROMOTION_CHANNEL_ADDRESS],
            },
        ),
        CapabilityContribution(
            contribution_id="inbox-delivery",
            slot="delivery.adapter",
            entrypoint=("qwenpaw.delivery.inbox:SystemInboxDeliveryAdapter"),
            metadata={
                "label": "QwenPaw local Inbox delivery",
                "delivery_addresses": ["local"],
            },
        ),
    ),
)


def system_contribution_factory(
    resolve_workspace: WorkspaceResolver,
    artifact_store_factory: ArtifactStoreFactory | None = None,
):
    """Build the factory used by the shared generation registry."""

    def create(declaration: CapabilityContribution) -> object:
        if declaration.contribution_id == "basic-planner":
            return BasicTaskPlanner()
        if declaration.contribution_id == "proactive-memory-sensor":
            return ProactiveMemorySensor()
        strategy_factories = {
            "default-strategy": DefaultRuntimeStrategy,
            "coding-strategy": CodingRuntimeStrategy,
            "goal-strategy": GoalRuntimeStrategy,
            "mission-strategy": MissionRuntimeStrategy,
        }
        strategy_factory = strategy_factories.get(
            declaration.contribution_id,
        )
        if strategy_factory is not None:
            return strategy_factory()
        if declaration.contribution_id == "console-agent":
            return ConsoleAgentRunner(
                resolve_workspace,
                artifact_store_factory,
            )
        harness_backends = {
            "codex-harness": (SYSTEM_CODEX_RUNNER_ID, "codex"),
            "qoder-harness": (SYSTEM_QODER_RUNNER_ID, "qoder"),
        }
        harness = harness_backends.get(declaration.contribution_id)
        if harness is not None:
            runner_id, backend = harness
            return HarnessTaskRunner(
                runner_id,
                backend,
                resolve_workspace,
                artifact_store_factory,
            )
        if declaration.contribution_id == "safe-artifact-renderer":
            implementation: object = SafeArtifactRenderer(
                SYSTEM_SAFE_ARTIFACT_RENDERER_ID,
            )
        elif declaration.contribution_id == "local-durable-scheduler":
            from ..scheduling import HostSchedulerProvider

            implementation = HostSchedulerProvider(
                SYSTEM_LOCAL_SCHEDULER_ID,
            )
        elif declaration.contribution_id == "channel-delivery":
            from ..delivery import SystemChannelDeliveryAdapter

            implementation = SystemChannelDeliveryAdapter(
                resolve_workspace,
            )
        elif declaration.contribution_id == "inbox-delivery":
            from ..delivery import SystemInboxDeliveryAdapter

            implementation = SystemInboxDeliveryAdapter()
        else:
            raise LookupError(declaration.contribution_id)
        return implementation

    return create
