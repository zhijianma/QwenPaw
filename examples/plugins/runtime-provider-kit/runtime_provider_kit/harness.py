# -*- coding: utf-8 -*-
"""Minimal Harness Runner using the public SDK execution envelope."""

from collections.abc import AsyncIterator

from qwenpaw.plugins.sdk import (
    CostAccountingMode,
    LocalAgentRunner,
    Run,
    RunnerSignal,
    RuntimeContext,
    TaskOrder,
)

RUNNER_ID = "runtime-provider-kit.echo-harness"


async def _execute(
    order: TaskOrder,
    run: Run,
    context: RuntimeContext,
) -> AsyncIterator[RunnerSignal]:
    """Translate one external-style result into kernel signals."""
    yield RunnerSignal(
        event_type="plugin.runtime-provider-kit.harness.completed",
        source=RUNNER_ID,
        payload={
            "objective": order.objective,
            "attempt": run.attempt,
            "registry_generation": context.registry_generation,
        },
    )
    yield await context.artifact_emitter.emit(
        kind="agent.response",
        media_type="text/markdown",
        content=order.objective.encode("utf-8"),
        name="task-result.md",
        evidence_claim="Example Harness completed the requested objective",
        metadata={"runner_id": RUNNER_ID},
    )


def create_runner() -> LocalAgentRunner:
    """Create a runner pinned and supervised by the host runtime."""
    return LocalAgentRunner(
        RUNNER_ID,
        execute_context=_execute,
        cost_accounting=CostAccountingMode.ZERO,
    )
