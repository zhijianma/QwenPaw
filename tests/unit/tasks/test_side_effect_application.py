# -*- coding: utf-8 -*-
"""Tests for the Task side-effect application boundary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from qwenpaw.kernel.models import (
    ActorRef,
    ActorType,
    SideEffectRecord,
    ToolEffect,
)
from qwenpaw.tasks.side_effect_application import (
    AuthorizeSideEffectRetryCommand,
    TaskSideEffectApplicationService,
)


def _record(task_id, run_id) -> SideEffectRecord:
    return SideEffectRecord(
        task_id=task_id,
        run_id=run_id,
        action="Write file",
        target="README.md",
        effect=ToolEffect.LOCAL_WRITE,
        idempotency_key="write-readme",
        request_hash="0" * 64,
    )


@pytest.mark.asyncio
async def test_lists_task_owned_side_effects_as_an_immutable_page() -> None:
    task_id = uuid4()
    run_id = uuid4()
    record = _record(task_id, run_id)
    service = SimpleNamespace(
        list_side_effects=AsyncMock(return_value=[record]),
    )
    application = TaskSideEffectApplicationService(service)

    result = await application.list(task_id, run_id=run_id)

    assert result == (record,)
    service.list_side_effects.assert_awaited_once_with(
        task_id,
        run_id=run_id,
    )


@pytest.mark.asyncio
async def test_authorizes_retry_through_one_transport_neutral_command(
) -> None:
    task_id = uuid4()
    run_id = uuid4()
    record = _record(task_id, run_id)
    actor = ActorRef(type=ActorType.USER, id="local-user")
    service = SimpleNamespace(
        authorize_side_effect_retry=AsyncMock(return_value=record),
    )
    application = TaskSideEffectApplicationService(service)

    result = await application.authorize_retry(
        AuthorizeSideEffectRetryCommand(
            task_id=task_id,
            record_id=record.record_id,
            actor=actor,
            reason="Confirmed that the write did not happen",
        ),
    )

    assert result == record
    service.authorize_side_effect_retry.assert_awaited_once_with(
        task_id,
        record.record_id,
        actor=actor,
        reason="Confirmed that the write did not happen",
    )
