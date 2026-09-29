# -*- coding: utf-8 -*-
"""Example Planner implemented only with the stable plugin SDK."""

from qwenpaw.plugins.sdk import PlanStep, TaskOrder

PLANNER_ID = "task-insights.insight-planner"


class InsightPlanner:
    """Create a small evidence-oriented plan for the example plugin."""

    planner_id = PLANNER_ID

    async def health_check(self) -> bool:
        """Report that the stateless Planner is ready."""
        return True

    async def plan(self, order: TaskOrder) -> tuple[PlanStep, ...]:
        """Return ordered steps without depending on application internals."""
        inspect_step = PlanStep(
            title="Inspect task objective",
            objective=f"Inspect the objective: {order.objective}",
        )
        publish_step = PlanStep(
            title="Publish task insight",
            objective="Publish a concise evidence-backed task insight",
            depends_on=(inspect_step.step_id,),
        )
        return inspect_step, publish_step


def create_planner() -> InsightPlanner:
    """Create the public-SDK Planner contribution."""
    return InsightPlanner()
