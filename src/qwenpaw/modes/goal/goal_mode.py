# -*- coding: utf-8 -*-
"""GoalMode — QwenPaw's built-in persistent loop mode.

Similar to Codex /goal: user sets a goal, agent works
until the rubric grader confirms completion or budget
is exhausted.

Inherits ``AgentMode`` so it plugs into the standard
``builtin_mode_clses`` bootstrap — all registration
stays inside this file and ``modes/goal/``.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from agentscope.message import Msg, TextBlock

from ..base import AgentMode, find_active_explicit_mode
from ...app.agent_context import (
    get_current_session_id,
)
from ...kernel import (
    ConversationOutcomeRequest,
    ConversationOutcomeStatus,
    GoalExecution,
    GoalExecutionStatus,
)
from ...loop.gates import (
    GoalStatusRubric,
    StopHandler,
    StopHandlerRegistration,
)
from ...runtime.hooks import HookBase, HookContext
from ...runtime.slash_command_registry import (
    CommandSpec,
)
from .gates import GoalBudgetGate, GoalTurnGate, RubricGate
from .helpers import (
    create_completion_gate,
    create_doom_loop_gate,
    register_goal_tools_governance,
    rewrite_user_msg,
)
from .prompts import (
    CONTINUATION_PROMPT,
    INITIAL_GOAL_PROMPT,
)

if TYPE_CHECKING:
    from ...kernel import GoalExecutionStore
    from ...runtime.prompt_manager import (
        PromptContributor,
    )

logger = logging.getLogger(__name__)

DEFAULT_MAX_ITERATIONS = 20
DEFAULT_MAX_TOKENS = 300000


@dataclass
class GoalSession:
    """Runtime state for an active /goal session."""

    goal: str
    active: bool = True
    iteration: int = 0
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    max_tokens: int = DEFAULT_MAX_TOKENS
    tokens_used: int = 0
    last_verdict: str = ""
    last_feedback: str = ""
    goal_id: UUID = field(default_factory=uuid4)
    agent_id: str = ""
    conversation_id: str = ""
    correlation_id: UUID | None = None
    revision: int = 0
    status: GoalExecutionStatus = GoalExecutionStatus.ACTIVE
    outcome_id: UUID | None = None
    outcome_status: ConversationOutcomeStatus | None = None
    started_at: float = field(
        default_factory=time.time,
    )


class GoalMode(AgentMode):
    """Built-in /goal mode (AgentMode subclass).

    Registers /goal and /cancel slash commands. When
    active, three gates in the universal StopHandler
    control loop termination:
    1. GoalTurnGate — hard turn limit + completion
    2. GoalBudgetGate — token budget limit
    3. RubricGate — rubric evaluation (LLM-based)

    This is the ONLY built-in loop mode. All other
    loops (ralph, ultrawork, etc.) are plugins.
    """

    name = "goal"

    def __init__(self, store: GoalExecutionStore | None = None) -> None:
        self._sessions: dict[str, GoalSession] = {}
        self._default_max_iterations = DEFAULT_MAX_ITERATIONS
        self._default_max_tokens = DEFAULT_MAX_TOKENS
        self._handler: StopHandler | None = None
        self._store = store

    @property
    def sessions(self) -> dict[str, GoalSession]:
        """Expose sessions for sibling modules."""
        return self._sessions

    @property
    def default_max_tokens(self) -> int:
        """Default token budget for new goals."""
        return self._default_max_tokens

    def active_session(
        self,
    ) -> GoalSession | None:
        """Return the active goal for the current execution identity.

        Chat invocations use ``ChatSpec.id``. Adapters without a stable Chat
        retain the transport-session compatibility fallback.
        """
        key = self.current_execution_key()
        if key is None:
            return None
        s = self._sessions.get(key)
        if s is not None and s.active:
            return s
        return None

    def session_by_ctx_var(
        self,
    ) -> Optional[GoalSession]:
        """Return goal state by current execution identity (any status).

        Inactive state remains visible so gates can distinguish a declared
        outcome from an active continuation.
        """
        key = self.current_execution_key()
        if key is None:
            return None
        return self._sessions.get(key)

    def deactivate(self) -> None:
        """Remove the current goal from the execution identity.

        Called after goal completion to prevent stale
        sessions from blocking subsequent messages.
        """
        key = self.current_execution_key()
        if key is not None:
            self._sessions.pop(key, None)

    async def on_conversation_reset(
        self,
        ctx: HookContext,
    ) -> None:
        """Clear the current Chat-owned goal on /new or /clear."""
        key = self._context_execution_key(ctx)
        invocation = getattr(ctx, "invocation_scope", None)
        conversation_id = getattr(invocation, "conversation_id", None)
        agent_id = getattr(invocation, "agent_id", None)
        if self._store is not None and conversation_id and agent_id:
            current = await self._store.read(
                agent_id=agent_id,
                conversation_id=conversation_id,
            )
            if current is not None and current.status in {
                GoalExecutionStatus.ACTIVE,
                GoalExecutionStatus.OUTCOME_PENDING,
            }:
                abandoned = current.model_copy(
                    update={
                        "status": GoalExecutionStatus.ABANDONED,
                        "outcome_id": None,
                        "outcome_status": None,
                        "last_verdict": "abandoned",
                    },
                )
                await self._store.write(
                    abandoned,
                    expected_revision=current.revision,
                )
        self._sessions.pop(key, None)
        if self._handler is not None:
            self._handler.reset_session()

    async def on_turn_start(self, ctx: HookContext) -> None:
        """Restore durable Goal state before stop-gate scope selection."""
        invocation = ctx.invocation_scope
        if (
            self._store is None
            or invocation is None
            or invocation.conversation_id is None
        ):
            return
        execution = await self._store.read(
            agent_id=invocation.agent_id,
            conversation_id=invocation.conversation_id,
        )
        key = invocation.conversation_id
        if execution is None or execution.status in {
            GoalExecutionStatus.COMPLETED,
            GoalExecutionStatus.BLOCKED,
            GoalExecutionStatus.ABANDONED,
            GoalExecutionStatus.EXHAUSTED,
        }:
            self._sessions.pop(key, None)
            return
        self._sessions[key] = self._session_from_execution(execution)
        if execution.status is GoalExecutionStatus.OUTCOME_PENDING:
            await self._finish_pending(self._sessions[key])

    # ---- AgentMode interface ----

    def commands(self) -> list[CommandSpec]:
        """Return /goal command spec."""
        return [
            CommandSpec(
                name="goal",
                handler=self._activate_handler,
                category="builtin",
                help_text=("Set a goal \u2014 agent works " "until done."),
                metadata={"builtin": True},
            ),
        ]

    def tools(self) -> list:
        """Return goal tools: get/create/update."""
        from ...runtime.tool_registry import (
            ToolDescriptor,
        )
        from .tools import (
            make_create_goal,
            make_get_goal,
            make_update_goal,
        )

        return [
            ToolDescriptor(
                name="get_goal",
                func=make_get_goal(self),
                requires_modes=("goal",),
                description=(
                    "Get the current goal status, " "budgets, and usage."
                ),
            ),
            ToolDescriptor(
                name="create_goal",
                func=make_create_goal(self),
                requires_modes=("goal",),
                description=(
                    "Create a goal only when " "explicitly requested."
                ),
            ),
            ToolDescriptor(
                name="update_goal",
                func=make_update_goal(self),
                requires_modes=("goal",),
                description=("Mark goal as complete " "or blocked."),
            ),
        ]

    def hooks(self) -> list[HookBase]:
        """No bypass hooks — Gate controls."""
        return []

    def prompt_contributors(
        self,
    ) -> list["PromptContributor"]:
        """Return goal-mode prompt contributor."""
        from .contributor import (
            GoalPromptContributor,
        )

        return [
            GoalPromptContributor(owner=self),
        ]

    def setup(self, workspace: object) -> None:
        """Register gates into the goal-scoped handler."""
        super().setup(workspace)

        from ...runtime.goals import lite_goal_execution_store

        if self._store is None:
            self._store = lite_goal_execution_store(workspace.workspace_dir)
        setattr(workspace, "conversation_correlation_resolver", self._store)

        goal_config = workspace.config.running.loop.goal
        self._default_max_iterations = goal_config.max_iterations
        self._default_max_tokens = goal_config.max_tokens

        handler = StopHandler()
        self._handler = handler
        workspace.plugins.stop_handlers.append(
            StopHandlerRegistration(
                plugin_id="__goal_mode__",
                handler=handler,
                priority=0,
                name="goal-stop-handler",
                scope="goal",
                is_active=lambda: self.active_session() is not None,
            ),
        )
        rubric = GoalStatusRubric(
            get_session_fn=self.session_by_ctx_var,
        )
        doom_gate = create_doom_loop_gate(workspace)
        if doom_gate is not None:
            handler.register(doom_gate)
        handler.register(
            GoalTurnGate(
                self,
                max_iterations=self._default_max_iterations,
            ),
        )
        handler.register(GoalBudgetGate(self))
        handler.register(RubricGate(self, rubric))

        completion_gate = create_completion_gate(
            workspace,
        )
        if completion_gate is not None:
            handler.register(completion_gate)

        register_goal_tools_governance()

    def is_active(self, ctx: Any) -> bool:
        """Goal mode is active when session live."""
        return self.active_session() is not None

    # ---- slash command handlers ----

    async def _activate_handler(
        self,
        ctx: Any,
        args: str,
    ) -> Optional[Msg]:
        """Handle /goal <task description>.

        Returns None so the Runtime does NOT skip
        the agent. Rewrites user message to bare text.
        """
        if not args or not args.strip():
            return Msg(
                name="system",
                content=[
                    TextBlock(
                        type="text",
                        text=(
                            "Usage: /goal <description>"
                            "\nExample: /goal fix all "
                            "failing tests"
                        ),
                    ),
                ],
                role="system",
            )

        conflict = find_active_explicit_mode(ctx)
        if conflict is not None:
            return Msg(
                name="system",
                content=[
                    TextBlock(
                        type="text",
                        text=(
                            f"End the active {conflict} mode before "
                            f"starting /goal."
                        ),
                    ),
                ],
                role="system",
            )

        goal_text = args.strip()
        session_key = self._current_session_key(
            ctx,
        )
        created = await self.create_current_goal(
            goal_text,
            max_tokens=self._default_max_tokens,
            ctx=ctx,
        )
        if not created:
            return Msg(
                name="system",
                role="system",
                content=[
                    TextBlock(
                        type="text",
                        text="An active goal already exists for this Chat.",
                    ),
                ],
            )

        logger.info(
            "Goal mode activated: %s (key=%s)",
            goal_text[:80],
            session_key,
        )

        rewrite_user_msg(ctx, goal_text)
        return None

    # ---- prompt / session helpers ----

    def prompt_provider(
        self,
        agent: Any,  # pylint: disable=unused-argument
    ) -> str:
        """Provide goal-mode skill prompt.

        First turn uses INITIAL_GOAL_PROMPT;
        subsequent turns use CONTINUATION_PROMPT.
        """
        session = self.active_session()
        if session is None:
            return ""

        if session.iteration == 0:
            return INITIAL_GOAL_PROMPT.format(
                objective=session.goal,
                max_iterations=(session.max_iterations),
                token_budget=session.max_tokens,
            )

        remaining = max(
            0,
            session.max_tokens - session.tokens_used,
        )
        return CONTINUATION_PROMPT.format(
            objective=session.goal,
            iteration=session.iteration,
            max_iterations=(session.max_iterations),
            tokens_used=session.tokens_used,
            token_budget=session.max_tokens,
            remaining_tokens=remaining,
        )

    @staticmethod
    def _current_session_key(
        ctx: Any,
    ) -> str:
        """Prefer ChatSpec.id and retain session fallback for adapters."""
        invocation = getattr(ctx, "invocation_scope", None)
        conversation_id = getattr(invocation, "conversation_id", None)
        if conversation_id:
            return str(conversation_id)
        if isinstance(ctx, dict):
            return ctx.get(
                "session_id",
                "default",
            )
        return getattr(
            ctx,
            "session_id",
            "default",
        )

    @staticmethod
    def _context_execution_key(ctx: Any) -> str:
        """Resolve a Hook context without consulting ambient state."""
        return GoalMode._current_session_key(ctx)

    @staticmethod
    def current_execution_key() -> str | None:
        """Use the current Chat identity before legacy session identity."""
        from ...runtime.outcome_context import current_outcome_context

        conversation_id = current_outcome_context().conversation_id
        return conversation_id or get_current_session_id()

    async def finish_current(
        self,
        *,
        status: ConversationOutcomeStatus,
        verdict: str,
    ) -> bool:
        """Persist business disposition before ending a Chat goal."""
        session = self.active_session()
        if session is None:
            return False
        if session.status is GoalExecutionStatus.OUTCOME_PENDING:
            if session.outcome_status is not status:
                return False
        else:
            session.outcome_id = uuid5(
                NAMESPACE_URL,
                f"qwenpaw:goal:{session.goal_id}:{status.value}",
            )
            session.outcome_status = status
            session.status = GoalExecutionStatus.OUTCOME_PENDING
            session.last_verdict = verdict
            if not await self.persist_current():
                session.status = GoalExecutionStatus.ACTIVE
                session.outcome_id = None
                session.outcome_status = None
                return False
        return await self._finish_pending(session)

    async def create_current_goal(
        self,
        objective: str,
        *,
        max_tokens: int,
        ctx: Any | None = None,
    ) -> bool:
        """Create one Chat-owned Goal or a legacy in-memory fallback."""
        from ...runtime.outcome_context import current_outcome_context

        outcome_context = current_outcome_context()
        invocation = getattr(ctx, "invocation_scope", None)
        conversation_id = (
            getattr(invocation, "conversation_id", None)
            or outcome_context.conversation_id
        )
        agent_id = (
            getattr(invocation, "agent_id", None)
            or outcome_context.agent_id
            or ""
        )
        correlation_id = (
            getattr(invocation, "correlation_id", None)
            or getattr(invocation, "invocation_id", None)
            or outcome_context.correlation_id
        )
        fallback_key = (
            self._current_session_key(ctx)
            if ctx is not None
            else self.current_execution_key()
        )
        key = str(conversation_id or fallback_key or "")
        if not key:
            return False
        existing = self._sessions.get(key)
        if existing is not None and existing.active:
            return False
        session = GoalSession(
            goal=objective.strip(),
            max_iterations=self._default_max_iterations,
            max_tokens=max_tokens,
            agent_id=str(agent_id),
            conversation_id=str(conversation_id or ""),
            correlation_id=correlation_id,
        )
        if (
            self._store is not None
            and conversation_id
            and agent_id
            and correlation_id is not None
        ):
            current = await self._store.read(
                agent_id=str(agent_id),
                conversation_id=str(conversation_id),
            )
            if current is not None and current.status in {
                GoalExecutionStatus.ACTIVE,
                GoalExecutionStatus.OUTCOME_PENDING,
            }:
                self._sessions[key] = self._session_from_execution(current)
                return False
            session.revision = current.revision if current is not None else 0
            persisted = await self._store.write(
                self._execution_from_session(session),
                expected_revision=session.revision,
            )
            session = self._session_from_execution(persisted)
        self._sessions[key] = session
        return True

    async def persist_current(self) -> bool:
        """CAS-persist current progress when the Goal is Chat-owned."""
        session = self.session_by_ctx_var()
        if session is None or not session.conversation_id:
            return True
        if self._store is None:
            return True
        try:
            persisted = await self._store.write(
                self._execution_from_session(session),
                expected_revision=session.revision,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Goal progress persistence failed")
            return False
        session.revision = persisted.revision
        session.started_at = persisted.started_at.timestamp()
        self._sessions[session.conversation_id] = session
        return True

    async def _finish_pending(self, session: GoalSession) -> bool:
        """Idempotently declare and close one prepared Goal outcome."""
        from ...runtime.outcome_context import current_outcome_context

        if session.outcome_id is None or session.outcome_status is None:
            return False
        outcome_context = current_outcome_context()
        if outcome_context.conversation_id is not None:
            if outcome_context.host is None:
                logger.error("Goal outcome host is unavailable")
                return False
            try:
                await outcome_context.host.declare(
                    ConversationOutcomeRequest(
                        outcome_id=session.outcome_id,
                        status=session.outcome_status,
                        summary=self._outcome_summary(
                            session.outcome_status,
                        ),
                    ),
                )
            except Exception:  # noqa: BLE001
                logger.exception("Goal outcome declaration failed")
                return False
        session.status = (
            GoalExecutionStatus.COMPLETED
            if session.outcome_status is ConversationOutcomeStatus.ACHIEVED
            else GoalExecutionStatus.BLOCKED
        )
        if not await self.persist_current():
            session.status = GoalExecutionStatus.OUTCOME_PENDING
            return False
        session.active = False
        if session.status is GoalExecutionStatus.COMPLETED:
            self.deactivate()
        return True

    @staticmethod
    def _outcome_summary(status: ConversationOutcomeStatus) -> str:
        from ...runtime.goals import goal_outcome_summary

        return goal_outcome_summary(status)

    @staticmethod
    def _session_from_execution(execution: GoalExecution) -> GoalSession:
        return GoalSession(
            goal=execution.objective,
            active=execution.status
            in {
                GoalExecutionStatus.ACTIVE,
                GoalExecutionStatus.OUTCOME_PENDING,
            },
            iteration=execution.iteration,
            max_iterations=execution.max_iterations,
            max_tokens=execution.token_budget,
            tokens_used=execution.tokens_used,
            last_verdict=execution.last_verdict,
            last_feedback=execution.last_feedback,
            goal_id=execution.goal_id,
            agent_id=execution.agent_id,
            conversation_id=execution.chat_id,
            correlation_id=execution.correlation_id,
            revision=execution.revision,
            status=execution.status,
            outcome_id=execution.outcome_id,
            outcome_status=execution.outcome_status,
            started_at=execution.started_at.timestamp(),
        )

    @staticmethod
    def _execution_from_session(session: GoalSession) -> GoalExecution:
        assert session.correlation_id is not None
        return GoalExecution(
            goal_id=session.goal_id,
            agent_id=session.agent_id,
            chat_id=session.conversation_id,
            correlation_id=session.correlation_id,
            objective=session.goal,
            status=session.status,
            iteration=session.iteration,
            max_iterations=session.max_iterations,
            tokens_used=session.tokens_used,
            token_budget=session.max_tokens,
            last_verdict=session.last_verdict,
            last_feedback=session.last_feedback,
            outcome_id=session.outcome_id,
            outcome_status=session.outcome_status,
            revision=session.revision,
            started_at=datetime.fromtimestamp(
                session.started_at,
                timezone.utc,
            ),
        )

    def get_session(
        self,
        session_key: str = "default",
    ) -> Optional[GoalSession]:
        """Get goal session (for status display)."""
        return self._sessions.get(session_key)

    def get_all_active_sessions(
        self,
    ) -> dict[str, GoalSession]:
        """Return all active sessions."""
        return {k: v for k, v in self._sessions.items() if v.active}


__all__ = ["GoalMode", "GoalSession"]
