# -*- coding: utf-8 -*-
"""Minimal runner contribution implemented only with the stable SDK."""

from collections.abc import AsyncIterator

from qwenpaw.plugins.sdk import (
    CostAccountingMode,
    LocalAgentRunner,
    Run,
    RunnerSignal,
    RuntimeContext,
    TaskOrder,
)

RUNNER_ID = "task-insights.summary-runner"


async def _execute(
    order: TaskOrder,
    run: Run,
    context: RuntimeContext,
) -> AsyncIterator[RunnerSignal]:
    yield RunnerSignal(
        event_type="plugin.task-insights.summary",
        source=RUNNER_ID,
        payload={
            "objective_length": len(order.objective),
            "attempt": run.attempt,
            "registry_generation": context.registry_generation,
            "approval_level": context.approval_level.value,
            "strategy_id": context.strategy_id,
            "strategy_parameters": context.strategy_parameters,
        },
    )
    yield await context.artifact_emitter.emit(
        kind="task.summary",
        media_type="text/markdown",
        content=(
            f"# Task insight\n\nObjective length: {len(order.objective)}\n"
        ).encode("utf-8"),
        name="task-insight.md",
        evidence_claim="Task insight summary",
        metadata={
            "attempt": run.attempt,
            "strategy_id": context.strategy_id,
        },
    )


def create_runner() -> LocalAgentRunner:
    """Create the example runner without application-internal imports."""
    return LocalAgentRunner(
        RUNNER_ID,
        execute_context=_execute,
        cost_accounting=CostAccountingMode.ZERO,
    )
