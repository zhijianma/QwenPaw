# -*- coding: utf-8 -*-
"""Tests for Host-owned Conversation Outcome admission."""

from uuid import uuid4

import pytest

from qwenpaw.kernel import (
    ConversationOutcomeDeclaration,
    ConversationOutcomeStatus,
    OutcomeProducerRegistration,
)
from qwenpaw.kernel.events import ExecutionEvent
from qwenpaw.kernel.models import (
    AcceptanceVerification,
    ActorRef,
    ActorType,
    ArtifactRef,
    ArtifactRequirement,
    CapabilityProviderKind,
    EvidenceRef,
    ExecutionContract,
    Run,
    Task,
    TaskSource,
    VerificationPolicy,
    VerificationResult,
    VerificationStatus,
    utc_now,
)
from qwenpaw.runtime.outcome_broker import (
    HostOutcomeBroker,
    OutcomeAdmissionError,
)
from qwenpaw.runtime.outcomes import lite_conversation_outcome_store


class _Artifacts:
    async def list_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 100,
    ) -> tuple:
        del conversation_id, limit
        return ()


class _Tasks:
    def __init__(self, task: Task, run: Run) -> None:
        self.task = task
        self.run = run

    async def get_task(self, task_id):
        return self.task if task_id == self.task.task_id else None

    async def get_run(self, run_id):
        return self.run if run_id == self.run.run_id else None


class _Events:
    def __init__(self, events: tuple[ExecutionEvent, ...]) -> None:
        self.events = events

    async def list_events(
        self,
        task_id,
        *,
        after_sequence: int = 0,
        limit: int = 200,
    ) -> tuple[ExecutionEvent, ...]:
        return tuple(
            event
            for event in self.events
            if event.task_id == task_id and event.sequence > after_sequence
        )[:limit]


def _event(
    task_id,
    run_id,
    sequence: int,
    *,
    artifacts: tuple[ArtifactRef, ...] = (),
    evidence: tuple[EvidenceRef, ...] = (),
    verification: VerificationResult | None = None,
) -> ExecutionEvent:
    return ExecutionEvent(
        task_id=task_id,
        run_id=run_id,
        sequence=sequence,
        event_type=(
            "verification.completed"
            if verification is not None
            else "artifact.created"
        ),
        registry_generation=1,
        actor=ActorRef(type=ActorType.RUNNER, id="runner.tests"),
        source=(
            verification.verifier_id
            if verification is not None
            else "runner.tests"
        ),
        payload=(
            {"result": verification.model_dump(mode="json")}
            if verification is not None
            else {}
        ),
        artifact_refs=artifacts,
        evidence_refs=evidence,
    )


def _producer(
    *,
    plugin: bool = False,
    allow_tasks: bool = False,
) -> OutcomeProducerRegistration:
    return OutcomeProducerRegistration(
        producer_id=(
            "example.plugin.outcome" if plugin else "qwenpaw.system.outcome"
        ),
        provider_kind=(
            CapabilityProviderKind.PLUGIN
            if plugin
            else CapabilityProviderKind.SYSTEM
        ),
        allow_task_outcomes=allow_tasks,
    )


@pytest.mark.asyncio
async def test_chat_outcome_requires_registered_producer(tmp_path) -> None:
    broker = HostOutcomeBroker(
        store=lite_conversation_outcome_store(tmp_path),
        conversation_artifacts=_Artifacts(),
    )
    declaration = ConversationOutcomeDeclaration(
        agent_id="default",
        conversation_id="chat-1",
        correlation_id=uuid4(),
        status=ConversationOutcomeStatus.PARTIAL,
        producer_id="example.plugin.outcome",
        summary="Work remains in progress.",
    )

    with pytest.raises(
        OutcomeAdmissionError,
        match="producer_not_registered",
    ):
        await broker.declare(declaration)

    broker.register(_producer(plugin=True))
    outcome = await broker.declare(declaration)
    assert outcome.outcome_id == declaration.outcome_id
    assert outcome.producer_id == declaration.producer_id


@pytest.mark.asyncio
async def test_outcome_replay_can_cross_invocations(tmp_path) -> None:
    broker = HostOutcomeBroker(
        store=lite_conversation_outcome_store(tmp_path),
        conversation_artifacts=_Artifacts(),
        producers=(_producer(),),
    )
    declaration = ConversationOutcomeDeclaration(
        agent_id="default",
        conversation_id="chat-1",
        correlation_id=uuid4(),
        status=ConversationOutcomeStatus.ACHIEVED,
        producer_id="qwenpaw.system.outcome",
        summary="The durable goal is complete.",
        invocation_id=uuid4(),
        registry_generation=1,
    )
    first = await broker.declare(declaration)

    replay = declaration.model_copy(
        update={
            "invocation_id": uuid4(),
            "registry_generation": 2,
            "declared_at": utc_now(),
        },
    )
    second = await broker.declare(replay)

    assert second == first


@pytest.mark.asyncio
async def test_outcome_replay_rejects_changed_business_claim(tmp_path) -> None:
    broker = HostOutcomeBroker(
        store=lite_conversation_outcome_store(tmp_path),
        conversation_artifacts=_Artifacts(),
        producers=(_producer(),),
    )
    declaration = ConversationOutcomeDeclaration(
        agent_id="default",
        conversation_id="chat-1",
        correlation_id=uuid4(),
        status=ConversationOutcomeStatus.ACHIEVED,
        producer_id="qwenpaw.system.outcome",
        summary="The durable goal is complete.",
    )
    await broker.declare(declaration)

    with pytest.raises(
        OutcomeAdmissionError,
        match="outcome_replay_conflict",
    ):
        await broker.declare(
            declaration.model_copy(update={"summary": "Changed claim."}),
        )


@pytest.mark.asyncio
async def test_chat_outcome_rejects_unowned_references(tmp_path) -> None:
    broker = HostOutcomeBroker(
        store=lite_conversation_outcome_store(tmp_path),
        conversation_artifacts=_Artifacts(),
        producers=(_producer(),),
    )
    declaration = ConversationOutcomeDeclaration(
        agent_id="default",
        conversation_id="chat-1",
        correlation_id=uuid4(),
        status=ConversationOutcomeStatus.PARTIAL,
        producer_id="qwenpaw.system.outcome",
        summary="An unowned artifact was claimed.",
        artifact_ids=(uuid4(),),
    )

    with pytest.raises(
        OutcomeAdmissionError,
        match="artifact_not_owned_by_conversation",
    ):
        await broker.declare(declaration)


def _verified_task():
    task_id = uuid4()
    run_id = uuid4()
    correlation_id = uuid4()
    criterion = "README installation is translated to Italian"
    contract = ExecutionContract(
        goal="Translate README installation",
        acceptance=(criterion,),
        required_artifacts=(ArtifactRequirement(kind="document.readme_it"),),
        verification_policy=VerificationPolicy(
            verifier_ids=("verifier.tests",),
            require_evidence=True,
        ),
    )
    task = Task(
        task_id=task_id,
        objective=contract.goal,
        acceptance_criteria=contract.acceptance,
        execution_contract=contract,
        source=TaskSource.USER,
        agent_id="default",
        metadata={"conversation_id": "chat-1"},
    )
    run = Run(
        run_id=run_id,
        task_id=task_id,
        attempt=1,
        registry_generation=1,
        runner_id="runner.tests",
        correlation_id=correlation_id,
    )
    artifact = ArtifactRef(
        kind="document.readme_it",
        uri="qwenpaw-artifact://sha256/readme-it",
        media_type="text/markdown",
        content_hash=f"sha256:{'0' * 64}",
        size_bytes=12,
    )
    evidence = EvidenceRef(
        artifact_id=artifact.artifact_id,
        claim="The translated installation section exists.",
        producer="verifier.tests",
    )
    verification = VerificationResult(
        task_id=task_id,
        run_id=run_id,
        verifier_id="verifier.tests",
        status=VerificationStatus.PASSED,
        acceptance=(
            AcceptanceVerification(
                criterion=criterion,
                passed=True,
                evidence_ids=(evidence.evidence_id,),
            ),
        ),
        artifact_ids=(artifact.artifact_id,),
        evidence_ids=(evidence.evidence_id,),
    )
    events = (
        _event(
            task_id,
            run_id,
            1,
            artifacts=(artifact,),
            evidence=(evidence,),
        ),
        _event(task_id, run_id, 2, verification=verification),
    )
    return task, run, correlation_id, events, verification


@pytest.mark.asyncio
async def test_task_achieved_uses_authoritative_completion_gate(
    tmp_path,
) -> None:
    task, run, correlation_id, events, verification = _verified_task()
    store = lite_conversation_outcome_store(tmp_path)
    producer = _producer(plugin=True, allow_tasks=True)
    declaration = ConversationOutcomeDeclaration(
        agent_id="default",
        conversation_id="chat-1",
        correlation_id=correlation_id,
        status=ConversationOutcomeStatus.ACHIEVED,
        producer_id=producer.producer_id,
        summary="The installation section is translated and verified.",
        task_id=task.task_id,
        run_id=run.run_id,
    )
    rejected = HostOutcomeBroker(
        store=store,
        conversation_artifacts=_Artifacts(),
        producers=(producer,),
        tasks=_Tasks(task, run),
        task_events=_Events(events[:1]),
    )
    with pytest.raises(
        OutcomeAdmissionError,
        match="task_completion_rejected",
    ):
        await rejected.declare(declaration)

    broker = HostOutcomeBroker(
        store=store,
        conversation_artifacts=_Artifacts(),
        producers=(producer,),
        tasks=_Tasks(task, run),
        task_events=_Events(events),
    )
    outcome = await broker.declare(declaration)
    assert outcome.verification_ids == (verification.verification_id,)
    assert len(outcome.artifact_ids) == 1
    assert len(outcome.evidence_ids) == 1
