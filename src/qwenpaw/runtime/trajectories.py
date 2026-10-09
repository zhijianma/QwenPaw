# -*- coding: utf-8 -*-
"""Correlation-scoped replay over existing semantic source facts."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid5

from ..kernel import (
    ConversationOutcome,
    ConversationOutcomeHistoryPort,
    ConversationOutcomeStatus,
    ConversationTrajectoryPage,
    ObservationCategory,
    ObservationHistoryPort,
    ObservationSource,
    ObservationStage,
    ObservationStatus,
    RuntimeObservation,
)
from .observation_index import LiteObservationIndex


def _outcome_status(
    status: ConversationOutcomeStatus,
) -> ObservationStatus:
    return {
        ConversationOutcomeStatus.ACHIEVED: ObservationStatus.SUCCEEDED,
        ConversationOutcomeStatus.PARTIAL: ObservationStatus.PARTIAL,
        ConversationOutcomeStatus.NOT_ACHIEVED: ObservationStatus.FAILED,
        ConversationOutcomeStatus.ABANDONED: ObservationStatus.CANCELLED,
    }[status]


def _outcome_observation(
    outcome: ConversationOutcome,
) -> RuntimeObservation:
    """Project one immutable Outcome without copying hidden reasoning."""
    return RuntimeObservation(
        observation_id=uuid5(
            outcome.outcome_id,
            "runtime-observation:outcome",
        ),
        category=ObservationCategory.OUTCOME,
        stage=ObservationStage.EVIDENCE,
        status=_outcome_status(outcome.status),
        source=ObservationSource(
            source_type="qwenpaw.conversation.outcome",
            source_id=str(outcome.outcome_id),
        ),
        task_id=outcome.task_id,
        run_id=outcome.run_id,
        chat_id=outcome.chat_id,
        invocation_id=outcome.invocation_id,
        correlation_id=outcome.correlation_id,
        registry_generation=outcome.registry_generation,
        title=f"Conversation outcome: {outcome.status.value}",
        facts={
            "producer_id": outcome.producer_id,
            "summary": outcome.summary,
            "artifact_ids": [str(item) for item in outcome.artifact_ids],
            "evidence_ids": [str(item) for item in outcome.evidence_ids],
            "verification_ids": [
                str(item) for item in outcome.verification_ids
            ],
            "supersedes_outcome_id": (
                str(outcome.supersedes_outcome_id)
                if outcome.supersedes_outcome_id is not None
                else None
            ),
        },
        occurred_at=outcome.created_at,
    )


class ConversationTrajectoryProjection:
    """Build a stable chronological replay from authoritative projections."""

    def __init__(
        self,
        observations: ObservationHistoryPort,
        outcomes: ConversationOutcomeHistoryPort,
        index: LiteObservationIndex,
    ) -> None:
        self._observations = observations
        self._outcomes = outcomes
        self._index = index

    async def _load(
        self,
        *,
        agent_id: str,
        conversation_id: str,
        correlation_id: UUID,
    ) -> tuple[RuntimeObservation, ...]:
        observations = await self._observations.scan_for_conversation(
            conversation_id,
        )
        outcome_history = await self._outcomes.list_for_correlation(
            agent_id=agent_id,
            conversation_id=conversation_id,
            correlation_id=correlation_id,
        )
        selected = [
            item
            for item in observations
            if item.chat_id == conversation_id
            and item.correlation_id == correlation_id
        ]
        selected.extend(
            _outcome_observation(outcome) for outcome in outcome_history
        )
        selected.sort(
            key=lambda item: (item.occurred_at, str(item.observation_id)),
        )
        return tuple(selected)

    async def page_for_correlation(
        self,
        *,
        agent_id: str,
        conversation_id: str,
        correlation_id: UUID,
        limit: int = 100,
        cursor: str | None = None,
    ) -> ConversationTrajectoryPage:
        """Return one fixed-snapshot trajectory page in replay order."""
        if not agent_id.strip() or not conversation_id.strip():
            raise ValueError("trajectory owner cannot be empty")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        observations = await self._load(
            agent_id=agent_id,
            conversation_id=conversation_id,
            correlation_id=correlation_id,
        )
        owner = f"trajectory:{conversation_id}:{correlation_id}"
        entries, next_cursor = await self._index.sync_and_page(
            owner,
            observations,
            limit=limit,
            cursor=cursor,
            ascending=True,
        )
        by_id = {str(item.observation_id): item for item in observations}
        missing = [
            item.observation_id
            for item in entries
            if item.observation_id not in by_id
        ]
        if missing:
            raise RuntimeError(
                "trajectory index references unavailable source facts",
            )
        return ConversationTrajectoryPage(
            chat_id=conversation_id,
            correlation_id=correlation_id,
            items=tuple(by_id[item.observation_id] for item in entries),
            next_cursor=next_cursor,
        )


def lite_conversation_trajectory(
    workspace_dir: Path,
    *,
    observations: ObservationHistoryPort,
    outcomes: ConversationOutcomeHistoryPort,
) -> ConversationTrajectoryProjection:
    """Return the Lite trajectory projection over existing source facts."""
    return ConversationTrajectoryProjection(
        observations,
        outcomes,
        LiteObservationIndex(
            Path(workspace_dir) / ".qwenpaw" / "lite" / "trajectories.sqlite3",
        ),
    )


__all__ = [
    "ConversationTrajectoryProjection",
    "lite_conversation_trajectory",
]
