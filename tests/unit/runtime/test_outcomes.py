# -*- coding: utf-8 -*-
"""Tests for explicit Conversation Outcome persistence."""

import stat
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from qwenpaw.kernel import ConversationOutcome, ConversationOutcomeStatus
from qwenpaw.kernel.models import utc_now
from qwenpaw.runtime.outcomes import (
    ConversationOutcomeConflictError,
    lite_conversation_outcome_store,
)


def test_outcome_rejects_ambiguous_causal_references() -> None:
    evidence_id = uuid4()
    with pytest.raises(ValidationError, match="task_id and run_id"):
        ConversationOutcome(
            agent_id="default",
            conversation_id="chat-1",
            correlation_id=uuid4(),
            status=ConversationOutcomeStatus.ACHIEVED,
            producer_id="qwenpaw.system.verifier",
            summary="Invalid Task ownership.",
            task_id=uuid4(),
        )
    with pytest.raises(ValidationError, match="evidence IDs must be unique"):
        ConversationOutcome(
            agent_id="default",
            conversation_id="chat-1",
            correlation_id=uuid4(),
            status=ConversationOutcomeStatus.ACHIEVED,
            producer_id="qwenpaw.system.verifier",
            summary="Duplicate evidence.",
            evidence_ids=(evidence_id, evidence_id),
        )


@pytest.mark.asyncio
async def test_outcome_requires_explicit_latest_supersession(tmp_path) -> None:
    store = lite_conversation_outcome_store(tmp_path)
    correlation_id = uuid4()
    first = ConversationOutcome(
        agent_id="default",
        conversation_id="chat-1",
        correlation_id=correlation_id,
        status=ConversationOutcomeStatus.PARTIAL,
        producer_id="qwenpaw.system.verifier",
        summary="Some acceptance criteria remain open.",
    )

    await store.append(first)
    await store.append(first)
    with pytest.raises(
        ConversationOutcomeConflictError,
        match="explicitly supersede",
    ):
        await store.append(
            first.model_copy(
                update={
                    "outcome_id": uuid4(),
                    "status": ConversationOutcomeStatus.ACHIEVED,
                    "summary": "All criteria now pass.",
                    "created_at": utc_now() + timedelta(seconds=1),
                },
            ),
        )

    achieved = first.model_copy(
        update={
            "outcome_id": uuid4(),
            "status": ConversationOutcomeStatus.ACHIEVED,
            "summary": "All criteria now pass.",
            "supersedes_outcome_id": first.outcome_id,
            "created_at": utc_now() + timedelta(seconds=1),
        },
    )
    await store.append(achieved)

    latest = await store.latest_for_correlations(
        agent_id="default",
        conversation_id="chat-1",
        correlation_ids=(correlation_id, uuid4()),
    )
    assert latest == (achieved,)
    history = await store.list_for_correlation(
        agent_id="default",
        conversation_id="chat-1",
        correlation_id=correlation_id,
    )
    assert history == (first, achieved)
    assert stat.S_IMODE(store.database_path.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_outcome_identity_conflict_fails_closed(tmp_path) -> None:
    store = lite_conversation_outcome_store(tmp_path)
    outcome = ConversationOutcome(
        agent_id="default",
        conversation_id="chat-1",
        correlation_id=uuid4(),
        status=ConversationOutcomeStatus.NOT_ACHIEVED,
        producer_id="qwenpaw.system.runtime",
        summary="The requested result was not achieved.",
    )
    await store.append(outcome)

    with pytest.raises(
        ConversationOutcomeConflictError,
        match="conflicting content",
    ):
        await store.append(
            outcome.model_copy(update={"summary": "Conflicting summary."}),
        )
