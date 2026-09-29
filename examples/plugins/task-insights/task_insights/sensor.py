# -*- coding: utf-8 -*-
"""Approval-gated example sensor implemented with the stable SDK."""

from qwenpaw.plugins.sdk import Proposal, RiskLevel, SensorContext


class ReviewSensor:
    """Propose one harmless review task when explicitly polled."""

    @property
    def sensor_id(self) -> str:
        """Return the manifest-qualified contribution identity."""
        return "task-insights.review-sensor"

    async def propose(self) -> tuple[Proposal, ...]:
        """Return a bounded proposal batch without executing work."""
        return self._proposals()

    async def propose_context(
        self,
        context: SensorContext,
    ) -> tuple[Proposal, ...]:
        """Return proposals with host-owned Agent and generation context."""
        return self._proposals(
            metadata={
                "agent_id": context.agent_id,
                "registry_generation": context.registry_generation,
            },
        )

    def _proposals(
        self,
        *,
        metadata: dict | None = None,
    ) -> tuple[Proposal, ...]:
        """Build one deterministic proposal batch."""
        return (
            Proposal(
                source=self.sensor_id,
                objective="Review the latest task outcome",
                rationale_summary=(
                    "A manual Task Insights sensor poll requested review"
                ),
                risk=RiskLevel.LOW,
                metadata=metadata or {},
            ),
        )


def create_sensor() -> ReviewSensor:
    """Create the sensor contribution without application imports."""
    return ReviewSensor()
