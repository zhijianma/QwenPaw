# -*- coding: utf-8 -*-
"""Application service for durable local task lifecycle operations."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import AwareDatetime, JsonValue

from ..kernel.events import (
    ExecutionCommit,
    ExecutionEvent,
    TaskProjectionSnapshot,
)
from ..kernel.models import (
    ActorRef,
    ActorType,
    ApprovalContinuation,
    ApprovalDecision,
    ApprovalDecisionValue,
    ApprovalDisplay,
    ApprovalRequest,
    ApprovalSource,
    ApprovalStatus,
    ExecutionCheckpoint,
    ExecutionContract,
    IdempotencyRecord,
    JsonObject,
    Plan,
    PlanStep,
    RiskLevel,
    Run,
    RunStatus,
    RunnerSignal,
    SideEffectDisposition,
    SideEffectRecord,
    SideEffectReservation,
    SideEffectStatus,
    Task,
    TaskSource,
    TaskStatus,
    ToolEffect,
)
from ..kernel.ports import CheckpointStore, ExecutionLedger, TaskStore
from ..kernel.state_machine import (
    validate_run_transition,
    validate_task_transition,
)
from .redaction import redact_payload
from .ledger import (
    EventSequenceError,
    SideEffectConflictError,
    TaskVersionConflictError,
)
from .results import (
    load_task_result_projection,
    validate_result_projection,
)

_APPROVAL_COMMIT_RETRIES = 4
_SIDE_EFFECT_COMMIT_RETRIES = 4
_MAX_CHECKPOINT_PAYLOAD_BYTES = 32 * 1024


def _validate_checkpoint_payload(
    runner_cursor: JsonValue,
    workspace_checkpoint_ref: str | None,
) -> None:
    """Reject empty references and oversized durable runner state."""
    if (
        workspace_checkpoint_ref is not None
        and not workspace_checkpoint_ref.strip()
    ):
        raise ValueError("workspace_checkpoint_ref cannot be empty")
    encoded = json.dumps(
        {
            "runner_cursor": runner_cursor,
            "workspace_checkpoint_ref": workspace_checkpoint_ref,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > _MAX_CHECKPOINT_PAYLOAD_BYTES:
        raise ValueError("checkpoint payload exceeds 32 KiB")


class DurableTaskStore(
    TaskStore,
    ExecutionLedger,
    CheckpointStore,
    Protocol,
):
    """Combined atomic store required by the Lite application service."""


class TaskNotFoundError(KeyError):
    """Raised when a task ID has no durable projection."""


class RunNotFoundError(KeyError):
    """Raised when a task has no active durable run."""


class ApprovalNotFoundError(KeyError):
    """Raised when an approval ID does not exist for a task."""


class ApprovalAlreadyResolvedError(ValueError):
    """Raised when an approval decision is submitted more than once."""


class CheckpointNotResumableError(ValueError):
    """Raised when no safe checkpoint can resume a task."""


class RunNotActiveError(ValueError):
    """Raised when a runner emits against an inactive task attempt."""


class IdempotencyRequestConflictError(ValueError):
    """Raised when one idempotency key is reused for another request."""


class SideEffectRequestConflictError(ValueError):
    """Raised when a side-effect key is reused for another operation."""


class SideEffectRecoveryRequiredError(ValueError):
    """Raised when uncertain operations block automatic task recovery."""


class SideEffectNotRecoverableError(ValueError):
    """Raised when retry authorization targets a settled operation."""


@dataclass(frozen=True, slots=True)
class TaskResumeOutcome:
    """Describe whether a resume command created a new execution attempt."""

    task: Task
    run: Run
    replayed: bool


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TaskService:  # pylint: disable=too-many-public-methods
    """Coordinates domain transitions and atomic durable commits."""

    def __init__(
        self,
        *,
        store: DurableTaskStore,
        registry_generation: int,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if registry_generation < 1:
            raise ValueError("registry_generation must be positive")
        self._store = store
        self._registry_generation = registry_generation
        self._clock = clock
        self._system_actor = ActorRef(
            type=ActorType.SYSTEM,
            id="task-service",
        )

    def set_registry_generation(self, generation: int) -> None:
        """Select the generation that future runs will pin."""
        if generation < 1:
            raise ValueError("registry_generation must be positive")
        self._registry_generation = generation

    async def _required_task(self, task_id: UUID) -> Task:
        task = await self._store.get_task(task_id)
        if task is None:
            raise TaskNotFoundError(str(task_id))
        return task

    async def _active_run(self, task: Task) -> Run:
        if task.active_run_id is None:
            raise RunNotFoundError(str(task.task_id))
        run = await self._store.get_run(task.active_run_id)
        if run is None:
            raise RunNotFoundError(str(task.active_run_id))
        return run

    async def _next_sequence(self, task_id: UUID) -> int:
        return await self._store.latest_sequence(task_id) + 1

    @staticmethod
    def _request_hash(values: JsonObject) -> str:
        """Return a stable digest for one idempotent mutation request."""
        return hashlib.sha256(
            json.dumps(
                values,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8"),
        ).hexdigest()

    async def _idempotent_response(
        self,
        *,
        operation: str,
        key: str | None,
        request_hash: str,
    ) -> JsonObject | None:
        """Replay a matching mutation response or reject key reuse."""
        if not key:
            return None
        existing = await self._store.get_idempotency(operation, key)
        if existing is None:
            return None
        if existing.request_hash != request_hash:
            raise IdempotencyRequestConflictError(
                "idempotency key was used for another request",
            )
        return existing.response

    @staticmethod
    def _idempotency_record(
        *,
        operation: str,
        key: str | None,
        request_hash: str,
        response: JsonObject,
    ) -> IdempotencyRecord | None:
        """Build the record committed atomically with a mutation."""
        if not key:
            return None
        return IdempotencyRecord(
            operation=operation,
            key=key,
            request_hash=request_hash,
            response=response,
        )

    def _event(
        self,
        *,
        task_id: UUID,
        sequence: int,
        event_type: str,
        run_id: UUID | None = None,
        actor: ActorRef | None = None,
        step_id: UUID | None = None,
        source: str | None = None,
        cause_event_id: UUID | None = None,
        invocation_id: UUID | None = None,
        correlation_id: UUID | None = None,
        payload: JsonObject | None = None,
        signal: RunnerSignal | None = None,
        registry_generation: int | None = None,
    ) -> ExecutionEvent:
        return ExecutionEvent(
            task_id=task_id,
            run_id=run_id,
            sequence=sequence,
            event_type=event_type,
            occurred_at=self._clock(),
            registry_generation=(
                registry_generation or self._registry_generation
            ),
            actor=actor or self._system_actor,
            step_id=(signal.step_id if signal is not None else step_id),
            source=(signal.source or source if signal is not None else source),
            cause_event_id=(
                signal.cause_event_id if signal is not None else cause_event_id
            ),
            invocation_id=(
                invocation_id or signal.invocation_id
                if signal is not None
                else invocation_id
            ),
            correlation_id=(
                signal.correlation_id or correlation_id
                if signal is not None
                else correlation_id
            ),
            payload=redact_payload(
                signal.payload if signal is not None else payload or {},
            ),
            artifact_refs=(signal.artifact_refs if signal is not None else ()),
            evidence_refs=(signal.evidence_refs if signal is not None else ()),
        )

    async def record_runner_signal(
        self,
        task_id: UUID,
        run_id: UUID,
        signal: RunnerSignal,
    ) -> ExecutionEvent:
        """Persist one validated runner signal as a canonical event."""
        task = await self._required_task(task_id)
        run = await self._active_run(task)
        if run.run_id != run_id:
            raise RunNotFoundError(str(run_id))
        if task.status is not TaskStatus.RUNNING:
            raise RunNotActiveError(
                f"runner signal requires a running task, got "
                f"{task.status.value}",
            )
        event = self._event(
            task_id=task_id,
            run_id=run_id,
            sequence=await self._next_sequence(task_id),
            event_type=signal.event_type,
            actor=ActorRef(type=ActorType.RUNNER, id=run.runner_id),
            source=run.runner_id,
            invocation_id=run.invocation_id,
            correlation_id=run.correlation_id,
            signal=signal,
            registry_generation=run.registry_generation,
        )
        await self._store.commit(ExecutionCommit(events=(event,)))
        return event

    async def record_exit_condition_triggered(
        self,
        task_id: UUID,
        run_id: UUID,
        *,
        condition_ids: Sequence[str],
        cause_event_id: UUID,
    ) -> ExecutionEvent:
        """Audit a host decision to stop at a satisfied optional condition."""
        normalized_ids = tuple(dict.fromkeys(condition_ids))
        if not normalized_ids:
            raise ValueError("condition_ids cannot be empty")
        task = await self._required_task(task_id)
        run = await self._active_run(task)
        if run.run_id != run_id:
            raise RunNotFoundError(str(run_id))
        if task.status is not TaskStatus.RUNNING:
            raise RunNotActiveError(
                f"exit condition trigger requires a running task, got "
                f"{task.status.value}",
            )
        event = self._event(
            task_id=task_id,
            run_id=run_id,
            sequence=await self._next_sequence(task_id),
            event_type="exit_condition.triggered",
            actor=self._system_actor,
            source="qwenpaw.system.tasks.execution-coordinator",
            cause_event_id=cause_event_id,
            invocation_id=run.invocation_id,
            correlation_id=run.correlation_id,
            payload={"condition_ids": list(normalized_ids)},
            registry_generation=run.registry_generation,
        )
        await self._store.commit(ExecutionCommit(events=(event,)))
        return event

    async def begin_side_effect(
        self,
        task_id: UUID,
        run_id: UUID,
        *,
        action: str,
        target: str,
        effect: ToolEffect,
        idempotency_key: str,
        request_hash: str,
        invocation_id: UUID | None = None,
        correlation_id: UUID | None = None,
        approval_id: UUID | None = None,
        policy_decision: str = "",
    ) -> SideEffectReservation:
        """Reserve one external operation before execution."""
        if effect is ToolEffect.NONE:
            raise ValueError("read-only tools do not create side effects")
        existing = await self._store.get_side_effect_by_key(
            task_id,
            idempotency_key,
        )
        if existing is not None:
            return self._side_effect_reservation(existing, request_hash)
        for attempt in range(_SIDE_EFFECT_COMMIT_RETRIES):
            current = await self._required_task(task_id)
            run = await self._active_run(current)
            if run.run_id != run_id:
                raise RunNotFoundError(str(run_id))
            effective_invocation_id = invocation_id or run.invocation_id
            record = SideEffectRecord(
                task_id=task_id,
                run_id=run_id,
                invocation_id=effective_invocation_id,
                correlation_id=(
                    correlation_id
                    or effective_invocation_id
                    or run.correlation_id
                ),
                approval_id=approval_id,
                action=action,
                target=target,
                effect=effect,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                policy_decision=policy_decision,
                started_at=self._clock(),
            )
            event = self._event(
                task_id=task_id,
                run_id=run_id,
                sequence=await self._next_sequence(task_id),
                event_type="side_effect.prepared",
                actor=ActorRef(type=ActorType.RUNNER, id=run.runner_id),
                source=run.runner_id,
                invocation_id=effective_invocation_id,
                correlation_id=(
                    correlation_id
                    or effective_invocation_id
                    or run.correlation_id
                ),
                payload={
                    "record_id": str(record.record_id),
                    "action": action,
                    "effect": effect.value,
                    "idempotency_key": idempotency_key,
                    "invocation_id": (
                        str(effective_invocation_id)
                        if effective_invocation_id
                        else None
                    ),
                    "correlation_id": (
                        str(record.correlation_id)
                        if record.correlation_id is not None
                        else None
                    ),
                    "approval_id": (str(approval_id) if approval_id else None),
                    "policy_decision": policy_decision,
                },
                registry_generation=run.registry_generation,
            )
            try:
                await self._store.commit(
                    ExecutionCommit(
                        events=(event,),
                        side_effect_record=record,
                    ),
                )
                return SideEffectReservation(
                    record=record,
                    disposition=SideEffectDisposition.EXECUTE,
                )
            except (
                EventSequenceError,
                SideEffectConflictError,
            ):
                existing = await self._store.get_side_effect_by_key(
                    task_id,
                    idempotency_key,
                )
                if existing is not None:
                    return self._side_effect_reservation(
                        existing,
                        request_hash,
                    )
                if attempt == _SIDE_EFFECT_COMMIT_RETRIES - 1:
                    raise
                await asyncio.sleep(0)
        raise AssertionError("side-effect retry loop exhausted")

    @staticmethod
    def _side_effect_reservation(
        existing: SideEffectRecord,
        request_hash: str,
    ) -> SideEffectReservation:
        if existing.request_hash != request_hash:
            raise SideEffectRequestConflictError(
                "side-effect idempotency key was used for another request",
            )
        disposition = (
            SideEffectDisposition.REPLAY
            if existing.status is SideEffectStatus.SUCCEEDED
            else SideEffectDisposition.BLOCKED
        )
        return SideEffectReservation(
            record=existing,
            disposition=disposition,
        )

    async def finish_side_effect(
        self,
        record_id: UUID,
        *,
        status: SideEffectStatus,
        result_digest: str | None = None,
        external_ref: str = "",
        error_code: str = "",
    ) -> SideEffectRecord:
        """Commit a terminal side-effect outcome and linked event."""
        if status is SideEffectStatus.PREPARED:
            raise ValueError("finish_side_effect requires terminal status")
        for attempt in range(_SIDE_EFFECT_COMMIT_RETRIES):
            current = await self._store.get_side_effect(record_id)
            if current is None:
                raise KeyError(str(record_id))
            if current.status is not SideEffectStatus.PREPARED:
                if current.status is status:
                    return current
                raise SideEffectRequestConflictError(
                    "side effect already has a different terminal status",
                )
            updated = SideEffectRecord.model_validate(
                {
                    **current.model_dump(),
                    "status": status,
                    "result_digest": result_digest,
                    "external_ref": external_ref,
                    "error_code": error_code,
                    "finished_at": self._clock(),
                },
            )
            run = await self._store.get_run(current.run_id)
            if run is None:
                raise RunNotFoundError(str(current.run_id))
            event = self._event(
                task_id=current.task_id,
                run_id=current.run_id,
                sequence=await self._next_sequence(current.task_id),
                event_type=f"side_effect.{status.value}",
                actor=ActorRef(type=ActorType.RUNNER, id=run.runner_id),
                source=run.runner_id,
                invocation_id=current.invocation_id or run.invocation_id,
                correlation_id=current.correlation_id,
                payload={
                    "record_id": str(record_id),
                    "action": current.action,
                    "effect": current.effect.value,
                    "idempotency_key": current.idempotency_key,
                    "result_digest": result_digest,
                    "error_code": error_code,
                },
                registry_generation=run.registry_generation,
            )
            try:
                await self._store.commit(
                    ExecutionCommit(
                        events=(event,),
                        side_effect_record=updated,
                    ),
                )
                return updated
            except (
                EventSequenceError,
                SideEffectConflictError,
            ) as exc:
                durable = await self._store.get_side_effect(record_id)
                if durable is not None and durable.status is not (
                    SideEffectStatus.PREPARED
                ):
                    if durable.status is status:
                        return durable
                    raise SideEffectRequestConflictError(
                        "side effect resolved with a different status",
                    ) from exc
                if attempt == _SIDE_EFFECT_COMMIT_RETRIES - 1:
                    raise
                await asyncio.sleep(0)
        raise AssertionError("side-effect finish retry loop exhausted")

    async def authorize_side_effect_retry(
        self,
        task_id: UUID,
        record_id: UUID,
        *,
        actor: ActorRef,
        reason: str,
    ) -> SideEffectRecord:
        """Record a human assertion that an uncertain action may rerun."""
        if not reason.strip():
            raise ValueError("side-effect retry requires a reason")
        for attempt in range(_SIDE_EFFECT_COMMIT_RETRIES):
            current = await self._store.get_side_effect(record_id)
            if current is None or current.task_id != task_id:
                raise KeyError(str(record_id))
            if (
                current.status is SideEffectStatus.FAILED
                and current.recovered_at is not None
            ):
                return current
            if current.status is not SideEffectStatus.UNCERTAIN:
                raise SideEffectNotRecoverableError(str(record_id))
            now = self._clock()
            updated = SideEffectRecord.model_validate(
                {
                    **current.model_dump(),
                    "status": SideEffectStatus.FAILED,
                    "error_code": "RetryAuthorized",
                    "recovery_actor": actor.model_dump(mode="json"),
                    "recovery_reason": reason,
                    "recovered_at": now,
                },
            )
            run = await self._store.get_run(current.run_id)
            if run is None:
                raise RunNotFoundError(str(current.run_id))
            event = self._event(
                task_id=task_id,
                run_id=current.run_id,
                sequence=await self._next_sequence(task_id),
                event_type="side_effect.retry_authorized",
                actor=actor,
                invocation_id=current.invocation_id or run.invocation_id,
                correlation_id=current.correlation_id,
                payload={
                    "record_id": str(record_id),
                    "reason": reason,
                },
                registry_generation=run.registry_generation,
            )
            try:
                await self._store.commit(
                    ExecutionCommit(
                        events=(event,),
                        side_effect_record=updated,
                    ),
                )
                return updated
            except (EventSequenceError, SideEffectConflictError):
                if attempt == _SIDE_EFFECT_COMMIT_RETRIES - 1:
                    raise
                await asyncio.sleep(0)
        raise AssertionError("side-effect recovery retry loop exhausted")

    async def create_task(
        self,
        *,
        objective: str,
        agent_id: str,
        source: TaskSource = TaskSource.USER,
        constraints: tuple[str, ...] = (),
        acceptance_criteria: tuple[str, ...] = (),
        execution_contract: ExecutionContract | None = None,
        metadata: JsonObject | None = None,
        idempotency_key: str | None = None,
    ) -> Task:
        """Create a durable task and its first immutable event."""
        request_values = {
            "objective": objective,
            "agent_id": agent_id,
            "source": source.value,
            "constraints": list(constraints),
            "acceptance_criteria": list(acceptance_criteria),
            "execution_contract": (
                execution_contract.model_dump(mode="json")
                if execution_contract is not None
                else None
            ),
            "metadata": metadata or {},
        }
        request_hash = self._request_hash(request_values)
        operation = "tasks.create"
        existing = await self._idempotent_response(
            operation=operation,
            key=idempotency_key,
            request_hash=request_hash,
        )
        if existing is not None:
            return Task.model_validate(existing)
        now = self._clock()
        task = Task(
            objective=objective,
            source=source,
            agent_id=agent_id,
            constraints=constraints,
            acceptance_criteria=acceptance_criteria,
            execution_contract=execution_contract,
            created_at=now,
            updated_at=now,
            metadata=metadata or {},
        )
        objective_hash = hashlib.sha256(objective.encode("utf-8")).hexdigest()
        event = self._event(
            task_id=task.task_id,
            sequence=1,
            event_type="task.created",
            payload={
                "agent_id": agent_id,
                "source": source.value,
                "objective_sha256": objective_hash,
            },
        )
        idempotency = self._idempotency_record(
            operation=operation,
            key=idempotency_key,
            request_hash=request_hash,
            response=task.model_dump(mode="json"),
        )
        await self._store.commit(
            ExecutionCommit(
                events=(event,),
                task=task,
                create_task=True,
                idempotency=idempotency,
            ),
        )
        return task

    async def plan_task(
        self,
        task_id: UUID,
        *,
        steps: tuple[PlanStep, ...],
        acceptance_criteria: tuple[str, ...] = (),
    ) -> tuple[Task, Plan]:
        """Attach the first plan and move a created task to planned."""
        current = await self._required_task(task_id)
        validate_task_transition(current.status, TaskStatus.PLANNED)
        plan = Plan(
            task_id=task_id,
            revision=1,
            steps=steps,
            acceptance_criteria=(
                acceptance_criteria or current.acceptance_criteria
            ),
            created_at=self._clock(),
        )
        updated = Task.model_validate(
            {
                **current.model_dump(),
                "status": TaskStatus.PLANNED,
                "version": current.version + 1,
                "updated_at": self._clock(),
            },
        )
        event = self._event(
            task_id=task_id,
            sequence=await self._next_sequence(task_id),
            event_type="task.planned",
            payload={
                "plan_id": str(plan.plan_id),
                "revision": plan.revision,
                "step_count": len(plan.steps),
            },
        )
        await self._store.commit(
            ExecutionCommit(
                events=(event,),
                task=updated,
                expected_task_version=current.version,
                plan=plan,
            ),
        )
        return updated, plan

    async def start_task(
        self,
        task_id: UUID,
        *,
        runner_id: str,
        strategy_id: str | None = None,
        registry_generation: int | None = None,
    ) -> tuple[Task, Run]:
        """Create and start a new run for a planned task."""
        current = await self._required_task(task_id)
        validate_task_transition(current.status, TaskStatus.RUNNING)
        attempts = await self._store.list_runs(task_id)
        now = self._clock()
        run_generation = registry_generation or self._registry_generation
        invocation_id = uuid4()
        run = Run(
            task_id=task_id,
            attempt=len(attempts) + 1,
            status=RunStatus.RUNNING,
            registry_generation=run_generation,
            runner_id=runner_id,
            strategy_id=strategy_id,
            invocation_id=invocation_id,
            correlation_id=invocation_id,
            started_at=now,
        )
        updated = Task.model_validate(
            {
                **current.model_dump(),
                "status": TaskStatus.RUNNING,
                "active_run_id": run.run_id,
                "version": current.version + 1,
                "updated_at": now,
            },
        )
        event = self._event(
            task_id=task_id,
            run_id=run.run_id,
            sequence=await self._next_sequence(task_id),
            event_type="run.started",
            source=runner_id,
            invocation_id=run.invocation_id,
            correlation_id=run.correlation_id,
            payload={
                "attempt": run.attempt,
                "runner_id": runner_id,
                "strategy_id": strategy_id,
            },
            registry_generation=run_generation,
        )
        await self._store.commit(
            ExecutionCommit(
                events=(event,),
                task=updated,
                expected_task_version=current.version,
                run=run,
                create_run=True,
            ),
        )
        return updated, run

    async def request_approval(
        self,
        task_id: UUID,
        *,
        action: str,
        risk: RiskLevel,
        requester: ActorRef,
        source: ApprovalSource = ApprovalSource.SYSTEM,
        approval_id: UUID | None = None,
        policy: str = "strict",
        continuation: ApprovalContinuation = (
            ApprovalContinuation.FAIL_ON_REJECTION
        ),
        redacted_arguments: JsonObject | None = None,
        display: ApprovalDisplay | None = None,
        expires_at: AwareDatetime | None = None,
        invocation_id: UUID | None = None,
        correlation_id: UUID | None = None,
    ) -> ApprovalRequest:
        """Suspend an active run at a durable approval boundary."""
        resolved_approval_id = approval_id or uuid4()
        for attempt in range(_APPROVAL_COMMIT_RETRIES):
            try:
                return await self._request_approval_once(
                    task_id,
                    action=action,
                    risk=risk,
                    requester=requester,
                    source=source,
                    approval_id=resolved_approval_id,
                    policy=policy,
                    continuation=continuation,
                    redacted_arguments=redacted_arguments,
                    display=display,
                    expires_at=expires_at,
                    invocation_id=invocation_id,
                    correlation_id=correlation_id,
                )
            except (EventSequenceError, TaskVersionConflictError):
                if attempt == _APPROVAL_COMMIT_RETRIES - 1:
                    raise
                await asyncio.sleep(0)
        raise AssertionError("approval retry loop exhausted")

    async def _request_approval_once(
        self,
        task_id: UUID,
        *,
        action: str,
        risk: RiskLevel,
        requester: ActorRef,
        source: ApprovalSource,
        approval_id: UUID,
        policy: str,
        continuation: ApprovalContinuation,
        redacted_arguments: JsonObject | None,
        display: ApprovalDisplay | None,
        expires_at: AwareDatetime | None,
        invocation_id: UUID | None,
        correlation_id: UUID | None,
    ) -> ApprovalRequest:
        """Commit one approval attempt against fresh task projections."""
        current = await self._required_task(task_id)
        run = await self._active_run(current)
        if current.status is not TaskStatus.WAITING_APPROVAL:
            validate_task_transition(
                current.status,
                TaskStatus.WAITING_APPROVAL,
            )
        if run.status is not RunStatus.WAITING_APPROVAL:
            validate_run_transition(
                run.status,
                RunStatus.WAITING_APPROVAL,
            )
        now = self._clock()
        sequence = await self._next_sequence(task_id)
        checkpoint = ExecutionCheckpoint(
            task_id=task_id,
            run_id=run.run_id,
            sequence=sequence,
            safe_to_resume=True,
            runner_cursor={
                "boundary": "approval",
                "approval_id": str(approval_id),
                "action": action,
            },
            created_at=now,
        )
        approval = ApprovalRequest(
            approval_id=approval_id,
            task_id=task_id,
            run_id=run.run_id,
            invocation_id=(invocation_id or run.invocation_id),
            correlation_id=(
                correlation_id or invocation_id or run.correlation_id
            ),
            source=source,
            action=action,
            risk=risk,
            requester=requester,
            policy=policy,
            continuation=continuation,
            checkpoint_id=checkpoint.checkpoint_id,
            redacted_arguments=redact_payload(redacted_arguments or {}),
            display=display,
            expires_at=expires_at,
            created_at=now,
        )
        updated_task = Task.model_validate(
            {
                **current.model_dump(),
                "status": TaskStatus.WAITING_APPROVAL,
                "version": current.version + 1,
                "updated_at": now,
            },
        )
        updated_run = Run.model_validate(
            {
                **run.model_dump(),
                "status": RunStatus.WAITING_APPROVAL,
            },
        )
        event = self._event(
            task_id=task_id,
            run_id=run.run_id,
            sequence=sequence,
            event_type="approval.requested",
            actor=requester,
            source=f"approval.{source.value}",
            invocation_id=approval.invocation_id,
            correlation_id=approval.correlation_id,
            payload={
                "approval_id": str(approval.approval_id),
                "action": action,
                "risk": risk.value,
                "source": source.value,
                "checkpoint_id": str(checkpoint.checkpoint_id),
                "invocation_id": (
                    str(approval.invocation_id)
                    if approval.invocation_id is not None
                    else None
                ),
                "correlation_id": (
                    str(approval.correlation_id)
                    if approval.correlation_id is not None
                    else None
                ),
                "arguments": approval.redacted_arguments,
                "display": (
                    display.model_dump(mode="json") if display else None
                ),
                "expires_at": (
                    approval.expires_at.isoformat()
                    if approval.expires_at is not None
                    else None
                ),
            },
            registry_generation=run.registry_generation,
        )
        await self._store.commit(
            ExecutionCommit(
                events=(event,),
                task=updated_task,
                expected_task_version=current.version,
                run=updated_run,
                approval_request=approval,
                checkpoint=checkpoint,
            ),
        )
        return approval

    @staticmethod
    def _approval_outcome(
        request: ApprovalRequest,
        decision: ApprovalDecisionValue,
        *,
        has_other_pending: bool,
    ) -> tuple[TaskStatus, RunStatus, str | None]:
        """Return aggregate targets for one approval decision."""
        resumes = (
            decision is ApprovalDecisionValue.APPROVED
            or request.continuation is ApprovalContinuation.RESUME_ON_DECISION
        )
        if resumes and has_other_pending:
            return (
                TaskStatus.WAITING_APPROVAL,
                RunStatus.WAITING_APPROVAL,
                None,
            )
        if resumes:
            return TaskStatus.RUNNING, RunStatus.RUNNING, "run.resumed"
        if decision is ApprovalDecisionValue.CANCELLED:
            return (
                TaskStatus.CANCELLED,
                RunStatus.CANCELLED,
                "run.cancelled",
            )
        return TaskStatus.FAILED, RunStatus.FAILED, "run.failed"

    def _cancel_pending_approvals(
        self,
        *,
        task_id: UUID,
        run: Run,
        pending_records: Sequence[
            tuple[ApprovalRequest, ApprovalDecision | None]
        ],
        sequence: int,
    ) -> tuple[list[ApprovalDecision], list[ExecutionEvent], int]:
        """Build immutable cancellations for a terminal run decision."""
        decisions = []
        events = []
        for pending_request, _ in pending_records:
            cancellation = ApprovalDecision(
                approval_id=pending_request.approval_id,
                decision=ApprovalDecisionValue.CANCELLED,
                actor=self._system_actor,
                reason="The task reached a terminal approval decision",
                decided_at=self._clock(),
            )
            decisions.append(cancellation)
            events.append(
                self._event(
                    task_id=task_id,
                    run_id=run.run_id,
                    sequence=sequence,
                    event_type="approval.decided",
                    actor=self._system_actor,
                    source=f"approval.{pending_request.source.value}",
                    invocation_id=(
                        pending_request.invocation_id or run.invocation_id
                    ),
                    correlation_id=(
                        pending_request.correlation_id
                        or pending_request.invocation_id
                    ),
                    payload={
                        "approval_id": str(pending_request.approval_id),
                        "decision": "cancelled",
                        "scope": "exact",
                    },
                    registry_generation=run.registry_generation,
                ),
            )
            sequence += 1
        return decisions, events, sequence

    async def decide_approval(
        self,
        task_id: UUID,
        approval_id: UUID,
        *,
        decision: ApprovalDecisionValue,
        actor: ActorRef,
        reason: str,
        scope: str = "exact",
        idempotency_key: str | None = None,
    ) -> ApprovalDecision:
        """Resolve an approval and atomically continue or stop its run."""
        operation = "tasks.approvals.decide"
        request_hash = self._request_hash(
            {
                "task_id": str(task_id),
                "approval_id": str(approval_id),
                "decision": decision.value,
                "actor": actor.model_dump(mode="json"),
                "reason": reason,
                "scope": scope,
            },
        )
        existing = await self._idempotent_response(
            operation=operation,
            key=idempotency_key,
            request_hash=request_hash,
        )
        if existing is not None:
            return ApprovalDecision.model_validate(existing)
        approval_record = await self._store.get_approval(approval_id)
        if approval_record is None or approval_record[0].task_id != task_id:
            raise ApprovalNotFoundError(str(approval_id))
        request, existing_decision = approval_record
        if existing_decision is not None:
            raise ApprovalAlreadyResolvedError(str(approval_id))

        current = await self._required_task(task_id)
        run = await self._active_run(current)
        pending_records = await self._store.list_approvals(
            task_id,
            run_id=run.run_id,
            status=ApprovalStatus.PENDING,
        )
        other_pending = [
            item
            for item in pending_records
            if item[0].approval_id != approval_id
        ]
        approval_decision = ApprovalDecision(
            approval_id=approval_id,
            decision=decision,
            actor=actor,
            scope=scope,
            reason=reason,
            decided_at=self._clock(),
        )
        sequence = await self._next_sequence(task_id)
        events = [
            self._event(
                task_id=task_id,
                run_id=run.run_id,
                sequence=sequence,
                event_type="approval.decided",
                actor=actor,
                source=f"approval.{request.source.value}",
                invocation_id=(request.invocation_id or run.invocation_id),
                correlation_id=(
                    request.correlation_id or request.invocation_id
                ),
                payload={
                    "approval_id": str(approval_id),
                    "decision": decision.value,
                    "scope": scope,
                },
                registry_generation=run.registry_generation,
            ),
        ]
        sequence += 1

        task_target, run_target, result_event_type = self._approval_outcome(
            request,
            decision,
            has_other_pending=bool(other_pending),
        )
        additional_decisions: list[ApprovalDecision] = []
        if task_target in {TaskStatus.FAILED, TaskStatus.CANCELLED}:
            (
                additional_decisions,
                cancellation_events,
                sequence,
            ) = self._cancel_pending_approvals(
                task_id=task_id,
                run=run,
                pending_records=other_pending,
                sequence=sequence,
            )
            events.extend(cancellation_events)

        if current.status is not task_target:
            validate_task_transition(current.status, task_target)
        if run.status is not run_target:
            validate_run_transition(run.status, run_target)
        now = self._clock()
        updated_task = Task.model_validate(
            {
                **current.model_dump(),
                "status": task_target,
                "version": current.version + 1,
                "updated_at": now,
            },
        )
        updated_run_values = {
            **run.model_dump(),
            "status": run_target,
        }
        if run_target in {
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            updated_run_values["finished_at"] = now
        updated_run = Run.model_validate(updated_run_values)
        if result_event_type is not None:
            events.append(
                self._event(
                    task_id=task_id,
                    run_id=run.run_id,
                    sequence=sequence,
                    event_type=result_event_type,
                    source=run.runner_id,
                    invocation_id=run.invocation_id,
                    correlation_id=run.correlation_id,
                    payload={"approval_id": str(request.approval_id)},
                    registry_generation=run.registry_generation,
                ),
            )
        await self._store.commit(
            ExecutionCommit(
                events=tuple(events),
                task=updated_task,
                expected_task_version=current.version,
                run=updated_run,
                approval_decision=approval_decision,
                additional_approval_decisions=tuple(
                    additional_decisions,
                ),
                idempotency=self._idempotency_record(
                    operation=operation,
                    key=idempotency_key,
                    request_hash=request_hash,
                    response=approval_decision.model_dump(mode="json"),
                ),
            ),
        )
        return approval_decision

    async def complete_task(self, task_id: UUID) -> Task:
        """Complete only after artifacts and verification satisfy contract."""
        current = await self._required_task(task_id)
        run = await self._active_run(current)
        projection = await load_task_result_projection(self, task_id)
        package = validate_result_projection(current, run, projection)
        return await self._finish_task(
            task_id,
            task_status=TaskStatus.COMPLETED,
            run_status=RunStatus.SUCCEEDED,
            event_type="run.completed",
            payload={
                "artifact_ids": [
                    str(item.artifact_id) for item in package.artifacts
                ],
                "evidence_ids": [
                    str(item.evidence_id) for item in package.evidence
                ],
                "verification_ids": [
                    str(item.verification_id) for item in package.verifications
                ],
            },
        )

    async def fail_task(
        self,
        task_id: UUID,
        *,
        error_summary: str,
    ) -> Task:
        """Mark an active run failed without persisting raw exceptions."""
        return await self._finish_task(
            task_id,
            task_status=TaskStatus.FAILED,
            run_status=RunStatus.FAILED,
            event_type="run.failed",
            payload={"error_summary": error_summary},
        )

    async def _finish_task(
        self,
        task_id: UUID,
        *,
        task_status: TaskStatus,
        run_status: RunStatus,
        event_type: str,
        payload: JsonObject | None = None,
    ) -> Task:
        current = await self._required_task(task_id)
        run = await self._active_run(current)
        validate_task_transition(current.status, task_status)
        validate_run_transition(run.status, run_status)
        now = self._clock()
        updated_task = Task.model_validate(
            {
                **current.model_dump(),
                "status": task_status,
                "version": current.version + 1,
                "updated_at": now,
            },
        )
        updated_run = Run.model_validate(
            {
                **run.model_dump(),
                "status": run_status,
                "finished_at": now,
            },
        )
        sequence = await self._next_sequence(task_id)
        pending_records = await self._store.list_approvals(
            task_id,
            run_id=run.run_id,
            status=ApprovalStatus.PENDING,
        )
        (
            cancelled_approvals,
            events,
            sequence,
        ) = self._cancel_pending_approvals(
            task_id=task_id,
            run=run,
            pending_records=pending_records,
            sequence=sequence,
        )
        events.append(
            self._event(
                task_id=task_id,
                run_id=run.run_id,
                sequence=sequence,
                event_type=event_type,
                source=run.runner_id,
                invocation_id=run.invocation_id,
                correlation_id=run.correlation_id,
                payload=payload,
                registry_generation=run.registry_generation,
            ),
        )
        await self._store.commit(
            ExecutionCommit(
                events=tuple(events),
                task=updated_task,
                expected_task_version=current.version,
                run=updated_run,
                additional_approval_decisions=tuple(
                    cancelled_approvals,
                ),
            ),
        )
        return updated_task

    async def cancel_task(
        self,
        task_id: UUID,
        *,
        idempotency_key: str | None = None,
    ) -> Task:
        """Cancel a task and its active run when present."""
        operation = "tasks.cancel"
        request_hash = self._request_hash({"task_id": str(task_id)})
        existing = await self._idempotent_response(
            operation=operation,
            key=idempotency_key,
            request_hash=request_hash,
        )
        if existing is not None:
            return Task.model_validate(existing)
        current = await self._required_task(task_id)
        validate_task_transition(current.status, TaskStatus.CANCELLED)
        now = self._clock()
        updated_task = Task.model_validate(
            {
                **current.model_dump(),
                "status": TaskStatus.CANCELLED,
                "version": current.version + 1,
                "updated_at": now,
            },
        )
        run = None
        event_type = "task.cancelled"
        approval_decisions: tuple[ApprovalDecision, ...] = ()
        events: list[ExecutionEvent] = []
        sequence = await self._next_sequence(task_id)
        if current.active_run_id is not None:
            run = await self._active_run(current)
            validate_run_transition(run.status, RunStatus.CANCELLED)
            pending_records = await self._store.list_approvals(
                task_id,
                run_id=run.run_id,
                status=ApprovalStatus.PENDING,
            )
            (
                cancelled_approvals,
                approval_events,
                sequence,
            ) = self._cancel_pending_approvals(
                task_id=task_id,
                run=run,
                pending_records=pending_records,
                sequence=sequence,
            )
            approval_decisions = tuple(cancelled_approvals)
            events.extend(approval_events)
            run = Run.model_validate(
                {
                    **run.model_dump(),
                    "status": RunStatus.CANCELLED,
                    "finished_at": now,
                },
            )
            event_type = "run.cancelled"
        events.append(
            self._event(
                task_id=task_id,
                run_id=run.run_id if run else None,
                sequence=sequence,
                event_type=event_type,
                source=(run.runner_id if run else None),
                invocation_id=(run.invocation_id if run else None),
                correlation_id=(run.correlation_id if run else None),
                registry_generation=(run.registry_generation if run else None),
            ),
        )
        await self._store.commit(
            ExecutionCommit(
                events=tuple(events),
                task=updated_task,
                expected_task_version=current.version,
                run=run,
                additional_approval_decisions=approval_decisions,
                idempotency=self._idempotency_record(
                    operation=operation,
                    key=idempotency_key,
                    request_hash=request_hash,
                    response=updated_task.model_dump(mode="json"),
                ),
            ),
        )
        return updated_task

    async def recover_orphaned_task(
        self,
        task_id: UUID,
        *,
        expected_run_id: UUID | None = None,
    ) -> Task:
        """Fail one run whose process-local execution handle was lost.

        Approval boundaries are safe checkpoints, so the failed Task can be
        resumed as a new attempt. Any operation that was prepared but never
        finalized is marked uncertain before the run becomes terminal.
        """
        current = await self._required_task(task_id)
        if (
            expected_run_id is not None
            and current.active_run_id != expected_run_id
        ):
            return current
        if current.status not in {
            TaskStatus.RUNNING,
            TaskStatus.WAITING_APPROVAL,
        }:
            return current
        run = await self._active_run(current)
        records = await self._store.list_side_effects(
            task_id,
            run_id=run.run_id,
        )
        for record in records:
            if record.status is SideEffectStatus.PREPARED:
                await self.finish_side_effect(
                    record.record_id,
                    status=SideEffectStatus.UNCERTAIN,
                    error_code="RuntimeProcessLost",
                )
        return await self.fail_task(
            task_id,
            error_summary="RuntimeProcessLost",
        )

    async def suspend_task(
        self,
        task_id: UUID,
        *,
        runner_cursor: JsonValue = None,
        workspace_checkpoint_ref: str | None = None,
    ) -> ExecutionCheckpoint:
        """Suspend an active run and persist a safe resume boundary."""
        _validate_checkpoint_payload(
            runner_cursor,
            workspace_checkpoint_ref,
        )
        current = await self._required_task(task_id)
        run = await self._active_run(current)
        validate_task_transition(current.status, TaskStatus.SUSPENDED)
        validate_run_transition(run.status, RunStatus.SUSPENDED)
        now = self._clock()
        sequence = await self._next_sequence(task_id)
        checkpoint = ExecutionCheckpoint(
            task_id=task_id,
            run_id=run.run_id,
            sequence=sequence,
            safe_to_resume=True,
            runner_cursor=runner_cursor,
            workspace_checkpoint_ref=workspace_checkpoint_ref,
            created_at=now,
        )
        updated_task = Task.model_validate(
            {
                **current.model_dump(),
                "status": TaskStatus.SUSPENDED,
                "version": current.version + 1,
                "updated_at": now,
            },
        )
        updated_run = Run.model_validate(
            {
                **run.model_dump(),
                "status": RunStatus.SUSPENDED,
                "checkpoint_id": checkpoint.checkpoint_id,
            },
        )
        event = self._event(
            task_id=task_id,
            run_id=run.run_id,
            sequence=sequence,
            event_type="run.suspended",
            source=run.runner_id,
            invocation_id=run.invocation_id,
            correlation_id=run.correlation_id,
            payload={"checkpoint_id": str(checkpoint.checkpoint_id)},
            registry_generation=run.registry_generation,
        )
        await self._store.commit(
            ExecutionCommit(
                events=(event,),
                task=updated_task,
                expected_task_version=current.version,
                run=updated_run,
                checkpoint=checkpoint,
            ),
        )
        return checkpoint

    async def create_checkpoint(
        self,
        task_id: UUID,
        run_id: UUID,
        *,
        runner_cursor: JsonValue = None,
        workspace_checkpoint_ref: str | None = None,
        idempotency_key: str | None = None,
    ) -> ExecutionCheckpoint:
        """Persist a safe boundary without changing the active run state."""
        _validate_checkpoint_payload(
            runner_cursor,
            workspace_checkpoint_ref,
        )
        operation = "tasks.checkpoint.create"
        request_hash = self._request_hash(
            {
                "task_id": str(task_id),
                "run_id": str(run_id),
                "runner_cursor": runner_cursor,
                "workspace_checkpoint_ref": workspace_checkpoint_ref,
            },
        )
        existing = await self._idempotent_response(
            operation=operation,
            key=idempotency_key,
            request_hash=request_hash,
        )
        if existing is not None:
            return ExecutionCheckpoint.model_validate(existing)
        current = await self._required_task(task_id)
        run = await self._active_run(current)
        if run.run_id != run_id:
            raise RunNotFoundError(str(run_id))
        if (
            current.status is not TaskStatus.RUNNING
            or run.status is not RunStatus.RUNNING
        ):
            raise RunNotActiveError(
                "checkpoint creation requires an active running run",
            )
        now = self._clock()
        sequence = await self._next_sequence(task_id)
        checkpoint = ExecutionCheckpoint(
            task_id=task_id,
            run_id=run_id,
            sequence=sequence,
            safe_to_resume=True,
            runner_cursor=runner_cursor,
            workspace_checkpoint_ref=workspace_checkpoint_ref,
            created_at=now,
        )
        event = self._event(
            task_id=task_id,
            run_id=run_id,
            sequence=sequence,
            event_type="checkpoint.created",
            actor=ActorRef(type=ActorType.RUNNER, id=run.runner_id),
            source=run.runner_id,
            invocation_id=run.invocation_id,
            correlation_id=run.correlation_id,
            payload={"checkpoint_id": str(checkpoint.checkpoint_id)},
            registry_generation=run.registry_generation,
        )
        await self._store.commit(
            ExecutionCommit(
                events=(event,),
                checkpoint=checkpoint,
                idempotency=self._idempotency_record(
                    operation=operation,
                    key=idempotency_key,
                    request_hash=request_hash,
                    response=checkpoint.model_dump(mode="json"),
                ),
            ),
        )
        return checkpoint

    async def resume_task(
        self,
        task_id: UUID,
        *,
        strategy_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[Task, Run]:
        """Create a fresh run from the newest safe checkpoint."""
        outcome = await self.resume_task_with_outcome(
            task_id,
            strategy_id=strategy_id,
            idempotency_key=idempotency_key,
        )
        return outcome.task, outcome.run

    async def resume_task_with_outcome(
        self,
        task_id: UUID,
        *,
        strategy_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> TaskResumeOutcome:
        """Resume once and expose idempotent replay to the orchestrator."""
        operation = "tasks.resume"
        request_hash = self._resume_request_hash(
            task_id,
            strategy_id,
        )
        existing = await self._idempotent_response(
            operation=operation,
            key=idempotency_key,
            request_hash=request_hash,
        )
        if existing is not None:
            return await self._resume_replay_outcome(task_id, existing)
        current = await self._required_task(task_id)
        if current.status not in {TaskStatus.SUSPENDED, TaskStatus.FAILED}:
            validate_task_transition(current.status, TaskStatus.RUNNING)
        checkpoint = await self._store.latest_resumable(task_id)
        if checkpoint is None:
            raise CheckpointNotResumableError(str(task_id))
        side_effects = await self._store.list_side_effects(task_id)
        unresolved_effects = [
            record
            for record in side_effects
            if record.status
            in {SideEffectStatus.PREPARED, SideEffectStatus.UNCERTAIN}
        ]
        if unresolved_effects:
            raise SideEffectRecoveryRequiredError(
                ",".join(
                    str(record.record_id) for record in unresolved_effects
                ),
            )
        attempts = await self._store.list_runs(task_id)
        now = self._clock()
        invocation_id = uuid4()
        run = Run(
            task_id=task_id,
            attempt=len(attempts) + 1,
            status=RunStatus.RUNNING,
            registry_generation=self._registry_generation,
            runner_id=attempts[-1].runner_id,
            strategy_id=(strategy_id or attempts[-1].strategy_id),
            invocation_id=invocation_id,
            correlation_id=(attempts[-1].correlation_id or uuid4()),
            checkpoint_id=checkpoint.checkpoint_id,
            started_at=now,
            metadata={"resumed_from_run_id": str(checkpoint.run_id)},
        )
        sequence = await self._next_sequence(task_id)
        events: list[ExecutionEvent] = []
        if current.status is TaskStatus.FAILED:
            validate_task_transition(current.status, TaskStatus.PLANNED)
            events.append(
                self._event(
                    task_id=task_id,
                    sequence=sequence,
                    event_type="task.planned",
                    invocation_id=run.invocation_id,
                    correlation_id=run.correlation_id,
                    payload={"resume": True},
                ),
            )
            sequence += 1
        else:
            validate_task_transition(current.status, TaskStatus.RUNNING)
        events.append(
            self._event(
                task_id=task_id,
                run_id=run.run_id,
                sequence=sequence,
                event_type="run.started",
                source=run.runner_id,
                invocation_id=run.invocation_id,
                correlation_id=run.correlation_id,
                payload={
                    "attempt": run.attempt,
                    "runner_id": run.runner_id,
                    "strategy_id": run.strategy_id,
                    "resumed_from_checkpoint_id": str(
                        checkpoint.checkpoint_id,
                    ),
                },
                registry_generation=run.registry_generation,
            ),
        )
        updated_task = Task.model_validate(
            {
                **current.model_dump(),
                "status": TaskStatus.RUNNING,
                "active_run_id": run.run_id,
                "version": current.version + 1,
                "updated_at": now,
            },
        )
        await self._store.commit(
            ExecutionCommit(
                events=tuple(events),
                task=updated_task,
                expected_task_version=current.version,
                run=run,
                create_run=True,
                idempotency=self._idempotency_record(
                    operation=operation,
                    key=idempotency_key,
                    request_hash=request_hash,
                    response={
                        "task": updated_task.model_dump(mode="json"),
                        "run": run.model_dump(mode="json"),
                    },
                ),
            ),
        )
        return TaskResumeOutcome(
            task=updated_task,
            run=run,
            replayed=False,
        )

    async def replayed_resume_task(
        self,
        task_id: UUID,
        *,
        strategy_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> TaskResumeOutcome | None:
        """Return an existing resume result without resolving capabilities."""
        existing = await self._idempotent_response(
            operation="tasks.resume",
            key=idempotency_key,
            request_hash=self._resume_request_hash(task_id, strategy_id),
        )
        if existing is None:
            return None
        return await self._resume_replay_outcome(task_id, existing)

    @classmethod
    def _resume_request_hash(
        cls,
        task_id: UUID,
        strategy_id: str | None,
    ) -> str:
        return cls._request_hash(
            {
                "task_id": str(task_id),
                "strategy_id": strategy_id,
            },
        )

    async def _resume_replay_outcome(
        self,
        task_id: UUID,
        response: JsonObject,
    ) -> TaskResumeOutcome:
        replayed_run = Run.model_validate(response["run"])
        stored_runs = await self._store.list_runs(task_id)
        current_run = next(
            (
                item
                for item in stored_runs
                if item.run_id == replayed_run.run_id
            ),
            replayed_run,
        )
        return TaskResumeOutcome(
            task=await self._required_task(task_id),
            run=current_run,
            replayed=True,
        )

    async def get_task(self, task_id: UUID) -> Task | None:
        """Return one durable task."""
        return await self._store.get_task(task_id)

    async def get_approval(
        self,
        approval_id: UUID,
    ) -> tuple[ApprovalRequest, ApprovalDecision | None] | None:
        """Return one durable approval record."""
        return await self._store.get_approval(approval_id)

    async def list_approvals(
        self,
        task_id: UUID,
        *,
        run_id: UUID | None = None,
        status: ApprovalStatus | None = None,
    ) -> Sequence[tuple[ApprovalRequest, ApprovalDecision | None]]:
        """Return task-scoped approval projections in creation order."""
        await self._required_task(task_id)
        return await self._store.list_approvals(
            task_id,
            run_id=run_id,
            status=status,
        )

    async def get_side_effect(
        self,
        record_id: UUID,
    ) -> SideEffectRecord | None:
        """Return one durable side-effect record."""
        return await self._store.get_side_effect(record_id)

    async def list_side_effects(
        self,
        task_id: UUID,
        *,
        run_id: UUID | None = None,
    ) -> Sequence[SideEffectRecord]:
        """Return Task-scoped side effects in creation order."""
        await self._required_task(task_id)
        return await self._store.list_side_effects(task_id, run_id=run_id)

    async def list_tasks(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Sequence[Task]:
        """Return a stable page of durable tasks."""
        return await self._store.list_tasks(cursor=cursor, limit=limit)

    async def list_runs(self, task_id: UUID) -> Sequence[Run]:
        """Return all durable attempts for a task."""
        return await self._store.list_runs(task_id)

    async def latest_plan(self, task_id: UUID) -> Plan | None:
        """Return the newest plan revision for a task."""
        return await self._store.latest_plan(task_id)

    async def projection_snapshot(
        self,
        task_id: UUID,
        *,
        event_limit: int = 1000,
    ) -> TaskProjectionSnapshot:
        """Return one transactionally consistent Workbench snapshot."""
        return await self._store.read_projection(
            task_id,
            event_limit=event_limit,
        )

    async def list_events(
        self,
        task_id: UUID,
        *,
        after_sequence: int = 0,
        limit: int = 200,
    ) -> Sequence[ExecutionEvent]:
        """Return a task timeline in ascending sequence order."""
        return await self._store.list_events(
            task_id,
            after_sequence=after_sequence,
            limit=limit,
        )
