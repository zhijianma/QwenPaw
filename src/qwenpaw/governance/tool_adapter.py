# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""PolicyGuardedTool — Governance policy-checked tool wrapper.

Replaces the GuardedFunctionTool. Each tool call goes through two layers:
1. check_permissions: pre-execution decision
2. __call__: actual execution — handles sandbox violation retry loop
"""

from __future__ import annotations

import logging
import hashlib
import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any, Optional
from uuid import UUID

from agentscope.message import TextBlock
from agentscope.tool import ToolChunk

from ..app.approvals.interaction_bridge import attach_pending_to_interaction
from ..app.approvals.task_bridge import attach_pending_to_durable_task
from ..kernel.models import (
    ActionKind,
    ApprovalDisplay,
    ApprovalSource,
    SideEffectBroker,
    SideEffectDisposition,
    SideEffectReservation,
    SideEffectStatus,
    ToolEffect,
    EnvironmentResolutionStatus,
)
from .policy import (
    GovernanceDecision,
    GovernanceAction,
    ToolCallSpec,
)
from .resource_governor import ResourceGovernor
from .tool_registry import DEFAULT_REGISTRY

logger = logging.getLogger(__name__)

_NO_RETRY_INSTRUCTION = (
    "\n\n\u26a0\ufe0f **System instruction**: this denial is final"
    " for the current request. Do not retry this tool with similar"
    " parameters. Reply to the user explaining why the action could"
    " not be completed and, if appropriate, ask them how they want"
    " to proceed."
)


def _is_execution_level_off() -> bool:
    """Check if execution_level is 'off' (dev mode pass-through).

    Reads directly from policy.yaml (without needing a governor) to
    support the case where governor initialization failed but the user
    explicitly configured execution_level=off for development.
    """
    try:
        from pathlib import Path

        import yaml

        from ..constant import WORKING_DIR

        policy_path = Path(WORKING_DIR) / ".qwenpaw" / "policy.yaml"
        if not policy_path.exists():
            return False
        with open(policy_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return isinstance(data, dict) and data.get("execution_level") == "off"
    except Exception:
        return False


def _resolve_effective_approval_level(
    request_context: dict[str, str] | None,
) -> Optional[Any]:
    """Resolve the effective approval_level for this tool call.

    Priority:
      1. ``request_context["approval_level"]`` — session-level override
         injected by the frontend (localStorage per chat, carried in each
         request). Zero I/O: already in memory.
      2. ``agent.json`` → ``AgentProfileConfig.approval_level`` — the
         agent-level default set via the Web UI 'Tool Execution Security' card.
      3. ``None`` — unresolvable (caller falls back to AUTO).

    Returns the :class:`ToolExecutionLevel` enum, or ``None``.
    """
    if not request_context:
        return None

    from ..security.tool_guard.execution_level import ToolExecutionLevel

    # Session-level override (injected by frontend per request)
    session_raw = request_context.get("approval_level")
    if session_raw:
        level = ToolExecutionLevel.from_config(session_raw)
        if level is not None:
            return level

    # Agent-level default from agent.json
    agent_id = request_context.get("agent_id", "")
    if not agent_id:
        return None
    try:
        from ..config.config import load_agent_config

        profile = load_agent_config(agent_id)
        raw = getattr(profile, "approval_level", None)
        return ToolExecutionLevel.from_config(raw)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# PolicyGuardedTool
# ---------------------------------------------------------------------------


class PolicyGuardedTool:
    """Governance policy-checked tool wrapper.

    Dynamically inherits from FunctionTool, implementing:
    - check_permissions: calls assert_policy() + audit() for policy decision
    - __call__: overrides to handle sandbox execution + violation retry

    .. warning:: Known limitation — dynamic anonymous class

        ``__new__`` creates a fresh class via ``type(...)`` on every call,
        so ``isinstance(tool, PolicyGuardedTool)`` always returns False.
        This is the same pattern used by ``GuardedFunctionTool`` and
        ``DriverCapabilityTool``.  A unified refactor (metaclass or mixin)
        is tracked as a follow-up task.
    """

    def __new__(cls, *args: Any, **kwargs: Any) -> Any:
        from agentscope.tool import FunctionTool

        if cls is PolicyGuardedTool:
            real_cls = type(
                "PolicyGuardedTool",
                (FunctionTool,),
                {
                    "__init__": _policy_tool_init,
                    "_build_tc_spec": _build_tc_spec,
                    "check_permissions": _policy_tool_check_permissions,
                    "__call__": _policy_tool_call,
                    "__doc__": cls.__doc__,
                },
            )
            return real_cls(*args, **kwargs)
        return object.__new__(cls)


def _policy_tool_init(
    self: Any,
    func: Any,
    *,
    governor: Optional[ResourceGovernor] = None,
    request_context: dict[str, str] | None = None,
    governance_registry: Any = None,
    **kwargs: Any,
) -> None:
    from agentscope.tool import FunctionTool

    FunctionTool.__init__(self, func, **kwargs)
    self._qp_governor = governor
    self._qp_request_context = request_context or {}
    self._qp_governance_registry = governance_registry or DEFAULT_REGISTRY
    # A plugin backend can retain a stable public tool name while selecting a
    # more precise governance identity. Remote searches can therefore opt into
    # a network policy while local backends keep the internal policy identity.
    self._qp_policy_name = getattr(func, "_qwenpaw_policy_name", "")
    self._qp_policy_decision = None  # Pre-evaluation result
    self._qp_sandbox_mode = False  # Whether to execute in sandbox
    self._qp_raw_params = {}  # Set per-call by check_permissions


def _relative_path_base(governor: Any) -> str:
    """Return the directory a tool's *relative* paths resolve against.

    Must match the tool layer exactly. File/shell tools resolve relative
    paths from the PRIMARY project directory (``get_tool_base_dir()``),
    not from the agent workspace — evaluating the policy against a
    different base would check one path and operate on another, so a
    DENY/ASK rule scoped to the project would be missed while the
    workspace ALLOW rules matched instead.

    ``governor.coding_project_dir`` is the request-scoped primary project
    dir and already falls back to the workspace when nothing is
    configured, so this is the workspace whenever no project is bound.
    """
    if governor is None:
        return ""
    base = getattr(governor, "coding_project_dir", None)
    if base:
        return str(base)
    return str(getattr(governor, "workspace_dir", "") or "")


def _policy_tool_name(tool: Any) -> str:
    """Return an optional per-tool governance identity or the name mapping."""
    registry = getattr(
        tool,
        "_qp_governance_registry",
        DEFAULT_REGISTRY,
    )
    return getattr(tool, "_qp_policy_name", "") or (
        registry.python_to_policy_name(
            getattr(tool, "name", "Unknown"),
        )
    )


def _build_tc_spec(self: Any) -> ToolCallSpec:
    """Build ToolCallSpec from instance fields + dynamic target."""
    governor = self._qp_governor
    params = getattr(self, "_qp_raw_params", {})
    tool_name = _policy_tool_name(self)
    registry = getattr(
        self,
        "_qp_governance_registry",
        DEFAULT_REGISTRY,
    )
    request_ctx = getattr(self, "_qp_request_context", {}) or {}
    return ToolCallSpec(
        tool_name=tool_name,
        target=registry.extract_target(
            tool_name,
            params,
            workspace_dir=_relative_path_base(governor),
        ),
        agent_id=request_ctx.get("agent_id", ""),
        session_id=request_ctx.get("session_id", ""),
        raw_params=params,
        invocation_id=str(request_ctx.get("os_invocation_id") or ""),
        correlation_id=str(
            request_ctx.get("os_correlation_id")
            or request_ctx.get("os_invocation_id")
            or "",
        ),
        tool_type=registry.get_type(tool_name),
        effect=registry.get_effect(tool_name),
    )


def _prepare_off_mode_sandbox(tool: Any, governor: Any) -> None:
    """Compile+attach a ``sandbox_config`` for fail-closed tools in OFF mode.

    ``approval_level=OFF`` short-circuits the policy pipeline to ALLOW-all,
    which normally also skips the ``SANDBOX_FALLBACK`` branch that compiles a
    ``sandbox_config`` (see :func:`_policy_tool_check_permissions`). Sandbox
    provisioning and user approval are independent concerns: skipping "ask the
    user" must not skip "run it in a sandbox".

    Only tools flagged ``requires_sandbox`` in the registry are handled — i.e.
    the REPL, which returns ``DENIED`` without a config. Fail-open shell tools
    like ``Bash`` are deliberately left untouched: in OFF mode they run
    unsandboxed by design, and forcing a sandbox on them would silently narrow
    their filesystem access.

    A no-op (leaving ``sandbox_config=None``) when the sandbox is not usable
    -- either the platform has no sandbox, or the operator turned the global
    ``security.sandbox_enabled`` switch off. In both cases such a REPL is
    never registered in the first place, or it tolerates unsandboxed
    execution via ``allow_unsandboxed``.

    Uses ``governor.sandbox_usable`` (platform support AND the global switch)
    rather than the platform-only ``sandbox_available`` probe, so that an
    explicit ``sandbox_enabled=false`` is honoured on the OFF-mode path just
    like it is on the normal policy path (see
    :meth:`ResourceGovernor._sandbox_usable`).
    """
    # These attributes live on a reusable FunctionTool wrapper, but describe
    # one invocation only.  Clear any previous decision before consulting the
    # current switch so a hot toggle (or a failed recompile) cannot reuse a
    # stale sandbox config from an earlier call.
    for attr in ("_qp_sandbox_mode", "_qp_sandbox_config"):
        if hasattr(tool, attr):
            delattr(tool, attr)

    if governor is None:
        return
    policy_name = _policy_tool_name(tool)
    registry = getattr(
        tool,
        "_qp_governance_registry",
        DEFAULT_REGISTRY,
    )
    if not registry.requires_sandbox(policy_name):
        return
    if not getattr(governor, "sandbox_usable", False):
        return
    try:
        tc_spec = tool._build_tc_spec()
        tool._qp_sandbox_config = governor.compile_sandbox_config(tc_spec)
        tool._qp_sandbox_mode = True
    except Exception:
        # Leave sandbox_config unset; the tool's own fail-closed guard still
        # protects us — better a clean denial than an unsandboxed run.
        logger.exception(
            "OFF-mode sandbox_config compilation failed for '%s'.",
            getattr(tool, "name", "Unknown"),
        )


# pylint: disable=too-many-return-statements,too-many-branches
async def _policy_tool_check_permissions(
    self: Any,
    input_data: dict[str, Any] | None = None,
    context: Any = None,
    *_extra_args: Any,
    **_extra_kwargs: Any,
) -> Any:
    """Perform governance policy evaluation for a tool call.

    Flow:
        1. Construct ToolCallSpec(tool_name, target, agent_id, session_id)
        2. governor.assert_policy(tool_call) → GovernanceDecision
        3. governor.audit(tool_call, decision)
        4. Map to PermissionDecision
    """
    from agentscope.permission import PermissionBehavior, PermissionDecision

    del context

    governor = getattr(self, "_qp_governor", None)
    self._qp_raw_params = input_data or {}
    self._qp_policy_decision = None
    self._qp_approval_id = ""

    # ── Effective approval_level check (session > agent) ──
    request_ctx = getattr(self, "_qp_request_context", None) or {}
    effective_level = _resolve_effective_approval_level(request_ctx)

    # ── Mail F1 exploration mode: force STRICT for every tool ──
    # F1 is a session-level flag registered by the
    # activate_f1_exploration_mode tool (a module-level registry is used
    # because per-tool asyncio tasks isolate ContextVar writes). While
    # active it overrides the session/agent approval_level (including
    # OFF): all tool calls require user approval.
    from ..config.context import (
        get_current_session_id,
        is_f1_active_for_session,
    )
    from ..security.tool_guard.execution_level import ToolExecutionLevel

    _f1_session_id = request_ctx.get("session_id") or get_current_session_id()
    f1_active = is_f1_active_for_session(_f1_session_id)
    if f1_active:
        effective_level = ToolExecutionLevel.STRICT

    if effective_level is not None and effective_level.is_disabled():
        # OFF means "never ask the user" — it does NOT mean "skip the
        # sandbox". Sandbox isolation is an execution mechanism, not an
        # approval gate. Fail-closed tools (the REPL) return DENIED without
        # a sandbox_config, which the guard layer then misreads as a sandbox
        # violation and escalates to a recurring approval prompt OFF can
        # never resolve. So we still compile+attach the sandbox here; only
        # the "ask the user" step is skipped.
        _prepare_off_mode_sandbox(self, governor)
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="governance: approval_level=off, all tools allowed.",
        )

    if governor is None:
        # Check if execution_level is "off" (dev mode) — allow pass-through
        if _is_execution_level_off():
            return PermissionDecision(
                behavior=PermissionBehavior.ALLOW,
                message="governance: execution_level=off (dev mode), "
                "governor unavailable — pass-through.",
            )
        # Fail-closed: if governance layer failed to initialize, deny all
        # tool calls rather than silently allowing unguarded execution.
        logger.error(
            "PolicyGuardedTool: governor is None for tool '%s' — "
            "governance layer not initialized; denying (fail-closed).",
            getattr(self, "name", "Unknown"),
        )
        return PermissionDecision(
            behavior=PermissionBehavior.DENY,
            message=(
                "Governance layer unavailable (governor not initialized). "
                "All tool calls are denied until governance is restored. "
                "Please check server logs for initialization errors."
            ),
        )

    tc_spec = self._build_tc_spec()

    decision = governor.assert_policy(
        tc_spec,
        execution_level=(
            effective_level.value if effective_level is not None else None
        ),
    )
    governor.audit(tc_spec, decision)

    # Cache the decision + tc_spec for __call__ to use
    self._qp_policy_decision = decision
    self._qp_tc_spec = tc_spec
    self._qp_sandbox_mode = False

    if decision.action is GovernanceAction.ALLOW:
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="governance: tool allowed.",
        )
    elif decision.action is GovernanceAction.DENY:
        return PermissionDecision(
            behavior=PermissionBehavior.DENY,
            message=f"governance: '{tc_spec.tool_name}' is denied by policy",
        )
    elif decision.action is GovernanceAction.SANDBOX_FALLBACK:
        # Bash tool with no rule match → allow execution in sandbox
        self._qp_sandbox_mode = True
        self._qp_sandbox_config = decision.sandbox_config
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="governance: sandbox fallback.",
        )
    elif decision.action is GovernanceAction.ASK:
        # Requires user confirmation
        self._qp_policy_decision = decision

        return await _ask_user_approval(
            governor=governor,
            tc_spec=tc_spec,
            request_context=getattr(self, "_qp_request_context", {}) or {},
            tool_instance=self,
            policy_findings=decision.findings,
            governance_reason=decision.reason,
            source=decision.source,
        )
    else:
        # Unknown decision → deny as safe default
        return PermissionDecision(
            behavior=PermissionBehavior.DENY,
            message=f"Unknown policy decision: {decision.action}",
        )


def _optional_uuid(value: Any) -> UUID | None:
    """Parse a UUID without trusting legacy request-context strings."""
    try:
        return UUID(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def _tool_call_id() -> str:
    """Return AgentScope's per-call identity when supervision is active."""
    try:
        from ..tool_calls._ctxvars import get_call_context

        context = get_call_context()
        return context.tool_call_id if context is not None else ""
    except Exception:  # noqa: BLE001
        return ""


def _active_tool_call_context() -> Any | None:
    """Return the supervised call context without exposing ContextVar APIs."""
    try:
        from ..tool_calls._ctxvars import get_call_context

        return get_call_context()
    except Exception:  # noqa: BLE001
        return None


async def _begin_tool_action(tool: Any) -> None:
    """Fail closed if runtime-owned action intent cannot be recorded."""
    request_context = getattr(tool, "_qp_request_context", {}) or {}
    recorder = request_context.get("_action_recorder")
    if recorder is None:
        return
    context = _active_tool_call_context()
    if context is None:
        raise RuntimeError("action recorder requires supervised tool context")
    tc_spec = getattr(tool, "_qp_tc_spec", None) or tool._build_tc_spec()
    decision = getattr(tool, "_qp_policy_decision", None)
    effect = ToolEffect(tc_spec.effect)
    kind = (
        ActionKind.SHELL if effect is ToolEffect.PROCESS else ActionKind.TOOL
    )
    environment_ref = None
    environment_resolution = None
    sandbox_config = (
        getattr(tool, "_qp_sandbox_config", None)
        if getattr(tool, "_qp_sandbox_mode", False)
        else None
    )
    manager = request_context.get("_sandbox_environment_manager")
    if sandbox_config is not None and manager is not None:
        environment_ref, environment_resolution = await manager.resolve(
            sandbox_config,
            action_id=recorder.action_id(context, kind=kind),
        )
    await recorder.begin(
        context,
        effect=effect,
        policy_decision=(
            decision.action.value if decision is not None else "allow"
        ),
        approval_id=_optional_uuid(getattr(tool, "_qp_approval_id", "")),
        environment_ref=environment_ref,
    )
    if (
        environment_resolution is not None
        and environment_resolution.status
        is EnvironmentResolutionStatus.UNSATISFIED
    ):
        from ..runtime.environments import (
            EnvironmentContractUnsatisfiedError,
        )

        raise EnvironmentContractUnsatisfiedError(environment_resolution)


async def _begin_tool_side_effect(
    tool: Any,
) -> SideEffectReservation | None:
    """Reserve a durable record for one declared mutating tool call."""
    request_context = getattr(tool, "_qp_request_context", {}) or {}
    broker = request_context.get("_task_side_effect_broker")
    if not isinstance(broker, SideEffectBroker):
        return None
    tc_spec = getattr(tool, "_qp_tc_spec", None) or tool._build_tc_spec()
    effect = ToolEffect(tc_spec.effect)
    if effect is ToolEffect.NONE:
        return None
    invocation_id = _optional_uuid(tc_spec.invocation_id)
    correlation_id = _optional_uuid(tc_spec.correlation_id) or invocation_id
    call_id = _tool_call_id()
    idempotency_key = (
        f"tool:{invocation_id or tc_spec.session_id}:{call_id}"
        if call_id
        else f"tool:{uuid.uuid4()}"
    )
    request_hash = hashlib.sha256(
        json.dumps(
            {
                "action": tc_spec.tool_name,
                "target": tc_spec.target,
                "params": tc_spec.raw_params,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        ).encode("utf-8"),
    ).hexdigest()
    decision = getattr(tool, "_qp_policy_decision", None)
    approval_id = _optional_uuid(getattr(tool, "_qp_approval_id", ""))
    return await broker.begin(
        action=tc_spec.tool_name,
        target=tc_spec.target,
        effect=effect,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        invocation_id=invocation_id,
        correlation_id=correlation_id,
        approval_id=approval_id,
        policy_decision=(
            decision.action.value if decision is not None else "allow"
        ),
    )


def _side_effect_replay_chunk(
    reservation: SideEffectReservation,
) -> ToolChunk:
    """Return a deterministic response without repeating an operation."""
    from agentscope.message import ToolResultState

    replay = reservation.disposition is SideEffectDisposition.REPLAY
    return ToolChunk(
        is_last=True,
        state=(ToolResultState.SUCCESS if replay else ToolResultState.DENIED),
        content=[
            TextBlock(
                type="text",
                text=(
                    "The operation already completed for this tool call; "
                    "the durable result was reused."
                    if replay
                    else "The operation has an unresolved durable record; "
                    "it was not repeated because its outcome may be unsafe."
                ),
            ),
        ],
    )


def _chunk_terminal_status(chunk: Any) -> SideEffectStatus:
    """Map a final ToolChunk state onto the durable outcome vocabulary."""
    from agentscope.message import ToolResultState

    state = getattr(chunk, "state", ToolResultState.ERROR)
    if state is ToolResultState.SUCCESS:
        return SideEffectStatus.SUCCEEDED
    if state in {ToolResultState.ERROR, ToolResultState.DENIED}:
        return SideEffectStatus.FAILED
    return SideEffectStatus.UNCERTAIN


async def _finish_side_effect(
    broker: SideEffectBroker,
    reservation: SideEffectReservation,
    *,
    status: SideEffectStatus,
    digest: str | None = None,
    error_code: str = "",
) -> None:
    """Commit the outcome without replacing the tool's primary result."""
    await broker.finish(
        reservation.record.record_id,
        status=status,
        result_digest=digest,
        error_code=error_code,
    )


async def _policy_tool_call(
    self: Any,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Execute one governed call with durable side-effect accounting."""
    await _begin_tool_action(self)
    reservation = await _begin_tool_side_effect(self)
    if (
        reservation is not None
        and reservation.disposition is not SideEffectDisposition.EXECUTE
    ):
        return _side_effect_replay_chunk(reservation)
    broker = (getattr(self, "_qp_request_context", {}) or {}).get(
        "_task_side_effect_broker",
    )
    try:
        result = await _execute_policy_tool_call(self, *args, **kwargs)
    except BaseException as exc:
        if reservation is not None and isinstance(broker, SideEffectBroker):
            await _finish_side_effect(
                broker,
                reservation,
                status=SideEffectStatus.UNCERTAIN,
                error_code=type(exc).__name__,
            )
        raise
    if reservation is None or not isinstance(broker, SideEffectBroker):
        return result
    if hasattr(result, "__aiter__"):
        return _tracked_side_effect_stream(broker, reservation, result)
    digest = hashlib.sha256(repr(result).encode("utf-8")).hexdigest()
    await _finish_side_effect(
        broker,
        reservation,
        status=_chunk_terminal_status(result),
        digest=digest,
    )
    return result


async def _tracked_side_effect_stream(
    broker: SideEffectBroker,
    reservation: SideEffectReservation,
    stream: Any,
) -> AsyncGenerator[Any, None]:
    """Finalize streaming tools only after their output is consumed."""
    digest = hashlib.sha256()
    terminal = SideEffectStatus.UNCERTAIN
    try:
        async for chunk in stream:
            digest.update(repr(chunk).encode("utf-8"))
            terminal = _chunk_terminal_status(chunk)
            yield chunk
    except BaseException as exc:
        await _finish_side_effect(
            broker,
            reservation,
            status=SideEffectStatus.UNCERTAIN,
            error_code=type(exc).__name__,
        )
        raise
    await _finish_side_effect(
        broker,
        reservation,
        status=terminal,
        digest=digest.hexdigest(),
    )


async def _execute_policy_tool_call(
    self: Any,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Override FunctionTool.__call__ for sandbox execution + retry.

    If sandbox execution triggers a violation (ToolChunk state=DENIED),
    request user approval.
    If the user approves, retry without sandbox.
    """
    sandbox_mode = getattr(self, "_qp_sandbox_mode", False)
    if sandbox_mode:
        sandbox_config = getattr(self, "_qp_sandbox_config", None)
        if sandbox_config is not None:
            kwargs["sandbox_config"] = sandbox_config

    # Call the original function
    from agentscope.tool import FunctionTool
    from agentscope.message import ToolResultState

    result = await FunctionTool.__call__(self, *args, **kwargs)

    # Check if sandbox violation was returned (state=DENIED)
    if not (
        isinstance(result, ToolChunk)
        and result.state == ToolResultState.DENIED
    ):
        return result

    # Extract violation message from metadata or content
    violation_msg = ""
    if hasattr(result, "metadata") and result.metadata:
        violation_msg = result.metadata.get("sandbox_violation", "")
    if not violation_msg:
        # Fallback: extract from content text
        for block in result.content or []:
            if hasattr(block, "text") and "Sandbox violation:" in block.text:
                violation_msg = (
                    block.text.split("Sandbox violation:", 1)[1]
                    .split("\n")[0]
                    .strip()
                )
                break

    logger.info(
        "PolicyGuardedTool: sandbox violation for '%s': %s",
        getattr(self, "name", "Unknown"),
        violation_msg,
    )

    governor = getattr(self, "_qp_governor", None)
    request_context = getattr(self, "_qp_request_context", {}) or {}

    if governor is None:
        # No governor, can't approve — return the violation as DENIED
        return ToolChunk(
            is_last=True,
            state=ToolResultState.DENIED,
            content=[
                TextBlock(
                    type="text",
                    text=f"Sandbox violation: {violation_msg}\n"
                    f"Command was blocked by sandbox security policy.",
                ),
            ],
        )

    # Trigger approval flow — reuse tc_spec from check_permissions
    tc_spec = getattr(self, "_qp_tc_spec", None)
    if tc_spec is None:
        # Fallback: reconstruct if check_permissions didn't run
        self._qp_raw_params = {}
        tc_spec = self._build_tc_spec()

    governance_reason = getattr(
        getattr(self, "_qp_policy_decision", None),
        "reason",
        None,
    )
    governance_source = getattr(
        getattr(self, "_qp_policy_decision", None),
        "source",
        "No rule hit",
    )

    # Record the ASK escalation (sandbox violation → ask user)
    governor.audit(
        tc_spec,
        GovernanceDecision(
            action=GovernanceAction.ASK,
            reason=(
                f"sandbox violation: {violation_msg}"
                if violation_msg
                else "sandbox violation, ask user"
            ),
        ),
    )

    from agentscope.permission import PermissionBehavior

    decision = await _ask_user_approval(
        governor=governor,
        tc_spec=tc_spec,
        request_context=request_context,
        tool_instance=self,
        violation_msg=violation_msg or None,
        governance_reason=governance_reason,
        source=governance_source,
    )

    if decision.behavior == PermissionBehavior.ALLOW:
        # User approved: retry without sandbox
        logger.info(
            "PolicyGuardedTool: user approved sandbox violation, "
            "retrying without sandbox for '%s'",
            getattr(self, "name", "Unknown"),
        )
        kwargs.pop("sandbox_config", None)
        self._qp_sandbox_mode = False
        return await FunctionTool.__call__(self, *args, **kwargs)
    else:
        # User denied: return the violation as DENIED
        return ToolChunk(
            is_last=True,
            state=ToolResultState.DENIED,
            content=[
                TextBlock(
                    type="text",
                    text=f"Sandbox violation: {violation_msg}\n"
                    f"Command was blocked and user denied approval.\n\n"
                    f"{_NO_RETRY_INSTRUCTION}",
                ),
            ],
        )


# ---------------------------------------------------------------------------
# ASK path: reuse ApprovalService
# ---------------------------------------------------------------------------


async def _ask_user_approval(  # pylint: disable=too-many-statements
    governor: ResourceGovernor,
    tc_spec: ToolCallSpec,
    request_context: dict[str, Any],
    *,
    tool_instance: Any | None = None,
    violation_msg: str | None = None,
    governance_reason: str | None = None,
    policy_findings: list[Any] | None = None,
    source: str = "No rule hit",
) -> Any:
    """Request user approval, blocking until a reply is received."""
    from agentscope.permission import PermissionBehavior, PermissionDecision

    from ..app.approvals import get_approval_service
    from ..app.approvals.timeouts import approval_timeout_seconds
    from ..constant import TOOL_GUARD_APPROVAL_TIMEOUT_SECONDS
    from ..security.tool_guard.approval import (
        ApprovalDecision,
        ApprovalScope,
        format_findings_summary,
    )
    from ..security.tool_guard.models import (
        GuardFinding,
        GuardSeverity,
        GuardThreatCategory,
        ToolGuardResult,
    )

    tool_name = tc_spec.tool_name
    target = tc_spec.target
    agent_id = tc_spec.agent_id
    session_id = tc_spec.session_id
    params = tc_spec.raw_params

    ctx = request_context or {}
    timeout_seconds = approval_timeout_seconds(
        ctx,
        default=TOOL_GUARD_APPROVAL_TIMEOUT_SECONDS,
    )
    user_id = str(ctx.get("user_id") or "")
    channel = str(ctx.get("channel") or "")
    root_session_id = str(ctx.get("root_session_id") or session_id)
    root_agent_id = str(ctx.get("root_agent_id") or agent_id or "unknown")

    from .generalize import generalize_target_for_approval

    generalize_kwargs: dict[str, Any] = {"agent_id": agent_id}
    if tc_spec.tool_type:
        generalize_kwargs["tool_type"] = tc_spec.tool_type
    generalized_target = await generalize_target_for_approval(
        tool_name,
        target,
        source,
        **generalize_kwargs,
    )
    display_target = generalized_target or target

    # Construct a ToolGuardResult for ApprovalService.
    # If deep-scan findings were attached by policy.evaluate(),
    # convert them into GuardFindings for the approval card.
    if policy_findings:
        converted_findings = []
        for pf in policy_findings:
            # pf is a governance.detectors.GuardFinding (dataclass)
            converted_findings.append(
                GuardFinding(
                    id=getattr(pf, "id", uuid.uuid4().hex[:8]),
                    rule_id=getattr(pf, "rule_id", "policy_deep_scan"),
                    category=GuardThreatCategory(
                        getattr(pf, "category", "resource_abuse"),
                    ),
                    severity=GuardSeverity(
                        getattr(pf, "severity", "INFO"),
                    ),
                    title=getattr(pf, "title", "Policy Approval Required"),
                    description=getattr(pf, "description", ""),
                    tool_name=tool_name,
                    param_name=getattr(pf, "param_name", None),
                    matched_value=getattr(pf, "matched_value", None),
                    matched_pattern=getattr(pf, "matched_pattern", None),
                    snippet=getattr(pf, "snippet", None),
                    remediation=getattr(pf, "remediation", None),
                    guardian=getattr(pf, "detector", "governance_policy"),
                    metadata=getattr(pf, "metadata", {}),
                ),
            )
        guard_result = ToolGuardResult(
            tool_name=tool_name,
            params=params,
            findings=converted_findings,
            guardians_used=["governance_policy"],
        )
    else:
        guard_result = ToolGuardResult(
            tool_name=tool_name,
            params=params,
            findings=[
                GuardFinding(
                    id=uuid.uuid4().hex[:8],
                    rule_id="policy_ask",
                    category=GuardThreatCategory.RESOURCE_ABUSE,
                    severity=(
                        GuardSeverity.HIGH
                        if violation_msg
                        else GuardSeverity.INFO
                    ),
                    title=(
                        "Sandbox Violation — Approve Unsandboxed Execution?"
                        if violation_msg
                        else "Policy Approval Required"
                    ),
                    description=(
                        (
                            f"Governance reason: {governance_reason}"
                            if governance_reason
                            else ""
                        )
                        + (
                            f"\n\n\u26a0\ufe0f Sandbox violation: "
                            f"{violation_msg}"
                            f"\n\n**If you approve, this command will be "
                            f"re-executed WITHOUT sandbox isolation (full "
                            f"host access).** The kernel-level filesystem "
                            f"restrictions that blocked it will no longer "
                            f"apply."
                            if violation_msg
                            else ""
                        )
                    ),
                    tool_name=tool_name,
                    remediation=(
                        "Approve to re-run without sandbox (full host "
                        "access), or deny to block the command."
                        if violation_msg
                        else "Approve or deny this tool call"
                    ),
                    guardian="governance_policy",
                    metadata={
                        "target": target,
                        **(
                            {
                                "sandbox_violation": violation_msg,
                                "escalation": "sandbox_to_host",
                            }
                            if violation_msg
                            else {}
                        ),
                    },
                ),
            ],
            guardians_used=["governance_policy"],
        )

    svc = get_approval_service()
    tool_call_id = str(ctx.get("tool_call_id") or "")
    if session_id and tool_call_id:
        await svc.cancel_stale_pending_for_tool_call(session_id, tool_call_id)

    from ..config.context import get_f1_reasoning

    pending = await svc.create_pending(
        session_id=session_id,
        root_session_id=root_session_id,
        owner_agent_id=root_agent_id,
        user_id=user_id,
        channel=channel,
        agent_id=agent_id or "unknown",
        tool_name=tool_name,
        result=guard_result,
        timeout_seconds=timeout_seconds,
        extra={
            "tool_call": {
                "id": tool_call_id,
                "name": tool_name,
                "input": dict(params or {}),
            },
            "display": {
                "tool_name": tool_name,
                "tool_source": source,
                "exact_target": target,
                "similar_target": display_target,
                "is_generalized": display_target != target,
            },
            "channel_meta": ctx.get("channel_meta"),
            "_channel_instance": ctx.get("_channel_instance"),
            "reasoning": get_f1_reasoning(session_id),
            **(
                {"_spawn_subagent": True} if ctx.get("_spawn_subagent") else {}
            ),
        },
    )
    if tool_instance is not None:
        tool_instance._qp_approval_id = pending.request_id

    bridge_ready = await attach_pending_to_durable_task(
        ctx,
        pending,
        svc,
        agent_id=agent_id or "unknown",
        tool_name=tool_name,
        severity=guard_result.max_severity.value,
        input_data=dict(params or {}),
        source=ApprovalSource.TOOL,
        action="tool.execute",
        policy="governance_policy",
        display=ApprovalDisplay(
            title=f"Approve {tool_name}",
            summary=format_findings_summary(guard_result),
            target=target,
            provider=source,
        ),
    )
    if not bridge_ready:
        return PermissionDecision(
            behavior=PermissionBehavior.DENY,
            message=(
                "Tool approval could not be persisted safely."
                + _NO_RETRY_INSTRUCTION
            ),
        )
    interaction_ready = await attach_pending_to_interaction(
        ctx,
        pending,
        svc,
        source="governance_policy",
        input_data=dict(params or {}),
    )
    if not interaction_ready:
        await svc.resolve_request(
            pending.request_id,
            ApprovalDecision.DENIED,
        )
        return PermissionDecision(
            behavior=PermissionBehavior.DENY,
            message=(
                "Tool approval could not be persisted safely."
                + _NO_RETRY_INSTRUCTION
            ),
        )

    logger.info(
        "PolicyGuardedTool: awaiting approval for tool=%s session=%s "
        "request_id=%s target=%s",
        tool_name,
        session_id[:8] if session_id else "",
        pending.request_id[:8],
        target,
    )

    try:
        decision = await svc.wait_for_approval(
            pending,
            timeout_seconds,
        )
    except Exception as exc:
        logger.error(
            "PolicyGuardedTool: wait_for_approval crashed (%s); denying",
            exc,
            exc_info=True,
        )
        decision = ApprovalDecision.DENIED

    # Record user approve/deny result to audit log
    approved = decision == ApprovalDecision.APPROVED
    # The scope the user chose (set by resolve_request on the same pending
    # object). None = no choice offered → EXACT.
    scope = getattr(pending, "scope", None)
    scope_label = scope.value if scope else "exact"
    approval_decision = GovernanceDecision(
        action=GovernanceAction.ALLOW if approved else GovernanceAction.DENY,
        reason=(f"User Approve ({scope_label})" if approved else "User Deny"),
    )
    governor.audit(tc_spec, approval_decision)

    summary = format_findings_summary(guard_result)
    if decision == ApprovalDecision.APPROVED:
        # ── Record approved rule (skip for builtin ask) ──
        # SIMILAR → the generalized pattern; EXACT (default) → the literal
        # target the user actually approved. Widening is opt-in.
        rule_target = (
            generalized_target if scope == ApprovalScope.SIMILAR else target
        )
        await governor.add_approved_rule(
            tc_spec,
            generalized_target=rule_target,
        )
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message=f"Approved by user ({scope_label}).\n{summary}",
        )

    denial_msg = (
        f"User denied the request to run '{tool_name}'.\n{summary}"
        if decision == ApprovalDecision.DENIED
        else (
            f"Approval for '{tool_name}' timed out after "
            f"{timeout_seconds:g}s.\n{summary}"
        )
    )
    return PermissionDecision(
        behavior=PermissionBehavior.DENY,
        message=denial_msg + _NO_RETRY_INSTRUCTION,
    )
