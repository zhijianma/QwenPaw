# -*- coding: utf-8 -*-
"""Example Runtime Strategy implemented with the stable plugin SDK."""

from qwenpaw.plugins.sdk import JsonObject, RuntimeContext, TaskOrder

STRATEGY_ID = "task-insights.insight-strategy"


class InsightStrategy:
    """Prepare bounded public parameters for the example Runner."""

    strategy_id = STRATEGY_ID

    async def health_check(self) -> bool:
        """Report that the stateless Strategy is ready."""
        return True

    async def prepare(
        self,
        context: RuntimeContext,
        order: TaskOrder,
    ) -> JsonObject:
        """Return parameters that remain visible to the selected Runner."""
        return {
            "analysis_depth": "concise",
            "objective_length": len(order.objective),
            "registry_generation": context.registry_generation,
        }


def create_strategy() -> InsightStrategy:
    """Create the public-SDK Runtime Strategy contribution."""
    return InsightStrategy()
