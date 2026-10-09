# -*- coding: utf-8 -*-
"""Host admission boundary for explicit Conversation outcomes."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from ..kernel import (
    ConversationArtifactHistoryPort,
    ConversationOutcome,
    ConversationOutcomeDeclaration,
    ConversationOutcomeStatus,
    ConversationOutcomeStore,
    OutcomeProducerRegistration,
    VerificationStatus,
)
from ..kernel.ports import TaskStore
from ..tasks.results import (
    CompletionRequirementsError,
    TaskEventReader,
    load_task_result_projection,
    validate_result_projection,
)


class OutcomeAdmissionError(ValueError):
    """Raised when the Host cannot trust an outcome declaration."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class OutcomeProducerLease:
    """Producer admission frozen for one already-open Invocation."""

    registration: OutcomeProducerRegistration


class HostOutcomeBroker:
    """Admit system and plugin declarations through one Host policy."""

    def __init__(
        self,
        *,
        store: ConversationOutcomeStore,
        conversation_artifacts: ConversationArtifactHistoryPort,
        producers: Iterable[OutcomeProducerRegistration] = (),
        tasks: TaskStore | None = None,
        task_events: TaskEventReader | None = None,
    ) -> None:
        self._store = store
        self._conversation_artifacts = conversation_artifacts
        self._tasks = tasks
        self._task_events = task_events
        self._producers: dict[str, OutcomeProducerRegistration] = {}
        for producer in producers:
            self.register(producer)

    def register(self, producer: OutcomeProducerRegistration) -> None:
        """Activate one Host-approved producer without restarting runtime."""
        existing = self._producers.get(producer.producer_id)
        if existing is not None and existing != producer:
            raise OutcomeAdmissionError("producer_registration_conflict")
        self._producers[producer.producer_id] = producer

    def unregister(self, producer_id: str) -> None:
        """Stop new declarations from one producer immediately."""
        self._producers.pop(producer_id, None)

    def is_registered(self, producer_id: str) -> bool:
        """Return whether new Invocations may bind this producer."""
        return producer_id in self._producers

    def bind(self, producer_id: str) -> OutcomeProducerLease | None:
        """Pin current producer admission for one Invocation."""
        producer = self._producers.get(producer_id)
        return OutcomeProducerLease(producer) if producer is not None else None

    def _producer(
        self,
        declaration: ConversationOutcomeDeclaration,
        lease: OutcomeProducerLease | None = None,
    ) -> OutcomeProducerRegistration:
        producer = (
            lease.registration
            if lease is not None
            else self._producers.get(declaration.producer_id)
        )
        if producer is None:
            raise OutcomeAdmissionError("producer_not_registered")
        if producer.producer_id != declaration.producer_id:
            raise OutcomeAdmissionError("producer_identity_mismatch")
        if (
            declaration.task_id is not None
            and not producer.allow_task_outcomes
        ):
            raise OutcomeAdmissionError("producer_task_outcomes_denied")
        return producer

    @staticmethod
    def _require_subset(
        requested: tuple[UUID, ...],
        available: set[UUID],
        code: str,
    ) -> None:
        if not set(requested) <= available:
            raise OutcomeAdmissionError(code)

    async def _validate_chat_references(
        self,
        declaration: ConversationOutcomeDeclaration,
    ) -> None:
        if declaration.verification_ids:
            raise OutcomeAdmissionError("chat_verification_not_task_owned")
        records = await self._conversation_artifacts.list_for_conversation(
            declaration.chat_id,
            limit=1000,
        )
        self._require_subset(
            declaration.artifact_ids,
            {record.artifact.artifact_id for record in records},
            "artifact_not_owned_by_conversation",
        )
        self._require_subset(
            declaration.evidence_ids,
            {record.evidence.evidence_id for record in records},
            "evidence_not_owned_by_conversation",
        )

    async def _task_references(
        self,
        declaration: ConversationOutcomeDeclaration,
    ) -> tuple[tuple[UUID, ...], tuple[UUID, ...], tuple[UUID, ...]]:
        if self._tasks is None or self._task_events is None:
            raise OutcomeAdmissionError("task_authority_unavailable")
        assert declaration.task_id is not None
        assert declaration.run_id is not None
        task = await self._tasks.get_task(declaration.task_id)
        run = await self._tasks.get_run(declaration.run_id)
        if task is None:
            raise OutcomeAdmissionError("task_not_found")
        if run is None or run.task_id != task.task_id:
            raise OutcomeAdmissionError("run_not_owned_by_task")
        if task.agent_id != declaration.agent_id:
            raise OutcomeAdmissionError("task_not_owned_by_agent")
        if declaration.chat_id not in (
            task.metadata.get("conversation_id"),
            task.metadata.get("chat_id"),
        ):
            raise OutcomeAdmissionError("task_not_owned_by_conversation")
        if (
            run.correlation_id is not None
            and run.correlation_id != declaration.correlation_id
        ):
            raise OutcomeAdmissionError("run_correlation_mismatch")
        projection = await load_task_result_projection(
            self._task_events,
            task.task_id,
        )
        package = projection.package(task.task_id, run.run_id)
        if declaration.status is ConversationOutcomeStatus.ACHIEVED:
            if (
                task.execution_contract is None
                or task.execution_contract.verification_policy is None
            ):
                raise OutcomeAdmissionError(
                    "task_verification_policy_missing",
                )
            try:
                package = validate_result_projection(task, run, projection)
            except CompletionRequirementsError as exc:
                raise OutcomeAdmissionError(
                    f"task_completion_rejected:{exc}",
                ) from exc
            if not any(
                item.status is VerificationStatus.PASSED
                for item in package.verifications
            ):
                raise OutcomeAdmissionError(
                    "task_passed_verification_missing",
                )
        artifact_ids = tuple(item.artifact_id for item in package.artifacts)
        evidence_ids = tuple(item.evidence_id for item in package.evidence)
        verification_ids = tuple(
            item.verification_id for item in package.verifications
        )
        self._require_subset(
            declaration.artifact_ids,
            set(artifact_ids),
            "artifact_not_owned_by_task",
        )
        self._require_subset(
            declaration.evidence_ids,
            set(evidence_ids),
            "evidence_not_owned_by_task",
        )
        self._require_subset(
            declaration.verification_ids,
            set(verification_ids),
            "verification_not_owned_by_run",
        )
        if declaration.status is ConversationOutcomeStatus.ACHIEVED:
            return artifact_ids, evidence_ids, verification_ids
        return (
            declaration.artifact_ids,
            declaration.evidence_ids,
            declaration.verification_ids,
        )

    async def declare(
        self,
        declaration: ConversationOutcomeDeclaration,
    ) -> ConversationOutcome:
        """Validate, materialize, and persist one explicit outcome."""
        return await self._declare(declaration)

    async def declare_bound(
        self,
        lease: OutcomeProducerLease,
        declaration: ConversationOutcomeDeclaration,
    ) -> ConversationOutcome:
        """Admit through permission pinned before a hot replacement."""
        return await self._declare(declaration, lease)

    async def _declare(
        self,
        declaration: ConversationOutcomeDeclaration,
        lease: OutcomeProducerLease | None = None,
    ) -> ConversationOutcome:
        """Apply admission and persist one materialized outcome."""
        self._producer(declaration, lease)
        get_outcome = getattr(self._store, "get", None)
        existing = (
            await get_outcome(declaration.outcome_id)
            if callable(get_outcome)
            else None
        )
        if existing is not None:
            self._validate_idempotent_replay(existing, declaration)
            return existing
        references = (
            declaration.artifact_ids,
            declaration.evidence_ids,
            declaration.verification_ids,
        )
        if declaration.task_id is None:
            await self._validate_chat_references(declaration)
        else:
            references = await self._task_references(declaration)
        outcome = ConversationOutcome(
            outcome_id=declaration.outcome_id,
            agent_id=declaration.agent_id,
            chat_id=declaration.chat_id,
            correlation_id=declaration.correlation_id,
            status=declaration.status,
            producer_id=declaration.producer_id,
            summary=declaration.summary,
            invocation_id=declaration.invocation_id,
            registry_generation=declaration.registry_generation,
            task_id=declaration.task_id,
            run_id=declaration.run_id,
            artifact_ids=references[0],
            evidence_ids=references[1],
            verification_ids=references[2],
            supersedes_outcome_id=declaration.supersedes_outcome_id,
            created_at=declaration.declared_at,
        )
        try:
            await self._store.append(outcome)
        except Exception as error:  # noqa: BLE001
            get_after_conflict = getattr(self._store, "get", None)
            if not callable(get_after_conflict):
                raise
            concurrent = await get_after_conflict(outcome.outcome_id)
            if concurrent is None:
                raise
            try:
                self._validate_idempotent_replay(concurrent, declaration)
            except OutcomeAdmissionError as replay_error:
                raise error from replay_error
            return concurrent
        return outcome

    @staticmethod
    def _validate_idempotent_replay(
        existing: ConversationOutcome,
        declaration: ConversationOutcomeDeclaration,
    ) -> None:
        """Allow a new Invocation to confirm one exact business outcome."""
        comparable = (
            "outcome_id",
            "agent_id",
            "chat_id",
            "correlation_id",
            "status",
            "producer_id",
            "summary",
            "task_id",
            "run_id",
            "artifact_ids",
            "evidence_ids",
            "verification_ids",
            "supersedes_outcome_id",
        )
        if any(
            getattr(existing, field) != getattr(declaration, field)
            for field in comparable
        ):
            raise OutcomeAdmissionError("outcome_replay_conflict")


__all__ = ["HostOutcomeBroker", "OutcomeAdmissionError"]
