# -*- coding: utf-8 -*-
"""Contract tests for immutable operational source facts."""

from datetime import datetime, timezone

from qwenpaw.kernel import (
    OperationalEvent,
    OperationalSeverity,
    OperationalStatus,
)


def test_operational_event_derives_stable_identity() -> None:
    """Producer ownership and key determine identity, not timestamp."""
    first = OperationalEvent(
        agent_id="default",
        producer_id="qwenpaw.system.skills",
        event_type="skill.auto_sync",
        idempotency_key="sync:abc",
        source_type="skill_autoupdate",
        status=OperationalStatus.SUCCESS,
        severity=OperationalSeverity.INFO,
        title="Skill sync completed",
    )
    replay = first.model_copy(
        update={
            "occurred_at": datetime(2030, 1, 1, tzinfo=timezone.utc),
        },
    )

    assert first.event_id == replay.event_id
    assert first.same_fact(replay)


def test_operational_event_identity_is_agent_scoped() -> None:
    """The same producer key in another Agent is a different fact."""
    values = {
        "producer_id": "qwenpaw.system.skills",
        "event_type": "skill.auto_sync",
        "idempotency_key": "sync:abc",
        "source_type": "skill_autoupdate",
        "status": "success",
        "title": "Skill sync completed",
    }

    first = OperationalEvent(agent_id="agent-a", **values)
    second = OperationalEvent(agent_id="agent-b", **values)

    assert first.event_id != second.event_id
