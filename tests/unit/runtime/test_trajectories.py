# -*- coding: utf-8 -*-
"""Tests for correlation-scoped Conversation trajectory replay."""

from datetime import timedelta
from uuid import uuid4

import pytest

from qwenpaw.kernel import (
    ConversationOutcome,
    ConversationOutcomeStatus,
    ObservationCategory,
    ObservationSource,
    ObservationStage,
    ObservationStatus,
    RuntimeObservation,
)
from qwenpaw.kernel.models import utc_now
from qwenpaw.runtime.observation_index import (
    LiteObservationIndex,
    ObservationCursorError,
)
from qwenpaw.runtime.outcomes import lite_conversation_outcome_store
from qwenpaw.runtime.trajectories import ConversationTrajectoryProjection


class _ObservationHistory:
    def __init__(self, items: list[RuntimeObservation]) -> None:
        self.items = items

    async def scan_for_conversation(
        self,
        conversation_id: str,
    ) -> tuple[RuntimeObservation, ...]:
        return tuple(
            item
            for item in self.items
            if item.conversation_id == conversation_id
        )


def _observation(
    *,
    conversation_id: str,
    correlation_id,
    occurred_at,
    title: str,
) -> RuntimeObservation:
    observation_id = uuid4()
    return RuntimeObservation(
        observation_id=observation_id,
        category=ObservationCategory.ACTION,
        stage=ObservationStage.EXECUTION,
        status=ObservationStatus.SUCCEEDED,
        source=ObservationSource(
            source_type="qwenpaw.test.action",
            source_id=str(observation_id),
        ),
        conversation_id=conversation_id,
        correlation_id=correlation_id,
        title=title,
        occurred_at=occurred_at,
    )


@pytest.mark.asyncio
async def test_trajectory_replays_outcome_supersession_in_order(
    tmp_path,
) -> None:
    now = utc_now()
    correlation_id = uuid4()
    other_correlation_id = uuid4()
    history = _ObservationHistory(
        [
            _observation(
                conversation_id="chat-1",
                correlation_id=correlation_id,
                occurred_at=now,
                title="Intent admitted",
            ),
            _observation(
                conversation_id="chat-1",
                correlation_id=other_correlation_id,
                occurred_at=now,
                title="Unrelated intent",
            ),
        ],
    )
    outcomes = lite_conversation_outcome_store(tmp_path)
    partial = ConversationOutcome(
        agent_id="default",
        conversation_id="chat-1",
        correlation_id=correlation_id,
        status=ConversationOutcomeStatus.PARTIAL,
        producer_id="qwenpaw.system.tests",
        summary="One criterion remains open.",
        created_at=now + timedelta(seconds=1),
    )
    achieved = partial.model_copy(
        update={
            "outcome_id": uuid4(),
            "status": ConversationOutcomeStatus.ACHIEVED,
            "summary": "All criteria are verified.",
            "supersedes_outcome_id": partial.outcome_id,
            "created_at": now + timedelta(seconds=2),
        },
    )
    await outcomes.append(partial)
    await outcomes.append(achieved)
    projection = ConversationTrajectoryProjection(
        history,
        outcomes,
        LiteObservationIndex(tmp_path / "trajectory-index.sqlite3"),
    )

    page = await projection.page_for_correlation(
        agent_id="default",
        conversation_id="chat-1",
        correlation_id=correlation_id,
    )

    payload = page.model_dump(mode="json")
    assert payload["chat_id"] == "chat-1"
    assert "conversation_id" not in payload
    assert [item.category for item in page.items] == [
        ObservationCategory.ACTION,
        ObservationCategory.OUTCOME,
        ObservationCategory.OUTCOME,
    ]
    assert [item.status for item in page.items[-2:]] == [
        ObservationStatus.PARTIAL,
        ObservationStatus.SUCCEEDED,
    ]
    assert page.items[-1].facts["supersedes_outcome_id"] == str(
        partial.outcome_id,
    )


@pytest.mark.asyncio
async def test_trajectory_cursor_freezes_snapshot_and_owner(tmp_path) -> None:
    now = utc_now()
    correlation_id = uuid4()
    history = _ObservationHistory(
        [
            _observation(
                conversation_id="chat-1",
                correlation_id=correlation_id,
                occurred_at=now + timedelta(seconds=index),
                title=f"Step {index}",
            )
            for index in range(3)
        ],
    )
    projection = ConversationTrajectoryProjection(
        history,
        lite_conversation_outcome_store(tmp_path),
        LiteObservationIndex(tmp_path / "trajectory-index.sqlite3"),
    )
    first = await projection.page_for_correlation(
        agent_id="default",
        conversation_id="chat-1",
        correlation_id=correlation_id,
        limit=2,
    )
    assert [item.title for item in first.items] == ["Step 0", "Step 1"]
    assert first.next_cursor is not None

    history.items.append(
        _observation(
            conversation_id="chat-1",
            correlation_id=correlation_id,
            occurred_at=now + timedelta(seconds=3),
            title="Arrived after snapshot",
        ),
    )
    second = await projection.page_for_correlation(
        agent_id="default",
        conversation_id="chat-1",
        correlation_id=correlation_id,
        limit=2,
        cursor=first.next_cursor,
    )
    assert [item.title for item in second.items] == ["Step 2"]

    with pytest.raises(
        ObservationCursorError,
        match="owner mismatch",
    ):
        await projection.page_for_correlation(
            agent_id="default",
            conversation_id="chat-1",
            correlation_id=uuid4(),
            limit=2,
            cursor=first.next_cursor,
        )
