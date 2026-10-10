# -*- coding: utf-8 -*-
"""Compatibility translation from legacy Cron jobs to Kernel schedules."""

from __future__ import annotations

import hashlib
from datetime import timedelta

from ...delivery.channel import (
    SYSTEM_CHANNEL_DELIVERY_ID,
    encode_channel_address,
)
from ...kernel import (
    ApprovalLevel,
    AutonomyLevel,
    DeliveryDestination,
    DeliveryKind,
    DeliveryMode,
    DeliveryPolicy,
    ExecutionContract,
    ModelSelection,
    ScheduleDefinition,
    ScheduleTrigger,
    ScheduleWorkKind,
    TimeoutPolicy,
)
from ...tasks.system_contributions import (
    SYSTEM_BASIC_PLANNER_ID,
    SYSTEM_CONSOLE_RUNNER_ID,
    SYSTEM_DEFAULT_STRATEGY_ID,
)
from .conversation_binding import CronConversationBinding
from .models import CronJobSpec


class CronScheduleMigrationError(ValueError):
    """Raised when a legacy job cannot be represented without data loss."""


def cron_model_selection(job: CronJobSpec) -> ModelSelection | None:
    """Translate one legacy request override without accepting fallback."""
    request = job.request
    if request is None:
        return None
    if "model_slot_override" in request.model_fields_set:
        raw = getattr(request, "model_slot_override", None)
    else:
        context = getattr(request, "request_context", None)
        raw = (
            context.get("model_slot_override")
            if isinstance(context, dict)
            else None
        )
    if raw is None:
        return None
    if isinstance(raw, str):
        provider_id, separator, model = raw.partition(":")
        if not separator:
            raise CronScheduleMigrationError(
                "Cron model override must use provider:model",
            )
        raw = {
            "provider_id": provider_id.strip(),
            "model": model.strip(),
        }
    if not isinstance(raw, dict):
        raise CronScheduleMigrationError(
            "Cron model override must be a string or object",
        )
    try:
        return ModelSelection.model_validate(raw)
    except ValueError as error:
        raise CronScheduleMigrationError(
            "Cron model override has no stable provider and model",
        ) from error


def _objective(job: CronJobSpec) -> str:
    if job.request is None:
        raise CronScheduleMigrationError("agent Cron job has no request")
    raw_input = job.request.input
    if isinstance(raw_input, str) and raw_input.strip():
        return raw_input.strip()
    if isinstance(raw_input, list):
        for message in reversed(raw_input):
            content = (
                message.get("content")
                if isinstance(message, dict)
                else getattr(message, "content", None)
            )
            if not isinstance(content, list):
                continue
            texts = []
            for part in content:
                text = (
                    part.get("text")
                    if isinstance(part, dict)
                    else getattr(part, "text", None)
                )
                if isinstance(text, str) and text:
                    texts.append(text)
            if texts:
                return "\n".join(texts)
    raise CronScheduleMigrationError(
        "Cron request input has no representable text objective",
    )


def cron_schedule_id(agent_id: str, job_id: str) -> str:
    """Return the deterministic Kernel schedule identity for a Cron job."""
    digest = hashlib.sha256(
        f"{agent_id}:{job_id}".encode("utf-8"),
    ).hexdigest()[:20]
    return f"qwenpaw.legacy-cron.job-{digest}"


def _schedule_trigger(job: CronJobSpec) -> ScheduleTrigger:
    """Translate the legacy time shape without execution semantics."""
    if job.schedule.type == "cron":
        return ScheduleTrigger(
            kind="cron",
            timezone=job.schedule.timezone,
            cron=job.schedule.cron,
        )
    if job.schedule.repeat_every_days is None:
        return ScheduleTrigger(
            kind="once",
            timezone=job.schedule.timezone,
            run_at=job.schedule.run_at,
        )
    assert job.schedule.run_at is not None
    end_at = job.schedule.repeat_until
    if (
        job.schedule.repeat_end_type == "count"
        and job.schedule.repeat_count is not None
    ):
        end_at = job.schedule.run_at + timedelta(
            days=(
                job.schedule.repeat_every_days
                * (job.schedule.repeat_count - 1)
            ),
        )
    return ScheduleTrigger(
        kind="interval",
        timezone=job.schedule.timezone,
        interval_seconds=(job.schedule.repeat_every_days * 24 * 60 * 60),
        start_at=job.schedule.run_at,
        end_at=end_at,
    )


class TextDeliveryScheduleAdapter:
    """Translate fixed text into a durable Delivery schedule."""

    def convert(
        self,
        job: CronJobSpec,
        *,
        agent_id: str,
    ) -> ScheduleDefinition:
        """Return a non-Task schedule with an adapter-owned destination."""
        if job.id is None:
            raise CronScheduleMigrationError("Cron job has no stable ID")
        if job.task_type != "text" or not job.text:
            raise CronScheduleMigrationError(
                "Text Delivery schedule requires fixed text",
            )
        channel_meta = {
            key: value
            for key, value in dict(job.dispatch.meta or {}).items()
            if key not in {"session_id", "user_id"}
        }
        destination = DeliveryDestination(
            adapter_id=SYSTEM_CHANNEL_DELIVERY_ID,
            address=encode_channel_address(
                channel=job.dispatch.channel,
                user_id=job.dispatch.target.user_id,
                transport_context=job.dispatch.target.session_id,
            ),
            metadata={"channel_meta": channel_meta},
        )
        return ScheduleDefinition(
            schedule_id=cron_schedule_id(agent_id, job.id),
            agent_id=agent_id,
            name=job.name,
            objective="Deliver fixed text to a configured destination.",
            trigger=_schedule_trigger(job),
            work_kind=ScheduleWorkKind.DELIVERY,
            runner_id="qwenpaw.system.crons.text-delivery",
            enabled=job.enabled,
            max_concurrency=job.runtime.max_concurrency,
            misfire_grace_seconds=job.runtime.misfire_grace_seconds,
            metadata={
                "legacy_cron_job_id": job.id,
                "delivery_destination": destination.model_dump(mode="json"),
                "delivery_text": job.text.strip(),
                "source": "legacy_cron_text_delivery",
            },
        )


class CronScheduleAdapter:
    """Produce one loss-aware Kernel schedule from a verified binding."""

    def convert(
        self,
        job: CronJobSpec,
        *,
        agent_id: str,
        binding: CronConversationBinding,
    ) -> ScheduleDefinition:
        """Translate stable execution and delivery facts."""
        if job.id is None:
            raise CronScheduleMigrationError("Cron job has no stable ID")
        if job.task_type != "agent":
            raise CronScheduleMigrationError(
                "text Cron jobs belong to Delivery, not Task Runtime",
            )
        objective = _objective(job)
        mode = (
            DeliveryMode.SILENT
            if job.dispatch.silent
            else DeliveryMode(job.dispatch.mode)
        )
        if job.runtime.tool_safety and mode is DeliveryMode.SILENT:
            mode = DeliveryMode.FINAL
        channel_meta = {
            key: value
            for key, value in dict(job.dispatch.meta or {}).items()
            if key not in {"session_id", "user_id"}
        }
        channel_meta["suppress_console_push"] = True
        delivery_policy = DeliveryPolicy(
            destination=DeliveryDestination(
                adapter_id=SYSTEM_CHANNEL_DELIVERY_ID,
                address=encode_channel_address(
                    channel=binding.channel,
                    user_id=binding.user_id,
                    transport_context=job.dispatch.target.session_id,
                ),
                chat_id=binding.conversation_id,
                metadata={"channel_meta": channel_meta},
            ),
            mode=mode,
            kinds=(
                (
                    DeliveryKind.REPLY,
                    DeliveryKind.ACTIVITY,
                    DeliveryKind.EXCEPTION,
                    DeliveryKind.ARTIFACT_READY,
                )
                if mode is DeliveryMode.STREAM
                else (
                    (
                        DeliveryKind.APPROVAL,
                        DeliveryKind.EXCEPTION,
                    )
                    if job.dispatch.silent and job.runtime.tool_safety
                    else (
                        DeliveryKind.RESULT,
                        DeliveryKind.APPROVAL,
                        DeliveryKind.EXCEPTION,
                    )
                    if job.runtime.tool_safety
                    else (DeliveryKind.RESULT,)
                )
            ),
        )
        approval_level = (
            ApprovalLevel.AUTO
            if job.runtime.tool_safety
            else ApprovalLevel.OFF
        )
        model_selection = cron_model_selection(job)
        return ScheduleDefinition(
            schedule_id=cron_schedule_id(agent_id, job.id),
            agent_id=agent_id,
            conversation_id=binding.conversation_id,
            name=job.name,
            objective=objective,
            trigger=_schedule_trigger(job),
            planner_id=SYSTEM_BASIC_PLANNER_ID,
            runner_id=SYSTEM_CONSOLE_RUNNER_ID,
            strategy_id=SYSTEM_DEFAULT_STRATEGY_ID,
            execution_contract=ExecutionContract(
                goal=objective,
                autonomy_level=AutonomyLevel.CONTROLLED_BACKGROUND,
                timeout_policy=TimeoutPolicy(
                    attempt_seconds=job.runtime.timeout_seconds,
                    approval_seconds=(
                        job.runtime.effective_approval_timeout_seconds()
                    ),
                ),
            ),
            enabled=job.enabled,
            max_concurrency=job.runtime.max_concurrency,
            misfire_grace_seconds=job.runtime.misfire_grace_seconds,
            metadata={
                "legacy_cron_job_id": job.id,
                "approval_level": approval_level.value,
                "model_selection": (
                    model_selection.model_dump(mode="json")
                    if model_selection is not None
                    else None
                ),
                "delivery_policy": delivery_policy.model_dump(mode="json"),
            },
        )


__all__ = [
    "CronScheduleAdapter",
    "CronScheduleMigrationError",
    "cron_schedule_id",
    "cron_model_selection",
]
