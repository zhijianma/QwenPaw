# -*- coding: utf-8 -*-
"""Nine-phase request orchestration.

Delegates to:

* ``Envelope``       — SSE state machine
* ``AgentBuilder``   — per-request agent assembly
* ``AgentExecutor``  — heartbeat-wrapped reply stream

Insertable features are resolved from generation-pinned Provider sessions.
Compatibility providers adapt the per-workspace Hook Registry and Agent Modes.
The fixed build and execute steps are the only agent-touching code.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, AsyncGenerator

from ..agents.acp.meta import ACP_EPHEMERAL_META_KEY
from ..exceptions import ConfigurationException
from ..utils.daily_telemetry import record_agent_activity
from .builder import AgentBuilder  # pylint: disable=unused-import
from .assembly import RuntimeAssemblyFactory, capability_registry_for
from .envelope import Envelope
from .executor import AgentExecutor
from .hooks import HookContext
from .message_convert import _get_last_user_text, _request_input_to_msgs
from .model_calls import ModelStepRecoveryError
from ..kernel.models import (
    CommandDisposition,
    HookDisposition,
    HookOutcome,
    LifecyclePhase,
)
from ..kernel import (
    ModelStepContextCheckpoint,
    ModelStepContinuationStatus,
    SubmissionStatus,
    TurnSubmissionRequest,
)
from .phases import Phase

logger = logging.getLogger(__name__)
_CANCEL_HOOK_TIMEOUT_SECONDS = 2.0


class Runtime:
    """Per-workspace request orchestrator.

    One ``Runtime`` instance per ``Workspace``.  ``run()`` is called once
    per ``AgentRequest`` and yields SSE envelope objects identical to
    what the legacy ``Runner.stream_query`` produced.
    """

    def __init__(
        self,
        *,
        workspace: Any,
        app_services: Any,
    ) -> None:
        self.workspace = workspace
        self.app_services = app_services

    async def run(  # pylint: disable=too-many-branches,too-many-statements
        self,
        request: Any,
    ) -> AsyncGenerator[Any, None]:
        """Nine-phase lifecycle orchestration."""
        request = self._normalize(request)
        ctx = self._build_context(request)
        envelope = Envelope(session_id=ctx.session_id)
        ctx.envelope = envelope
        skip_agent = False
        assembly = None
        ctx.extras["invocation_terminal_status"] = SubmissionStatus.FAILED

        try:
            (
                task_invocation_id,
                task_correlation_id,
            ) = self._task_causal_identity(request)
            assembly = await RuntimeAssemblyFactory(
                capability_registry_for(self.workspace),
            ).open(
                agent_id=ctx.agent_id,
                chat_id=self._conversation_id(request),
                session_id=ctx.session_id,
                root_agent_id=ctx.root_agent_id,
                root_session_id=ctx.root_session_id,
                workspace_dir=ctx.workspace_dir or ".",
                selection_overrides=getattr(
                    getattr(self.workspace, "config", None),
                    "capability_selection",
                    None,
                ),
                registry_generation=self._task_registry_generation(request),
                invocation_id=task_invocation_id,
                correlation_id=task_correlation_id,
            )
            ctx.invocation_scope = assembly.scope
            ctx.extras["runtime_assembly"] = assembly
            control_service = getattr(
                self.workspace,
                "invocation_control",
                None,
            )
            interaction_service = getattr(
                self.workspace,
                "interaction_service",
                None,
            )
            if interaction_service is not None:
                ctx.extras["interaction_service"] = interaction_service
            legacy_approval_compatibility = getattr(
                self.workspace,
                "legacy_approval_compatibility",
                None,
            )
            if legacy_approval_compatibility is not None:
                ctx.extras["legacy_approval_compatibility"] = (
                    legacy_approval_compatibility
                )
            resource_wait_service = getattr(
                self.workspace,
                "model_resource_wait_service",
                None,
            )
            if resource_wait_service is not None:
                ctx.extras["model_resource_wait_service"] = (
                    resource_wait_service
                )
            if control_service is not None:
                ctx.extras["invocation_control_service"] = control_service
                await self._open_invocation_control(
                    ctx,
                    request,
                    assembly.scope.invocation_id,
                    control_service,
                )
            ctx.extras["hook_session"] = await self._open_hook_session(
                ctx,
                assembly,
            )
            ctx.extras["stop_gate_session"] = (
                await self._open_stop_gate_session(ctx, assembly)
            )
            ctx.extras["agent_mode_session"] = (
                await self._open_agent_mode_session(ctx, assembly)
            )
            ctx.extras["command_session"] = await self._open_command_session(
                ctx,
                assembly,
            )

            # --- [phase 1] PRE_DISPATCH ---
            r = await self._run_hook_phase(ctx, Phase.PRE_DISPATCH)
            if r.disposition == HookDisposition.SHORT_CIRCUIT:
                async for ev in envelope.from_msg(self._hook_message(r)):
                    yield ev
                ctx.extras["invocation_terminal_status"] = (
                    SubmissionStatus.SUCCEEDED
                )
                return
            if r.disposition == HookDisposition.SKIP_AGENT:
                skip_agent = True

            # --- [fixed 1] slash command dispatch ---
            text = _get_last_user_text(ctx.input_msgs)
            command_session = ctx.extras["command_session"]
            command_result = await command_session.dispatch(text or "")
            if command_result.disposition == CommandDisposition.RESPOND:
                from agentscope.message import Msg, TextBlock

                message = command_result.message
                if message is None:
                    raise TypeError("respond command result has no message")
                command_msg = Msg(
                    name="assistant",
                    role="assistant",
                    content=[TextBlock(type="text", text=message.text)],
                    metadata=message.metadata,
                )
                async for ev in envelope.from_msg(command_msg):
                    yield ev
                skip_agent = True
            else:
                # --- [phase 2] POST_DISPATCH ---
                r = await self._run_hook_phase(ctx, Phase.POST_DISPATCH)
                if r.disposition == HookDisposition.SHORT_CIRCUIT:
                    async for ev in envelope.from_msg(self._hook_message(r)):
                        yield ev
                    skip_agent = True
                elif r.disposition == HookDisposition.SKIP_AGENT:
                    skip_agent = True

            if not skip_agent:
                # --- [phase 3] PRE_AGENT_BUILD ---
                r = await self._run_hook_phase(ctx, Phase.PRE_AGENT_BUILD)
                if r.disposition == HookDisposition.SHORT_CIRCUIT:
                    async for ev in envelope.from_msg(self._hook_message(r)):
                        yield ev
                    skip_agent = True
                elif r.disposition == HookDisposition.SKIP_AGENT:
                    skip_agent = True

            if not skip_agent:
                # --- [fixed 2] build agent ---
                factory_id = assembly.scope.selection.agent_factory_id
                agent_factory = assembly.require(
                    factory_id,
                    "agent.factory",
                )
                if getattr(agent_factory, "factory_id", None) != factory_id:
                    raise TypeError(
                        f"agent factory '{factory_id}' returned an "
                        "implementation with a mismatched identity",
                    )
                build_agent = getattr(agent_factory, "build", None)
                if not callable(build_agent):
                    raise TypeError(
                        f"agent factory '{factory_id}' does not implement "
                        "build()",
                    )
                ctx.agent = await build_agent(ctx, self.app_services)
                try:
                    setattr(
                        ctx.agent,
                        "_stop_gate_session",
                        ctx.extras["stop_gate_session"],
                    )
                except (AttributeError, TypeError):
                    logger.debug(
                        "runtime: agent does not accept stop-gate session",
                    )
                await self._start_modes(ctx)
                await self._start_stop_gates(ctx)

                # --- [phase 4] POST_AGENT_BUILD ---
                await self._run_hook_phase(ctx, Phase.POST_AGENT_BUILD)

                # --- [phase 5] PRE_EXECUTE ---
                r = await self._run_hook_phase(ctx, Phase.PRE_EXECUTE)
                if r.disposition == HookDisposition.SHORT_CIRCUIT:
                    async for ev in envelope.from_msg(self._hook_message(r)):
                        yield ev
                    skip_agent = True
                elif r.disposition == HookDisposition.SKIP_AGENT:
                    skip_agent = True

            if not skip_agent:
                self._apply_context_injections(ctx)
                # --- [fixed 3] execute agent ---
                async for ev in envelope.emit_response_created():
                    yield ev
                executor = AgentExecutor(ctx.agent, envelope)
                logger.debug(
                    "Agent input: %s",
                    _get_last_user_text(
                        ctx.input_msgs,
                    )
                    or "(empty)",
                )
                await record_agent_activity()
                async for ev in executor.run(ctx.input_msgs):
                    yield ev

            # --- [phase 6] POST_RESPONSE ---
            await self._run_hook_phase(ctx, Phase.POST_RESPONSE)

            # Finalize envelope (complete message + response).
            async for ev in envelope.finalize():
                yield ev
            ctx.extras["invocation_terminal_status"] = (
                SubmissionStatus.SUCCEEDED
            )

        except (asyncio.CancelledError, KeyboardInterrupt) as e:
            ctx.error = e
            ctx.extras["invocation_terminal_status"] = (
                SubmissionStatus.INTERRUPTED
            )
            # Every selected system/plugin Provider observes cancellation
            # through its generation-pinned session. Each hook is bounded
            # and isolated; Host persistence remains authoritative below.
            await self._run_cancel_phase(ctx)

            # The Task's _must_cancel flag may still be True after
            # catching CancelledError, causing the next await to raise
            # CancelledError again.  Wrap ON_ERROR hooks so that
            # cancel_envelope is always yielded — the frontend SDK
            # needs a terminal {object:response} event to
            # exit loading state.
            try:
                await self._run_hook_phase(ctx, Phase.ON_ERROR)
            except asyncio.CancelledError:
                logger.debug(
                    "ON_ERROR hooks skipped due to asyncio "
                    "re-cancellation (session=%s)",
                    getattr(ctx, "session_id", ""),
                )

            # Persist agent state so the interrupted turn is not lost.
            # asyncio.shield protects the save from task re-cancellation.
            await self._try_save_on_cancel(ctx)

            async for ev in envelope.cancel_envelope():
                yield ev
            raise
        except ConfigurationException as e:
            ctx.error = e
            logger.info(
                "runtime: configuration required session=%s code=%s",
                getattr(ctx, "session_id", ""),
                e.error_code or "CONFIGURATION_REQUIRED",
            )
            async for ev in envelope.error_envelope(
                e.message or str(e),
                e.error_code or "CONFIGURATION_REQUIRED",
            ):
                yield ev
            raise
        except ModelStepRecoveryError as e:
            ctx.error = e
            saved_state = await self._try_save_on_cancel(
                ctx,
                include_partial=False,
            )
            await self._checkpoint_model_step_context(
                ctx,
                e,
                saved_state,
            )
            logger.warning(
                "runtime: partial model step reached recovery boundary "
                "session=%s continuation=%s status=%s",
                getattr(ctx, "session_id", ""),
                e.continuation_id,
                e.status.value,
            )
            await self._run_hook_phase(ctx, Phase.ON_ERROR)
            exhausted = (
                e.status is ModelStepContinuationStatus.RECOVERY_EXHAUSTED
            )
            async for ev in envelope.error_envelope(
                (
                    "Automatic model-step recovery is exhausted."
                    if exhausted
                    else "The interrupted model step reached a durable "
                    "recovery boundary."
                ),
                (
                    "MODEL_STEP_RECOVERY_EXHAUSTED"
                    if exhausted
                    else "MODEL_STEP_RECOVERY_SCHEDULED"
                ),
            ):
                yield ev
            raise
        except BaseException as e:
            ctx.error = e
            await self._try_save_on_cancel(ctx)
            logger.error(
                "runtime: unhandled error session=%s: %s",
                getattr(ctx, "session_id", ""),
                e,
                exc_info=True,
            )
            await self._run_hook_phase(ctx, Phase.ON_ERROR)
            err_text = ctx.extras.get(
                "_error_text",
                str(e) or e.__class__.__name__,
            )
            err_code = ctx.extras.get(
                "_error_code",
                e.__class__.__name__,
            )
            async for ev in envelope.error_envelope(
                err_text,
                err_code,
            ):
                yield ev
            raise
        finally:
            await self._finalize_runtime(ctx, assembly)

    # ----------------------------------------------------------------- helpers

    @staticmethod
    async def _open_invocation_control(
        ctx: HookContext,
        request: Any,
        invocation_id: uuid.UUID,
        control_service: Any,
    ) -> None:
        """Open durable Chat control only with a server ChatSpec identity."""
        request_context = getattr(request, "request_context", None) or {}
        scope = getattr(ctx, "invocation_scope", None)
        conversation_id = getattr(scope, "conversation_id", None)
        idempotency_key = request_context.get(
            "os_submission_idempotency_key",
        )
        if not conversation_id or not idempotency_key:
            ctx.extras["steering_session"] = (
                await control_service.open_steering(invocation_id)
            )
            return

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
        content = _get_last_user_text(ctx.input_msgs)
        if not content:
            content = "[non-text user input]"
        submission = TurnSubmissionRequest(
            agent_id=ctx.agent_id,
            conversation_id=str(conversation_id),
            priority=priority,
            content=content,
            artifact_refs=artifact_refs,
            request_context={
                "channel": str(getattr(request, "channel", "") or ""),
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
            lease = await control_service.begin_submitted_turn(
                submission_id,
                invocation_id=invocation_id,
                agent_id=ctx.agent_id,
                conversation_id=str(conversation_id),
            )
        else:
            lease = await control_service.begin_turn(
                submission,
                invocation_id=invocation_id,
            )
        ctx.extras["invocation_control_lease"] = lease
        ctx.extras["steering_session"] = lease.steering
        runtime_task = asyncio.current_task()
        if runtime_task is None:
            raise RuntimeError("runtime invocation has no owning asyncio task")

        async def cancel_runtime_children() -> int:
            interaction_service = ctx.extras.get("interaction_service")
            interaction_error: Exception | None = None
            if interaction_service is not None:
                try:
                    await interaction_service.cancel_invocation(
                        invocation_id,
                        detail="invocation interrupt requested",
                        include_non_blocking=False,
                    )
                except Exception as error:  # pylint: disable=broad-except
                    interaction_error = error

            coordinator = getattr(ctx.app_services, "tool_coordinator", None)
            cancel = getattr(coordinator, "cancel_running_for_session", None)
            cancelled_tools = 0
            if callable(cancel):
                from ..tool_calls import CancelReason

                cancelled_tools = int(
                    await cancel(
                        ctx.session_id,
                        agent_id=ctx.agent_id,
                        reason=CancelReason.USER,
                    )
                    or 0,
                )
            if interaction_error is not None:
                raise RuntimeError(
                    "failed to cancel invocation interactions",
                ) from interaction_error
            return cancelled_tools

        ctx.extras["interrupt_session"] = await control_service.bind_interrupt(
            invocation_id,
            runtime_task,
            lease=lease,
            agent_id=ctx.agent_id,
            conversation_id=str(conversation_id),
            cancel_children=cancel_runtime_children,
        )

    async def _finalize_runtime(
        self,
        ctx: HookContext,
        assembly: Any,
    ) -> None:
        """Drain cleanup even when the caller receives another cancellation."""
        cleanup = asyncio.create_task(
            self._finalize_runtime_resources(ctx, assembly),
        )
        cancellation: asyncio.CancelledError | None = None
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as error:
                cancellation = error
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
        if cancellation is not None:
            try:
                cleanup.result()
            except BaseException:  # noqa: BLE001
                logger.warning(
                    "runtime: cleanup failed while cancellation was pending "
                    "session=%s",
                    getattr(ctx, "session_id", ""),
                    exc_info=True,
                )
            raise cancellation
        cleanup.result()

    # pylint: disable=too-many-branches,too-many-statements
    async def _finalize_runtime_resources(
        self,
        ctx: HookContext,
        assembly: Any,
    ) -> None:
        """Run FINALLY while sessions are live, then close in reverse order."""
        await self._close_interrupt_session(ctx)
        steering_session = ctx.extras.get("steering_session")
        if steering_session is not None:
            try:
                await steering_session.close()
            except Exception:  # pylint: disable=broad-except
                logger.warning(
                    "runtime: steering session close failed session=%s",
                    getattr(ctx, "session_id", ""),
                    exc_info=True,
                )
        agent = getattr(ctx, "agent", None)
        if agent is not None and callable(getattr(agent, "close", None)):
            try:
                await agent.close()
            except Exception:  # pylint: disable=broad-except
                logger.warning(
                    "runtime: agent close failed session=%s",
                    getattr(ctx, "session_id", ""),
                    exc_info=True,
                )

        finally_error: BaseException | None = None
        try:
            await self._run_hook_phase(ctx, Phase.FINALLY)
        except BaseException as error:  # noqa: BLE001
            finally_error = error

        from .command_providers import close_command_session
        from .driver_providers import close_driver_session
        from .hook_providers import close_hook_session
        from .memory_providers import close_memory_session
        from .mode_providers import close_agent_mode_session
        from .stop_gate_providers import close_stop_gate_session

        resources = (
            ("memory", close_memory_session, "memory_session"),
            ("driver", close_driver_session, "driver_session"),
            ("command", close_command_session, "command_session"),
            ("agent mode", close_agent_mode_session, "agent_mode_session"),
            ("stop-gate", close_stop_gate_session, "stop_gate_session"),
            ("hook", close_hook_session, "hook_session"),
        )
        for label, close_resource, key in resources:
            try:
                await close_resource(ctx.extras.get(key))
            except Exception:  # pylint: disable=broad-except
                logger.warning(
                    "runtime: %s session close failed session=%s",
                    label,
                    getattr(ctx, "session_id", ""),
                    exc_info=True,
                )
        if assembly is not None:
            try:
                await assembly.close()
            except Exception:  # pylint: disable=broad-except
                logger.warning(
                    "runtime: assembly close failed session=%s",
                    getattr(ctx, "session_id", ""),
                    exc_info=True,
                )
        lease = ctx.extras.get("invocation_control_lease")
        control_service = ctx.extras.get("invocation_control_service")
        if lease is not None and control_service is not None:
            terminal_status = ctx.extras.get(
                "invocation_terminal_status",
                SubmissionStatus.FAILED,
            )
            if finally_error is not None:
                terminal_status = SubmissionStatus.FAILED
            await self._cancel_open_interactions(
                ctx,
                lease.steering.invocation_id,
                terminal_status,
            )
            try:
                await control_service.finish_turn(lease, terminal_status)
            except Exception:  # pylint: disable=broad-except
                logger.warning(
                    "runtime: durable invocation finalization failed "
                    "session=%s",
                    getattr(ctx, "session_id", ""),
                    exc_info=True,
                )
        request = getattr(ctx, "request", None)
        request_context = getattr(request, "request_context", None)
        from ..tasks.usage_scope import close_usage_scope_in_context

        close_usage_scope_in_context(request_context)
        if finally_error is not None:
            raise finally_error

    @staticmethod
    async def _close_interrupt_session(ctx: HookContext) -> None:
        """Remove the live cancellation root before async cleanup begins."""
        interrupt_session = ctx.extras.get("interrupt_session")
        if interrupt_session is None:
            return
        try:
            await interrupt_session.close()
        except Exception:  # pylint: disable=broad-except
            logger.warning(
                "runtime: interrupt session close failed session=%s",
                getattr(ctx, "session_id", ""),
                exc_info=True,
            )

    @staticmethod
    async def _cancel_open_interactions(
        ctx: HookContext,
        invocation_id: uuid.UUID,
        terminal_status: SubmissionStatus,
    ) -> None:
        """Release every unresolved interaction owned by a terminal run."""
        interaction_service = ctx.extras.get("interaction_service")
        if interaction_service is None:
            return
        try:
            await interaction_service.cancel_invocation(
                invocation_id,
                detail=f"invocation finished as {terminal_status.value}",
                include_non_blocking=False,
                preserve_conversation_continuations=(
                    terminal_status is SubmissionStatus.SUCCEEDED
                ),
            )
        except Exception:  # pylint: disable=broad-except
            logger.warning(
                "runtime: interaction finalization failed session=%s",
                getattr(ctx, "session_id", ""),
                exc_info=True,
            )

    async def _start_modes(self, ctx: HookContext) -> None:
        """Prepare modes through the pinned invocation provider."""
        session = ctx.extras.get("agent_mode_session")
        if session is None:
            return
        await session.start_turn()

    async def _start_stop_gates(self, ctx: HookContext) -> None:
        """Prepare provider-owned stop-gate state after Agent Modes."""
        session = ctx.extras.get("stop_gate_session")
        if session is not None:
            await session.start_turn()

    async def _open_hook_session(
        self,
        ctx: HookContext,
        assembly: Any,
    ) -> Any:
        """Open and merge selected Hook Providers from one generation."""
        from .hook_providers import HookRouterSession, WorkspaceHookHost

        sessions = []
        try:
            for provider_id in assembly.scope.selection.hook_provider_ids:
                provider = assembly.require(provider_id, "hook.provider")
                if getattr(provider, "provider_id", None) != provider_id:
                    raise TypeError(
                        f"hook provider '{provider_id}' returned an "
                        "implementation with a mismatched identity",
                    )
                open_session = getattr(provider, "open", None)
                if not callable(open_session):
                    raise TypeError(
                        f"hook provider '{provider_id}' does not implement "
                        "open()",
                    )
                session = await open_session(
                    assembly.scope,
                    WorkspaceHookHost.capture(ctx, provider_id),
                )
                sessions.append(session)
                for member_name in (
                    "provider_id",
                    "list_hooks",
                    "run_hook",
                    "close",
                ):
                    if not hasattr(session, member_name):
                        raise TypeError(
                            f"hook provider '{provider_id}' returned a "
                            f"session without {member_name}",
                        )
            return HookRouterSession(sessions)
        except BaseException:
            await self._close_opened_sessions(sessions, "hook")
            raise

    async def _open_stop_gate_session(
        self,
        ctx: HookContext,
        assembly: Any,
    ) -> Any:
        """Open and merge selected loop Stop Gate Providers."""
        from .stop_gate_providers import (
            StopGateRouterSession,
            WorkspaceStopGateHost,
        )

        sessions = []
        try:
            for provider_id in assembly.scope.selection.stop_gate_provider_ids:
                provider = assembly.require(
                    provider_id,
                    "loop.gate.provider",
                )
                if getattr(provider, "provider_id", None) != provider_id:
                    raise TypeError(
                        f"stop-gate provider '{provider_id}' returned an "
                        "implementation with a mismatched identity",
                    )
                open_session = getattr(provider, "open", None)
                if not callable(open_session):
                    raise TypeError(
                        f"stop-gate provider '{provider_id}' does not "
                        "implement open()",
                    )
                session = await open_session(
                    assembly.scope,
                    WorkspaceStopGateHost.capture(ctx, provider_id),
                )
                sessions.append(session)
                for member_name in (
                    "provider_id",
                    "list_gates",
                    "is_active",
                    "evaluate",
                    "start_turn",
                    "reset_conversation",
                    "close",
                ):
                    if not hasattr(session, member_name):
                        raise TypeError(
                            f"stop-gate provider '{provider_id}' returned a "
                            f"session without {member_name}",
                        )
            return StopGateRouterSession(sessions)
        except BaseException:
            await self._close_opened_sessions(sessions, "stop-gate")
            raise

    async def _run_hook_phase(
        self,
        ctx: HookContext,
        phase: Phase,
    ) -> HookOutcome:
        """Run a pinned phase, with bootstrap fallback before session open."""
        session = ctx.extras.get("hook_session")
        if session is not None:
            return await session.run(LifecyclePhase(phase.value))
        from .hook_providers import run_legacy_phase

        return await run_legacy_phase(
            self.workspace.plugins.hook_registry,
            phase,
            ctx,
        )

    async def _run_cancel_phase(self, ctx: HookContext) -> None:
        """Drain pinned cancellation hooks under repeated cancellation."""

        async def run_phase() -> tuple[str, ...]:
            session = ctx.extras.get("hook_session")
            if session is not None:
                return await session.run_isolated(
                    LifecyclePhase.ON_CANCEL,
                    timeout_seconds=_CANCEL_HOOK_TIMEOUT_SECONDS,
                )
            return await self.workspace.plugins.hook_registry.run_isolated(
                Phase.ON_CANCEL,
                ctx,
                timeout_seconds=_CANCEL_HOOK_TIMEOUT_SECONDS,
            )

        cleanup = asyncio.create_task(run_phase())
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
            except BaseException:  # noqa: BLE001
                break
        try:
            failures = cleanup.result()
        except BaseException:  # noqa: BLE001
            logger.warning(
                "runtime: cancellation hook phase failed session=%s",
                getattr(ctx, "session_id", ""),
                exc_info=True,
            )
            failures = ("hook.provider",)
        if failures:
            ctx.extras["cancel_hook_failures"] = failures

    @staticmethod
    def _hook_message(outcome: HookOutcome) -> Any:
        """Convert one public short-circuit response for the SSE envelope."""
        from agentscope.message import Msg, TextBlock

        message = outcome.message
        if message is None:
            raise TypeError("short-circuit hook outcome has no message")
        return Msg(
            name="assistant",
            role="assistant",
            content=[TextBlock(type="text", text=message.text)],
            metadata=message.metadata,
        )

    async def _open_agent_mode_session(
        self,
        ctx: HookContext,
        assembly: Any,
    ) -> Any:
        """Open the selected Agent Mode Provider from the pinned assembly."""
        provider_id = assembly.scope.selection.agent_mode_provider_id
        provider = assembly.require(provider_id, "agent.mode.provider")
        if getattr(provider, "provider_id", None) != provider_id:
            raise TypeError(
                f"agent mode provider '{provider_id}' returned an "
                "implementation with a mismatched identity",
            )
        open_session = getattr(provider, "open", None)
        if not callable(open_session):
            raise TypeError(
                f"agent mode provider '{provider_id}' does not implement "
                "open()",
            )
        from ..kernel.invocation import DEFAULT_AGENT_MODE_PROVIDER_ID
        from .mode_providers import (
            ProviderAgentModeHost,
            WorkspaceAgentModeHost,
            bind_agent_mode_state,
        )
        from .provider_config import validate_provider_config

        profile = getattr(self.workspace, "config", None)
        capability_configs = getattr(profile, "capability_configs", {}) or {}
        provider_config = dict(capability_configs.get(provider_id, {}) or {})
        descriptor = assembly.descriptor(provider_id)
        provider_config = validate_provider_config(
            provider_id,
            provider_config,
            descriptor.config_schema,
        )
        state = bind_agent_mode_state(provider_id, assembly.scope)
        if provider_id == DEFAULT_AGENT_MODE_PROVIDER_ID:
            host = WorkspaceAgentModeHost.capture(
                ctx,
                provider_config,
                state,
            )
        else:
            host = ProviderAgentModeHost(provider_config, state)

        session = await open_session(
            assembly.scope,
            host,
        )
        try:
            for method_name in (
                "active_mode_names",
                "start_turn",
                "reset_conversation",
                "close",
            ):
                if not callable(getattr(session, method_name, None)):
                    raise TypeError(
                        f"agent mode provider '{provider_id}' returned a "
                        f"session without {method_name}()",
                    )
        except BaseException:
            await self._close_opened_sessions([session], "agent mode")
            raise
        return session

    async def _open_command_session(
        self,
        ctx: HookContext,
        assembly: Any,
    ) -> Any:
        """Open and merge selected providers from the pinned assembly."""
        from .command_providers import (
            CommandRouterSession,
            WorkspaceCommandHost,
        )

        sessions = []
        try:
            for provider_id in assembly.scope.selection.command_provider_ids:
                provider = assembly.require(provider_id, "command.provider")
                if getattr(provider, "provider_id", None) != provider_id:
                    raise TypeError(
                        f"command provider '{provider_id}' returned an "
                        "implementation with a mismatched identity",
                    )
                open_session = getattr(provider, "open", None)
                if not callable(open_session):
                    raise TypeError(
                        f"command provider '{provider_id}' does not implement "
                        "open()",
                    )
                session = await open_session(
                    assembly.scope,
                    WorkspaceCommandHost.capture(ctx, provider_id),
                )
                sessions.append(session)
                for member_name in (
                    "provider_id",
                    "allows_dynamic_fallback",
                    "list_commands",
                    "dispatch",
                    "fallback",
                    "close",
                ):
                    if not hasattr(session, member_name):
                        raise TypeError(
                            f"command provider '{provider_id}' returned a "
                            f"session without {member_name}",
                        )
            return CommandRouterSession(sessions)
        except BaseException:
            await self._close_opened_sessions(sessions, "command")
            raise

    @staticmethod
    async def _close_opened_sessions(
        sessions: list[Any],
        label: str,
    ) -> None:
        """Rollback sessions that were opened before assembly failed."""
        for session in reversed(sessions):
            close = getattr(session, "close", None)
            if not callable(close):
                continue
            try:
                await close()
            except BaseException:  # noqa: BLE001
                logger.warning(
                    "runtime: %s session rollback failed",
                    label,
                    exc_info=True,
                )

    async def _try_save_on_cancel(
        self,
        ctx: HookContext,
        *,
        include_partial: bool = True,
    ) -> dict[str, Any] | None:
        """Best-effort session save after cancellation or failure.

        Before snapshotting, any partial streaming content accumulated in
        the ``Envelope`` is injected into the agent's context so the
        interrupted turn's text is not lost on reload.

        ``state_dict()`` is called synchronously to snapshot the agent
        state *before* any further event-loop iteration.  The I/O write
        is wrapped in ``asyncio.shield`` so it completes even when the
        outer task's ``_must_cancel`` flag triggers a re-cancellation on
        the next ``await``.  In that case the shielded inner task still
        runs to completion in the background; the ``proxy`` owns an
        independent copy of the data so ``agent.close()`` in the
        ``finally`` block cannot corrupt it.

        .. note:: Why persistence remains Host-owned

           Generation-pinned ``ON_CANCEL`` hooks run first, but their failure
           or timeout cannot prevent the Host from preserving the interrupted
           turn. The synchronous inject, Cron restore and state snapshot stay
           here as invariants, while ``asyncio.shield`` protects the write.
        """
        request = getattr(ctx, "request", None)
        request_context = getattr(request, "request_context", None)
        if isinstance(request_context, dict):
            ephemeral = request_context.get(ACP_EPHEMERAL_META_KEY)
            if ephemeral is True or (
                isinstance(ephemeral, str)
                and ephemeral.lower() in {"1", "true", "yes"}
            ):
                return None

        agent = getattr(ctx, "agent", None)
        if agent is None:
            return None
        workspace = getattr(ctx, "workspace", None)
        session = getattr(workspace, "session", None) if workspace else None
        if session is None:
            return None
        try:
            envelope = getattr(ctx, "envelope", None)
            if envelope is not None and include_partial:
                self._inject_partial_response(agent, envelope)

            from ..hooks.cron.cron_hook import restore_cron_context
            from ._state_utils import StateProxy

            restore_cron_context(ctx)
            proxy = StateProxy()
            proxy.data = agent.state_dict()
            request = ctx.request
            from .console_turn_state import REGENERATE_FROM, stamp_console_turn

            # A failed replacement must not destroy the previously saved turn.
            if (getattr(request, "request_context", None) or {}).get(
                REGENERATE_FROM,
            ):
                return None
            stamp_console_turn(
                proxy.data,
                request,
                (
                    "canceled"
                    if isinstance(
                        ctx.error,
                        (asyncio.CancelledError, KeyboardInterrupt),
                    )
                    else "failed"
                ),
                ctx.error,
            )
            user_id = getattr(request, "user_id", "") or ctx.session_id
            channel = getattr(request, "channel", "") or ""
            await asyncio.shield(
                session.save_session_state(
                    session_id=ctx.session_id,
                    user_id=user_id,
                    channel=channel,
                    agent=proxy,
                ),
            )
            logger.info(
                "cancel-save: persisted interrupted turn (session=%s)",
                ctx.session_id,
            )
            return proxy.data
        except asyncio.CancelledError:
            logger.info(
                "cancel-save: outer await re-cancelled, inner save "
                "continues in background (session=%s)",
                ctx.session_id,
            )
        except Exception:
            logger.debug(
                "cancel-save: failed (session=%s)",
                ctx.session_id,
                exc_info=True,
            )
        return None

    async def _checkpoint_model_step_context(
        self,
        ctx: HookContext,
        error: ModelStepRecoveryError,
        agent_state: dict[str, Any] | None,
    ) -> None:
        """Publish a private context snapshot only after its durable save."""
        if (
            agent_state is None
            or error.status is not ModelStepContinuationStatus.READY
        ):
            return
        service = getattr(
            ctx.workspace,
            "model_resource_wait_service",
            None,
        )
        workspace_dir = getattr(ctx.workspace, "workspace_dir", None)
        if service is None or workspace_dir is None:
            return
        try:
            continuation = await service.get_model_step(
                error.continuation_id,
            )
            if continuation is None:
                return
            request_context = (
                getattr(ctx.request, "request_context", None) or {}
            )
            source_submission_id = uuid.UUID(
                str(request_context["os_submission_id"]),
            )
            from .actions import (
                lite_action_store,
                model_step_committed_action_items,
                model_step_action_evidence_digest,
            )
            from .model_step_contexts import (
                context_has_committed_action_items,
                lite_model_step_context_store,
            )

            actions = await lite_action_store(
                workspace_dir,
            ).scan_for_conversation(continuation.conversation_id)
            committed_items = model_step_committed_action_items(
                actions,
                continuation.invocation_id,
            )
            evidence_digest = model_step_action_evidence_digest(
                actions,
                continuation.invocation_id,
            )
            if (
                committed_items is None
                or evidence_digest is None
                or not context_has_committed_action_items(
                    agent_state,
                    committed_items,
                )
            ):
                return
            checkpoint = ModelStepContextCheckpoint(
                checkpoint_id=uuid.uuid5(
                    continuation.continuation_id,
                    "private-agent-context",
                ),
                continuation_id=continuation.continuation_id,
                invocation_id=continuation.invocation_id,
                chat_id=continuation.chat_id,
                source_submission_id=source_submission_id,
                action_evidence_digest=evidence_digest,
                action_count=len(committed_items),
            )
            stored = await lite_model_step_context_store(
                workspace_dir,
            ).save(checkpoint, agent_state)
            await service.attach_model_step_context(
                continuation.continuation_id,
                stored,
            )
            ctx.extras["model_step_context_checkpoint_id"] = str(
                stored.checkpoint_id,
            )
        except Exception:  # pylint: disable=broad-except
            logger.exception(
                "runtime: failed to checkpoint model-step context "
                "continuation=%s",
                error.continuation_id,
            )

    # pylint: disable=too-many-branches
    @staticmethod
    def _inject_partial_response(agent: Any, envelope: Any) -> None:
        """Inject accumulated streaming content from *envelope* into the
        agent's context so a cancel-save includes the partial response.

        Two responsibilities:

        1. **Partial text/thinking** — uses ``Envelope.collect_partial_blocks``
           to obtain content from the *interrupted* reasoning iteration, with
           a deduplication guard against double-saving.

        2. **Dangling tool calls** — AgentScope's
           ``_close_unfinished_tool_calls`` normally patches the context in a
           ``finally`` block, but its ``yield`` statements fail when the
           generator is being closed.  We replicate the context-mutation logic
           here (without yields) so that every tool call has a matching
           ``ToolResultBlock`` on reload.
        """
        # pylint: disable=too-many-nested-blocks
        try:
            from agentscope.message import TextBlock, ThinkingBlock

            # --- 1) Partial text/thinking injection ---
            partial = envelope.collect_partial_blocks()
            injected = 0

            if partial:
                agent_state = getattr(agent, "state", None)
                ctx_list = (
                    getattr(agent_state, "context", None)
                    if agent_state
                    else None
                )
                existing_texts: set[str] = set()
                if ctx_list and len(ctx_list) > 0:
                    last = ctx_list[-1]
                    if getattr(last, "role", None) == "assistant":
                        for blk in getattr(last, "content", []) or []:
                            if getattr(blk, "type", None) == "text":
                                existing_texts.add(
                                    getattr(blk, "text", ""),
                                )
                            elif getattr(blk, "type", None) == "thinking":
                                existing_texts.add(
                                    getattr(blk, "thinking", ""),
                                )

                blocks: list = []
                for btype, content in partial:
                    if content in existing_texts:
                        continue
                    if btype == "thinking":
                        blocks.append(ThinkingBlock(thinking=content))
                    else:
                        blocks.append(TextBlock(text=content))
                if blocks:
                    # pylint: disable=protected-access
                    agent._save_to_context(blocks)
                injected = len(blocks)

            # --- 2) Close dangling tool calls ---
            closed = Runtime._close_dangling_tool_calls(agent, envelope)

            if injected or closed:
                logger.info(
                    "cancel-save: injected %d partial block(s), "
                    "closed %d dangling tool call(s)",
                    injected,
                    closed,
                )
        except Exception:
            logger.debug(
                "cancel-save: partial response injection failed",
                exc_info=True,
            )

    @staticmethod
    def _close_dangling_tool_calls(agent: Any, envelope: Any) -> int:
        """Ensure every ``ToolCallBlock`` in the context has a matching
        ``ToolResultBlock``.

        AgentScope's ``_close_unfinished_tool_calls`` patches the context
        inside a generator ``finally`` block, but its ``yield`` statements
        trigger ``RuntimeError`` when the generator is being torn down.
        We replicate the mutation-only logic so dangling tool calls are
        properly closed before ``state_dict()`` is called.

        Returns the number of tool calls closed.
        """
        from agentscope.message import (
            ToolCallBlock,
            ToolCallState,
            ToolResultBlock,
            ToolResultState,
        )

        state = getattr(agent, "state", None)
        context = getattr(state, "context", None) if state else None
        if not context:
            return 0

        last_msg = context[-1]
        if getattr(last_msg, "role", None) != "assistant":
            return 0
        if getattr(last_msg, "name", None) != getattr(agent, "name", ""):
            return 0

        content = getattr(last_msg, "content", None)
        if not isinstance(content, list):
            return 0

        # Find tool calls without matching results.
        awaiting: dict[str, int] = {}
        for idx, block in enumerate(content):
            if isinstance(block, ToolCallBlock):
                awaiting[block.id] = idx
            elif isinstance(block, ToolResultBlock):
                awaiting.pop(block.id, None)

        if not awaiting:
            return 0

        # Incorporate any partial output accumulated in the envelope.
        envelope_tool_output = envelope.collect_tool_output()

        interruption_msg = (
            "<system-reminder>The tool call has been interrupted by "
            "the user.</system-reminder>"
        )

        closed = 0
        for call_id, idx in awaiting.items():
            block = content[idx]
            block.state = ToolCallState.FINISHED

            output = envelope_tool_output.get(call_id, "")
            if output:
                output += "\n" + interruption_msg
            else:
                output = interruption_msg

            content.append(
                ToolResultBlock(
                    id=call_id,
                    name=block.name,
                    output=output,
                    state=ToolResultState.INTERRUPTED,
                ),
            )
            closed += 1

        return closed

    @staticmethod
    def _normalize(request: Any) -> Any:
        from ..schemas import AgentRequest

        if isinstance(request, dict):
            request = AgentRequest(**request)
        if not getattr(request, "session_id", None):
            request.session_id = uuid.uuid4().hex
        if not getattr(request, "user_id", None):
            request.user_id = request.session_id
        return request

    @staticmethod
    def _task_registry_generation(request: Any) -> int | None:
        """Trust a pinned generation only from the internal Task bridge."""
        request_context = getattr(request, "request_context", None)
        if not isinstance(request_context, dict):
            return None
        if request_context.get("durable_task") is not True:
            return None
        if request_context.get("_task_approval_broker") is None:
            return None
        generation = request_context.get("os_registry_generation")
        if isinstance(generation, bool) or not isinstance(generation, int):
            return None
        return generation if generation >= 1 else None

    @staticmethod
    def _task_causal_identity(
        request: Any,
    ) -> tuple[uuid.UUID | None, uuid.UUID | None]:
        """Trust Task causal identity only from the internal broker bridge."""
        request_context = getattr(request, "request_context", None)
        if not isinstance(request_context, dict):
            return None, None
        if request_context.get("durable_task") is not True:
            return None, None
        if request_context.get("_task_approval_broker") is None:
            return None, None
        raw_invocation = request_context.get("os_invocation_id")
        raw_correlation = request_context.get("os_correlation_id")
        try:
            invocation_id = uuid.UUID(str(raw_invocation))
            correlation_id = uuid.UUID(str(raw_correlation))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError(
                "internal Task bridge has invalid causal identity",
            ) from exc
        return invocation_id, correlation_id

    @staticmethod
    def _conversation_id(request: Any) -> str | None:
        """Read the Chat adapter's stable Conversation identity once."""
        request_context = getattr(request, "request_context", None)
        if not isinstance(request_context, dict):
            return None
        conversation_id = request_context.get("os_conversation_id")
        if not isinstance(conversation_id, str) or not conversation_id:
            return None
        return conversation_id

    def _build_context(self, request: Any) -> HookContext:
        workspace_dir = getattr(self.workspace, "workspace_dir", None)
        # Prefer the workspace's resolved agent id over a bare "default", so an
        # agent selected by header (no body agent_id) loads its own config.
        agent_id = (
            getattr(request, "agent_id", None)
            or getattr(self.workspace, "agent_id", None)
            or "default"
        )
        session_id = request.session_id
        root_session_id = getattr(request, "root_session_id", "") or session_id
        root_agent_id = getattr(request, "root_agent_id", "") or agent_id

        return HookContext(
            request=request,
            session_id=session_id,
            agent_id=agent_id,
            root_session_id=root_session_id,
            root_agent_id=root_agent_id,
            workspace_dir=workspace_dir,
            workspace=self.workspace,
            app_services=self.app_services,
            input_msgs=_request_input_to_msgs(request.input),
        )

    @staticmethod
    def _apply_context_injections(ctx: HookContext) -> None:
        """Merge context_injections into input_msgs as a system hint.

        Sorts injections by priority (ascending) and prepends a
        single system-role message so the agent sees the dynamic
        context in its current turn.
        """
        injections = ctx.context_injections
        if not injections:
            return
        sorted_inj = sorted(
            injections,
            key=lambda x: x.get("priority", 100),
        )
        parts = [inj["content"] for inj in sorted_inj if inj.get("content")]
        if not parts:
            return
        try:
            from agentscope.message import Msg, TextBlock

            hint_msg = Msg(
                name="system",
                role="user",
                content=[
                    TextBlock(
                        type="text",
                        text="\n\n".join(parts),
                    ),
                ],
            )
            ctx.input_msgs.insert(0, hint_msg)
        except Exception:
            logger.debug(
                "runtime: failed to inject context: %d items",
                len(parts),
            )


__all__ = ["Runtime"]
