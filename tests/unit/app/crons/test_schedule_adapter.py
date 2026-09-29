# -*- coding: utf-8 -*-
"""Tests for loss-aware legacy Cron schedule translation."""

from datetime import datetime, timedelta, timezone

import pytest

from qwenpaw.app.crons.conversation_binding import CronConversationBinding
from qwenpaw.app.crons.schedule_adapter import (
    CronScheduleAdapter,
    CronScheduleMigrationError,
)
from qwenpaw.app.crons.models import CronJobRequest, ScheduleSpec
from tests.unit.app.conftest import make_cron_job_spec


def _binding() -> CronConversationBinding:
    return CronConversationBinding(
        conversation_id="chat-daily",
        session_id="cron:daily",
        user_id="u1",
        channel="console",
    )


def test_agent_job_maps_execution_delivery_and_identity() -> None:
    job = make_cron_job_spec(job_id="daily")
    job.dispatch.mode = "final"
    job.dispatch.meta = {
        "thread_id": "thread-1",
        "session_id": "must-not-leak",
        "user_id": "must-not-leak",
    }

    definition = CronScheduleAdapter().convert(
        job,
        agent_id="default",
        binding=_binding(),
    )

    assert definition.conversation_id == "chat-daily"
    assert definition.objective == "ping"
    assert definition.trigger.cron == "0 9 * * mon"
    assert definition.execution_contract is not None
    assert definition.execution_contract.timeout_policy.attempt_seconds == 120
    assert definition.metadata["approval_level"] == "off"
    assert definition.metadata["delivery_policy"]["mode"] == "final"
    assert definition.metadata["delivery_policy"]["kinds"] == ["result"]
    destination = definition.metadata["delivery_policy"]["destination"]
    assert destination["metadata"] == {
        "channel_meta": {
            "thread_id": "thread-1",
            "suppress_console_push": True,
        },
    }
    assert "session_id" not in destination["metadata"]["channel_meta"]
    assert "user_id" not in destination["metadata"]["channel_meta"]


def test_stream_job_maps_only_public_committed_activity() -> None:
    job = make_cron_job_spec(job_id="stream")
    job.dispatch.mode = "stream"

    definition = CronScheduleAdapter().convert(
        job,
        agent_id="default",
        binding=_binding(),
    )

    assert definition.metadata["delivery_policy"]["mode"] == "stream"
    assert definition.metadata["delivery_policy"]["kinds"] == [
        "reply",
        "activity",
        "exception",
        "artifact_ready",
    ]


@pytest.mark.parametrize("silent", [False, True])
def test_tool_safety_maps_bounded_approval_and_notifications(
    silent: bool,
) -> None:
    job = make_cron_job_spec(job_id="guarded")
    job.dispatch.mode = "final"
    job.dispatch.silent = silent
    job.runtime.tool_safety = True
    job.runtime.approval_timeout_seconds = 15

    definition = CronScheduleAdapter().convert(
        job,
        agent_id="default",
        binding=_binding(),
    )

    assert definition.metadata["approval_level"] == "auto"
    assert definition.execution_contract is not None
    assert definition.execution_contract.timeout_policy.approval_seconds == 15
    policy = definition.metadata["delivery_policy"]
    assert policy["mode"] == "final"
    assert policy["kinds"] == (
        ["approval", "exception"]
        if silent
        else ["result", "approval", "exception"]
    )


def test_once_job_maps_to_aware_kernel_trigger() -> None:
    job = make_cron_job_spec(job_id="once")
    run_at = datetime(2030, 1, 1, tzinfo=timezone.utc)
    job.schedule = ScheduleSpec(type="once", run_at=run_at)

    definition = CronScheduleAdapter().convert(
        job,
        agent_id="default",
        binding=_binding(),
    )

    assert definition.trigger.kind == "once"
    assert definition.trigger.run_at == run_at


@pytest.mark.parametrize(
    ("cron_request", "expected"),
    [
        (
            CronJobRequest(
                input="ping",
                model_slot_override="dashscope:qwen-max",
            ),
            {"provider_id": "dashscope", "model": "qwen-max"},
        ),
        (
            CronJobRequest(
                input="ping",
                request_context={
                    "model_slot_override": {
                        "provider_id": "openai",
                        "model": "gpt-test",
                    },
                },
            ),
            {"provider_id": "openai", "model": "gpt-test"},
        ),
    ],
)
def test_agent_job_maps_stable_model_selection(
    cron_request,
    expected,
) -> None:
    job = make_cron_job_spec(job_id="model")
    job.dispatch.mode = "final"
    job.request = cron_request.model_copy(
        update={"user_id": "u1", "session_id": "console:u1"},
    )

    definition = CronScheduleAdapter().convert(
        job,
        agent_id="default",
        binding=_binding(),
    )

    assert definition.metadata["model_selection"] == {
        "schema": "qwenpaw.kernel-model.v1",
        **expected,
    }


def test_agent_job_rejects_invalid_model_selection() -> None:
    job = make_cron_job_spec(job_id="invalid-model")
    job.dispatch.mode = "final"
    job.request = CronJobRequest(
        input="ping",
        model_slot_override="missing-separator",
    )

    with pytest.raises(CronScheduleMigrationError, match="provider:model"):
        CronScheduleAdapter().convert(
            job,
            agent_id="default",
            binding=_binding(),
        )


def test_text_job_is_not_misrepresented_as_agent_task() -> None:
    job = make_cron_job_spec(job_id="text", task_type="text")

    with pytest.raises(CronScheduleMigrationError, match="Delivery"):
        CronScheduleAdapter().convert(
            job,
            agent_id="default",
            binding=_binding(),
        )


@pytest.mark.parametrize(
    ("end_type", "end_value", "count", "expected_days"),
    [
        ("never", None, None, None),
        ("until", 8, None, 8),
        ("count", None, 4, 6),
    ],
)
def test_repeating_once_job_maps_to_anchored_interval(
    end_type: str,
    end_value: int | None,
    count: int | None,
    expected_days: int | None,
) -> None:
    job = make_cron_job_spec(job_id="repeat")
    start = datetime(2030, 1, 1, tzinfo=timezone.utc)
    job.schedule = ScheduleSpec(
        type="once",
        run_at=start,
        repeat_every_days=2,
        repeat_end_type=end_type,
        repeat_until=(
            start + timedelta(days=end_value)
            if end_value is not None
            else None
        ),
        repeat_count=count,
    )

    definition = CronScheduleAdapter().convert(
        job,
        agent_id="default",
        binding=_binding(),
    )

    assert definition.trigger.kind == "interval"
    assert definition.trigger.interval_seconds == 2 * 24 * 60 * 60
    assert definition.trigger.start_at == start
    assert definition.trigger.end_at == (
        start + timedelta(days=expected_days)
        if expected_days is not None
        else None
    )
