# -*- coding: utf-8 -*-
"""Tests for durable Chat-owned Goal execution state."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from qwenpaw.kernel import (
    CapabilityProviderKind,
    ConversationOutcomeStatus,
    GoalExecution,
    GoalExecutionConflictError,
    GoalExecutionStatus,
    InvocationScope,
)
from qwenpaw.modes.goal.goal_mode import GoalMode
from qwenpaw.modes.goal.gates import RubricGate
from qwenpaw.loop.gates import (
    RubricEvaluation,
    RubricVerdict,
    StopAction,
)
from qwenpaw.runtime.goals import SQLiteGoalExecutionStore
from qwenpaw.runtime.outcome_context import scoped_outcome_context
from qwenpaw.runtime.outcome_hosts import provider_outcome_host


def _scope(tmp_path, *, invocation_id=None) -> InvocationScope:
    return InvocationScope(
        invocation_id=invocation_id or uuid4(),
        correlation_id=uuid4(),
        agent_id="default",
        conversation_id="chat-a",
        session_id="transport-a",
        root_agent_id="default",
        root_session_id="transport-a",
        workspace_dir=str(tmp_path),
        registry_generation=3,
    )


def _execution() -> GoalExecution:
    return GoalExecution(
        agent_id="default",
        conversation_id="chat-a",
        correlation_id=uuid4(),
        objective="Finish the migration",
        max_iterations=20,
        token_budget=300000,
    )


@pytest.mark.asyncio
async def test_goal_store_uses_compare_and_swap(tmp_path) -> None:
    store = SQLiteGoalExecutionStore(tmp_path / "goals.db")
    first = await store.write(_execution(), expected_revision=0)

    assert first.revision == 1
    assert (
        await store.active_correlation(
            agent_id="default",
            conversation_id="chat-a",
        )
        == first.correlation_id
    )
    assert (
        await store.read(
            agent_id="default",
            conversation_id="chat-a",
        )
        == first
    )

    updated = await store.write(
        first.model_copy(update={"iteration": 1}),
        expected_revision=1,
    )
    assert updated.revision == 2
    assert updated.iteration == 1

    with pytest.raises(GoalExecutionConflictError):
        await store.write(first, expected_revision=1)


@pytest.mark.asyncio
async def test_goal_store_lists_only_pending_outcomes_by_agent(
    tmp_path,
) -> None:
    store = SQLiteGoalExecutionStore(tmp_path / "goals.db")
    active = await store.write(_execution(), expected_revision=0)
    pending = active.model_copy(
        update={
            "status": GoalExecutionStatus.OUTCOME_PENDING,
            "outcome_id": uuid4(),
            "outcome_status": ConversationOutcomeStatus.ACHIEVED,
        },
    )
    pending = await store.write(pending, expected_revision=active.revision)
    other = _execution().model_copy(
        update={
            "agent_id": "other",
            "conversation_id": "chat-other",
        },
    )
    await store.write(other, expected_revision=0)

    assert await store.list_pending(agent_id="default") == (pending,)
    assert await store.list_pending(agent_id="other") == ()


@pytest.mark.asyncio
async def test_goal_store_rejects_active_replacement_and_contract_drift(
    tmp_path,
) -> None:
    store = SQLiteGoalExecutionStore(tmp_path / "goals.db")
    first = await store.write(_execution(), expected_revision=0)

    with pytest.raises(
        GoalExecutionConflictError,
        match="active goal cannot be replaced",
    ):
        await store.write(
            _execution().model_copy(update={"revision": 1}),
            expected_revision=1,
        )

    with pytest.raises(
        GoalExecutionConflictError,
        match="contract changed",
    ):
        await store.write(
            first.model_copy(update={"objective": "Different objective"}),
            expected_revision=1,
        )


@pytest.mark.asyncio
async def test_goal_mode_restores_by_chat_after_new_process(tmp_path) -> None:
    store = SQLiteGoalExecutionStore(tmp_path / "goals.db")
    scope = _scope(tmp_path)
    first_mode = GoalMode(store)
    context = SimpleNamespace(invocation_scope=scope)

    with scoped_outcome_context(
        "chat-a",
        None,
        agent_id="default",
        correlation_id=scope.correlation_id,
    ):
        assert await first_mode.create_current_goal(
            "Finish the migration",
            max_tokens=1000,
            ctx=context,
        )

    restarted_mode = GoalMode(
        SQLiteGoalExecutionStore(tmp_path / "goals.db"),
    )
    await restarted_mode.on_turn_start(context)

    restored = restarted_mode.get_session("chat-a")
    assert restored is not None
    assert restored.goal == "Finish the migration"
    assert restored.correlation_id == scope.correlation_id
    assert restored.revision == 1


@pytest.mark.asyncio
async def test_goal_progress_is_durable_between_invocations(tmp_path) -> None:
    store = SQLiteGoalExecutionStore(tmp_path / "goals.db")
    scope = _scope(tmp_path)
    mode = GoalMode(store)
    context = SimpleNamespace(invocation_scope=scope)

    with scoped_outcome_context(
        "chat-a",
        None,
        agent_id="default",
        correlation_id=scope.correlation_id,
    ):
        assert await mode.create_current_goal(
            "Finish the migration",
            max_tokens=1000,
            ctx=context,
        )
        session = mode.active_session()
        assert session is not None
        session.iteration = 4
        session.tokens_used = 321
        assert await mode.persist_current()

    restarted = GoalMode(SQLiteGoalExecutionStore(tmp_path / "goals.db"))
    await restarted.on_turn_start(context)
    restored = restarted.get_session("chat-a")
    assert restored is not None
    assert restored.iteration == 4
    assert restored.tokens_used == 321


@pytest.mark.asyncio
async def test_clear_abandons_only_current_chat_goal(tmp_path) -> None:
    store = SQLiteGoalExecutionStore(tmp_path / "goals.db")
    scope = _scope(tmp_path)
    mode = GoalMode(store)
    context = SimpleNamespace(invocation_scope=scope)

    with scoped_outcome_context(
        "chat-a",
        None,
        agent_id="default",
        correlation_id=scope.correlation_id,
    ):
        assert await mode.create_current_goal(
            "Finish the migration",
            max_tokens=1000,
            ctx=context,
        )
        await mode.on_conversation_reset(context)

    persisted = await store.read(
        agent_id="default",
        conversation_id="chat-a",
    )
    assert persisted is not None
    assert persisted.status is GoalExecutionStatus.ABANDONED
    assert (
        await store.active_correlation(
            agent_id="default",
            conversation_id="chat-a",
        )
        is None
    )
    assert mode.get_session("chat-a") is None


class _FailFinalWriteStore:
    """Fail once after Outcome succeeds but before Goal becomes terminal."""

    def __init__(self, inner: SQLiteGoalExecutionStore) -> None:
        self.inner = inner
        self.failed = False

    async def read(self, **kwargs):
        return await self.inner.read(**kwargs)

    async def write(self, execution, *, expected_revision):
        if (
            execution.status is GoalExecutionStatus.COMPLETED
            and not self.failed
        ):
            self.failed = True
            raise RuntimeError("simulated crash window")
        return await self.inner.write(
            execution,
            expected_revision=expected_revision,
        )


@pytest.mark.asyncio
async def test_pending_goal_reconciles_existing_outcome_after_restart(
    tmp_path,
) -> None:
    workspace = SimpleNamespace(workspace_dir=tmp_path)
    durable_store = SQLiteGoalExecutionStore(tmp_path / "goals.db")
    first_scope = _scope(tmp_path)
    first_host = provider_outcome_host(
        workspace,
        first_scope,
        producer_id="qwenpaw.system.goal-mode",
        provider_kind=CapabilityProviderKind.SYSTEM,
    )
    assert first_host is not None
    first_mode = GoalMode(_FailFinalWriteStore(durable_store))
    first_context = SimpleNamespace(invocation_scope=first_scope)

    with scoped_outcome_context(
        "chat-a",
        first_host,
        agent_id="default",
        correlation_id=first_scope.correlation_id,
    ):
        assert await first_mode.create_current_goal(
            "Finish the migration",
            max_tokens=1000,
            ctx=first_context,
        )
        assert not await first_mode.finish_current(
            status=ConversationOutcomeStatus.ACHIEVED,
            verdict="satisfied",
        )

    pending = await durable_store.read(
        agent_id="default",
        conversation_id="chat-a",
    )
    assert pending is not None
    assert pending.status is GoalExecutionStatus.OUTCOME_PENDING

    second_scope = first_scope.model_copy(
        update={
            "invocation_id": uuid4(),
            "registry_generation": 4,
        },
    )
    second_host = provider_outcome_host(
        workspace,
        second_scope,
        producer_id="qwenpaw.system.goal-mode",
        provider_kind=CapabilityProviderKind.SYSTEM,
    )
    assert second_host is not None
    restarted_mode = GoalMode(
        SQLiteGoalExecutionStore(tmp_path / "goals.db"),
    )

    with scoped_outcome_context(
        "chat-a",
        second_host,
        agent_id="default",
        correlation_id=second_scope.correlation_id,
    ):
        await restarted_mode.on_turn_start(
            SimpleNamespace(invocation_scope=second_scope),
        )

    terminal = await durable_store.read(
        agent_id="default",
        conversation_id="chat-a",
    )
    assert terminal is not None
    assert terminal.status is GoalExecutionStatus.COMPLETED
    assert terminal.outcome_id == pending.outcome_id
    assert restarted_mode.get_session("chat-a") is None


@pytest.mark.asyncio
async def test_satisfied_rubric_declares_outcome_before_terminating(
    tmp_path,
) -> None:
    workspace = SimpleNamespace(workspace_dir=tmp_path)
    store = SQLiteGoalExecutionStore(tmp_path / "goals.db")
    scope = _scope(tmp_path)
    host = provider_outcome_host(
        workspace,
        scope,
        producer_id="qwenpaw.system.goal-mode",
        provider_kind=CapabilityProviderKind.SYSTEM,
    )
    assert host is not None
    mode = GoalMode(store)
    context = SimpleNamespace(invocation_scope=scope)

    class _SatisfiedRubric:
        async def evaluate(self, **_kwargs):
            return RubricEvaluation(
                iteration=1,
                verdict=RubricVerdict.SATISFIED,
                explanation="All acceptance checks passed.",
            )

    with scoped_outcome_context(
        "chat-a",
        host,
        agent_id="default",
        correlation_id=scope.correlation_id,
    ):
        assert await mode.create_current_goal(
            "Finish the migration",
            max_tokens=1000,
            ctx=context,
        )
        decision = await RubricGate(mode, _SatisfiedRubric()).check({})

    persisted = await store.read(
        agent_id="default",
        conversation_id="chat-a",
    )
    assert decision.action is StopAction.TERMINATE
    assert persisted is not None
    assert persisted.status is GoalExecutionStatus.COMPLETED
