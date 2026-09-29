# -*- coding: utf-8 -*-
"""Minimal loop Stop Gate Provider using only the public plugin SDK."""

from qwenpaw.plugins.sdk import (
    InvocationScope,
    StopGateAction,
    StopGateDecision,
    StopGateDefinition,
    StopGateHost,
    StopGateInput,
    StopGateSession,
)


class ReviewGateSession:
    """Request one review pass before allowing a turn to finish."""

    provider_id = "runtime-provider-kit.review-gates"

    def __init__(self) -> None:
        self._review_requested = False

    def list_gates(self) -> tuple[StopGateDefinition, ...]:
        """Return a deterministic provider-owned gate catalog."""
        return (
            StopGateDefinition(
                gate_id=f"{self.provider_id}.review-once",
                provider_id=self.provider_id,
                priority=120,
            ),
        )

    def is_active(self, gate_id: str) -> bool:
        """Keep this unscoped safety gate active for the invocation."""
        self._require_gate(gate_id)
        return True

    async def evaluate(
        self,
        gate_id: str,
        gate_input: StopGateInput,
    ) -> StopGateDecision:
        """Request one bounded review at the next safe loop boundary."""
        self._require_gate(gate_id)
        if self._review_requested:
            return StopGateDecision(
                action=StopGateAction.TERMINATE,
                reason="project review completed",
                final_message=gate_input.final_message,
            )
        self._review_requested = True
        return StopGateDecision(
            action=StopGateAction.INTERRUPT_AND_CONTINUE,
            continuation_message="Review the project answer once more.",
            reason="project review required",
            inject_on_tool_call=True,
        )

    async def start_turn(self) -> None:
        """Reset the one-review budget for a new user turn."""
        self._review_requested = False

    async def reset_conversation(self) -> None:
        """Reset provider-owned state after `/clear` or `/new`."""
        self._review_requested = False

    async def close(self) -> None:
        """Release no resources for this in-memory session."""

    def _require_gate(self, gate_id: str) -> None:
        expected_id = f"{self.provider_id}.review-once"
        if gate_id != expected_id:
            raise LookupError(gate_id)


class ReviewGateProvider:
    """Open one stateful review gate per invocation."""

    provider_id = ReviewGateSession.provider_id

    async def health_check(self) -> bool:
        """Report that this provider can be published."""
        return True

    async def open(
        self,
        scope: InvocationScope,
        host: StopGateHost,
    ) -> StopGateSession:
        """Create isolated state without retaining private host objects."""
        del scope, host
        return ReviewGateSession()


def create_provider() -> ReviewGateProvider:
    """Create the manifest contribution implementation."""
    return ReviewGateProvider()
