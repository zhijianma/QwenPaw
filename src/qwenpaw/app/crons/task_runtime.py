# -*- coding: utf-8 -*-
"""Lite durable Task Runtime for losslessly migratable Cron jobs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from .conversation_binding import CronConversationBinder
from .executor import cron_session_id_for_job
from .models import (
    CronJobSpec,
    CronRuntimeDecision,
    CronRuntimeDecisionCode,
    CronRuntimePath,
)
from .schedule_adapter import (
    CronScheduleAdapter,
    CronScheduleMigrationError,
    cron_model_selection,
)
from .scheduled_task_runtime import LiteScheduledTaskRuntime


class LiteCronTaskRuntime:
    """Run supported Cron jobs through Scheduler, Task, and Delivery."""

    def __init__(self, workspace: Any) -> None:
        self._workspace = workspace
        self._scheduled = LiteScheduledTaskRuntime(workspace)

    @staticmethod
    def _legacy_decision(
        code: CronRuntimeDecisionCode,
        reason: str,
        *gates: str,
    ) -> CronRuntimeDecision:
        return CronRuntimeDecision(
            path=CronRuntimePath.LEGACY_EXECUTOR,
            reason_code=code,
            reason=reason,
            removal_gates=gates,
        )

    def decision(  # pylint: disable=too-many-return-statements
        self,
        job: CronJobSpec,
    ) -> CronRuntimeDecision:
        """Return a stable, actionable migration decision for one job."""
        if job.task_type != "agent":
            return self._legacy_decision(
                CronRuntimeDecisionCode.TEXT_DELIVERY_ONLY,
                "Text-only Cron is a Delivery schedule, not a Task run.",
                "Add a durable text Delivery schedule contract.",
            )
        if job.request is None:
            return self._legacy_decision(
                CronRuntimeDecisionCode.AGENT_REQUEST_MISSING,
                "The agent Cron declaration has no executable request.",
                "Persist a validated agent request before migration.",
            )
        if not job.dispatch.silent and job.dispatch.mode != "final":
            return self._legacy_decision(
                CronRuntimeDecisionCode.STREAM_DELIVERY_UNVERIFIED,
                "Stream Delivery has not passed the external Channel gate.",
                "Pass browser stream equivalence validation.",
                "Pass one live external media Channel validation.",
            )
        try:
            cron_model_selection(job)
        except CronScheduleMigrationError:
            return self._legacy_decision(
                CronRuntimeDecisionCode.MODEL_SELECTION_INVALID,
                "The legacy model selection cannot be represented safely.",
                "Configure a valid provider and model selection.",
            )
        return CronRuntimeDecision(
            path=CronRuntimePath.DURABLE_TASK,
            reason_code=CronRuntimeDecisionCode.MIGRATED,
            reason="The job is losslessly represented by Task Runtime.",
        )

    def supports(self, job: CronJobSpec) -> bool:
        """Return the legacy boolean projection of :meth:`decision`."""
        return self.decision(job).uses_durable_runtime

    async def execute(
        self,
        job: CronJobSpec,
        *,
        trigger: Literal["scheduled", "manual"],
        scheduled_for: datetime,
    ) -> dict[str, Any]:
        """Wait for terminal Task facts and durable Delivery receipts."""
        if not self.supports(job):
            raise ValueError("Cron job is not losslessly migratable")
        binding = await CronConversationBinder(self._workspace).bind(
            job,
            session_id=cron_session_id_for_job(job),
            required=True,
        )
        if binding is None:  # pragma: no cover - strict Binder invariant
            raise RuntimeError("Cron Conversation binding is missing")
        definition = CronScheduleAdapter().convert(
            job,
            agent_id=self._workspace.agent_id,
            binding=binding,
        )
        fire_key = (
            f"scheduled:{job.id}:{scheduled_for.isoformat()}"
            if trigger == "scheduled"
            else f"manual:{job.id}:{uuid4()}"
        )
        return await self._scheduled.execute(
            definition,
            scheduled_for=scheduled_for,
            idempotency_key=fire_key,
            timeout_seconds=job.runtime.timeout_seconds,
            owner_prefix="cron",
        )


__all__ = ["LiteCronTaskRuntime"]
