# -*- coding: utf-8 -*-
"""Durable scheduling contracts independent from scheduler frameworks."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any, Literal, Self
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import AwareDatetime, Field, model_validator

from .models import (
    ExecutionContract,
    JsonObject,
    KernelModel,
    NamespacedId,
    NonEmptyStr,
    RetryPolicy,
    utc_now,
)


class ScheduleDefinitionNotFoundError(LookupError):
    """Raised when a fire references an absent schedule definition."""


class ScheduleFireConflictError(RuntimeError):
    """Raised when one idempotency key is reused for another fire."""


class ScheduleLeaseConflictError(RuntimeError):
    """Raised when lease ownership, revision, or lifecycle does not match."""


class ScheduleLeaseNotFoundError(LookupError):
    """Raised when a lease mutation references an absent lease."""


class ScheduleTrigger(KernelModel):
    """One backend-neutral time trigger with exactly one value shape."""

    kind: Literal["cron", "once", "interval"]
    timezone: NonEmptyStr = "UTC"
    cron: NonEmptyStr | None = None
    run_at: AwareDatetime | None = None
    interval_seconds: float | None = Field(default=None, gt=0)
    start_at: AwareDatetime | None = None
    end_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_trigger_shape(self) -> Self:
        """Require exactly the payload selected by ``kind``."""
        values = {
            "cron": self.cron,
            "once": self.run_at,
            "interval": self.interval_seconds,
        }
        if values[self.kind] is None:
            raise ValueError(f"{self.kind} trigger value is required")
        conflicts = [
            name
            for name, value in values.items()
            if name != self.kind and value is not None
        ]
        if conflicts:
            raise ValueError(
                f"{self.kind} trigger cannot include: "
                f"{', '.join(conflicts)}",
            )
        if self.kind != "interval" and (
            self.start_at is not None or self.end_at is not None
        ):
            raise ValueError(
                f"{self.kind} trigger cannot include interval bounds",
            )
        if (
            self.start_at is not None
            and self.end_at is not None
            and self.end_at < self.start_at
        ):
            raise ValueError("interval end_at cannot precede start_at")
        return self


class ScheduleDefinition(KernelModel):
    """Immutable Task-producing schedule selected by stable capabilities."""

    schedule_id: NamespacedId
    agent_id: NonEmptyStr
    conversation_id: NonEmptyStr | None = None
    name: NonEmptyStr
    objective: NonEmptyStr
    trigger: ScheduleTrigger
    planner_id: NamespacedId = "qwenpaw.system.tasks.basic-planner"
    runner_id: NamespacedId
    strategy_id: NamespacedId | None = None
    execution_contract: ExecutionContract | None = None
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    enabled: bool = True
    max_concurrency: int = Field(default=1, ge=1)
    misfire_grace_seconds: int = Field(default=600, ge=0)
    metadata: JsonObject = Field(default_factory=dict)


class ScheduleFire(KernelModel):
    """One idempotent occurrence emitted for a scheduled instant."""

    fire_id: UUID | None = None
    agent_id: NonEmptyStr
    schedule_id: NamespacedId
    registry_generation: int = Field(default=1, ge=1)
    scheduled_for: AwareDatetime
    idempotency_key: NonEmptyStr
    attempt: int = Field(default=1, ge=1)
    fired_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="before")
    @classmethod
    def derive_stable_fire_id(cls, value: Any) -> Any:
        """Derive one identity before transport observation fields exist."""
        if not isinstance(value, Mapping) or value.get("fire_id") is not None:
            return value
        agent_id = value.get("agent_id")
        schedule_id = value.get("schedule_id")
        idempotency_key = value.get("idempotency_key")
        if not all(
            isinstance(item, str) and item.strip()
            for item in (agent_id, schedule_id, idempotency_key)
        ):
            return value
        values = dict(value)
        identity = f"qwenpaw:schedule:{agent_id}:{schedule_id}:"
        values["fire_id"] = uuid5(
            NAMESPACE_URL,
            f"{identity}{idempotency_key}",
        )
        return values

    @model_validator(mode="after")
    def validate_fire_id(self) -> Self:
        """Require a stable identity after field validation."""
        if self.fire_id is None:
            raise ValueError("schedule fire_id could not be derived")
        return self

    def same_occurrence(self, other: "ScheduleFire") -> bool:
        """Compare logical trigger facts while ignoring observation time."""
        return (
            self.fire_id == other.fire_id
            and self.agent_id == other.agent_id
            and self.schedule_id == other.schedule_id
            and self.registry_generation == other.registry_generation
            and self.scheduled_for == other.scheduled_for
            and self.idempotency_key == other.idempotency_key
            and self.attempt == other.attempt
        )


class ScheduleLeaseStatus(str, Enum):
    """Durable ownership state for one schedule fire."""

    CLAIMED = "claimed"
    COMPLETED = "completed"
    FAILED = "failed"


class ScheduleLease(KernelModel):
    """Revisioned ownership proof preventing duplicate Task creation."""

    lease_id: UUID = Field(default_factory=uuid4)
    fire: ScheduleFire
    owner_id: NonEmptyStr
    status: ScheduleLeaseStatus = ScheduleLeaseStatus.CLAIMED
    revision: int = Field(default=1, ge=1)
    acquired_at: AwareDatetime = Field(default_factory=utc_now)
    expires_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    task_id: UUID | None = None
    run_id: UUID | None = None
    error_code: str = ""
    retry_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        """Keep lease expiry, terminal facts, and retry state coherent."""
        if self.expires_at <= self.acquired_at:
            raise ValueError("schedule lease must expire after acquisition")
        terminal = self.status is not ScheduleLeaseStatus.CLAIMED
        if terminal != (self.finished_at is not None):
            raise ValueError("terminal schedule lease requires finished_at")
        if self.run_id is not None and self.task_id is None:
            raise ValueError("schedule run_id requires task_id")
        if self.status is ScheduleLeaseStatus.COMPLETED:
            if self.task_id is None:
                raise ValueError("completed schedule lease requires task_id")
            if self.error_code or self.retry_at is not None:
                raise ValueError(
                    "completed schedule lease cannot contain failure state",
                )
        if self.status is ScheduleLeaseStatus.FAILED:
            if not self.error_code:
                raise ValueError("failed schedule lease requires error_code")
            if self.task_id is not None or self.run_id is not None:
                raise ValueError(
                    "failed schedule lease cannot claim a created Task",
                )
        claimed_has_terminal_state = any(
            (
                self.finished_at is not None,
                self.task_id is not None,
                self.run_id is not None,
                bool(self.error_code),
                self.retry_at is not None,
            ),
        )
        if (
            self.status is ScheduleLeaseStatus.CLAIMED
            and claimed_has_terminal_state
        ):
            raise ValueError("claimed schedule lease cannot be terminal")
        return self


__all__ = [
    "ScheduleDefinition",
    "ScheduleDefinitionNotFoundError",
    "ScheduleFire",
    "ScheduleFireConflictError",
    "ScheduleLease",
    "ScheduleLeaseConflictError",
    "ScheduleLeaseNotFoundError",
    "ScheduleLeaseStatus",
    "ScheduleTrigger",
]
