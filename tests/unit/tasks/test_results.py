# -*- coding: utf-8 -*-
"""Tests for artifact projection and truthful Task completion."""

from uuid import uuid4

import pytest

from qwenpaw.kernel.events import ExecutionEvent
from qwenpaw.kernel.models import (
    AcceptanceVerification,
    ActorRef,
    ActorType,
    ArtifactRef,
    ArtifactRequirement,
    ArtifactStatus,
    EvidenceRef,
    ExecutionContract,
    ExitCondition,
    Run,
    Task,
    TaskSource,
    VerificationPolicy,
    VerificationResult,
    VerificationStatus,
)
from qwenpaw.tasks.results import (
    CompletionRequirementsError,
    ResultProjectionIntegrityError,
    load_task_result_projection,
    project_task_results,
    validate_task_completion,
    verification_signal,
)


def _artifact(
    name: str,
    *,
    metadata: dict | None = None,
) -> ArtifactRef:
    return ArtifactRef(
        kind="document.readme_it",
        uri=f"qwenpaw-artifact://sha256/{name}",
        media_type="text/markdown",
        content_hash=f"sha256:{'0' * 64}",
        size_bytes=12,
        metadata=metadata or {},
    )


def _event(
    task_id,
    run_id,
    sequence: int,
    *,
    artifacts: tuple[ArtifactRef, ...] = (),
    evidence: tuple[EvidenceRef, ...] = (),
    result: VerificationResult | None = None,
    event_type: str | None = None,
    payload: dict | None = None,
    step_id=None,
    cause_event_id=None,
    correlation_id=None,
    source: str = "runner.tests",
) -> ExecutionEvent:
    return ExecutionEvent(
        task_id=task_id,
        run_id=run_id,
        sequence=sequence,
        event_type=(
            event_type
            or ("verification.completed" if result else "artifact.created")
        ),
        registry_generation=1,
        actor=ActorRef(type=ActorType.RUNNER, id="runner.tests"),
        step_id=step_id,
        cause_event_id=cause_event_id,
        correlation_id=correlation_id,
        source=source,
        payload=(
            payload
            if payload is not None
            else (
                {"result": result.model_dump(mode="json")}
                if result
                else {}
            )
        ),
        artifact_refs=artifacts,
        evidence_refs=evidence,
    )


def test_projection_tracks_versions_and_explicit_supersession() -> None:
    task_id = uuid4()
    run_id = uuid4()
    first = _artifact("first")
    second = _artifact(
        "second",
        metadata={"supersedes_artifact_id": str(first.artifact_id)},
    )

    projection = project_task_results(
        task_id,
        (
            _event(task_id, run_id, 1, artifacts=(first,)),
            _event(task_id, run_id, 2, artifacts=(second,)),
        ),
    )

    assert [record.version for record in projection.artifacts] == [1, 2]
    assert [record.status for record in projection.artifacts] == [
        ArtifactStatus.SUPERSEDED,
        ArtifactStatus.READY,
    ]
    assert projection.package(task_id, run_id).artifacts == (second,)


def test_projection_preserves_host_owned_result_provenance() -> None:
    task_id = uuid4()
    run_id = uuid4()
    step_id = uuid4()
    correlation_id = uuid4()
    artifact = _artifact("causal")
    evidence = EvidenceRef(
        artifact_id=artifact.artifact_id,
        claim="Causal evidence",
        producer="runner.tests",
    )
    artifact_event = _event(
        task_id,
        run_id,
        1,
        artifacts=(artifact,),
        evidence=(evidence,),
        step_id=step_id,
        correlation_id=correlation_id,
    )
    verification = VerificationResult(
        task_id=task_id,
        run_id=run_id,
        verifier_id="verifier.tests",
        status=VerificationStatus.PASSED,
        artifact_ids=(artifact.artifact_id,),
        evidence_ids=(evidence.evidence_id,),
    )
    verification_event = _event(
        task_id,
        run_id,
        2,
        result=verification,
        cause_event_id=artifact_event.event_id,
        correlation_id=correlation_id,
        source="verifier.tests",
    )

    projection = project_task_results(
        task_id,
        (artifact_event, verification_event),
    )

    artifact_record = projection.artifacts[0]
    assert artifact_record.event_id == artifact_event.event_id
    assert artifact_record.step_id == step_id
    assert artifact_record.correlation_id == correlation_id
    evidence_record = projection.evidence_records[0]
    assert evidence_record.event_id == artifact_event.event_id
    assert evidence_record.evidence == evidence
    verification_record = projection.verification_records[0]
    assert verification_record.event_id == verification_event.event_id
    assert verification_record.cause_event_id == artifact_event.event_id
    assert verification_record.correlation_id == correlation_id


def test_projection_rejects_unknown_superseded_artifact() -> None:
    task_id = uuid4()
    run_id = uuid4()
    artifact = _artifact(
        "orphan",
        metadata={"supersedes_artifact_id": str(uuid4())},
    )

    with pytest.raises(
        ResultProjectionIntegrityError,
        match="unknown artifact",
    ):
        project_task_results(
            task_id,
            (_event(task_id, run_id, 1, artifacts=(artifact,)),),
        )


def test_completion_requires_artifact_verification_and_evidence() -> None:
    task_id = uuid4()
    run_id = uuid4()
    criterion = "README installation is translated to Italian"
    contract = ExecutionContract(
        goal="Translate README installation",
        acceptance=(criterion,),
        required_artifacts=(
            ArtifactRequirement(
                kind="document.readme_it",
                media_types=("text/markdown",),
            ),
        ),
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
    )
    run = Run(
        run_id=run_id,
        task_id=task_id,
        attempt=1,
        registry_generation=1,
        runner_id="runner.tests",
    )
    artifact = _artifact("translated")
    evidence = EvidenceRef(
        artifact_id=artifact.artifact_id,
        claim="The translated installation section exists",
        producer="verifier.tests",
    )
    result = VerificationResult(
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
        _event(task_id, run_id, 2, result=result),
    )

    package = validate_task_completion(task, run, events)

    assert package.artifacts == (artifact,)
    assert package.evidence == (evidence,)
    assert package.verifications == (result,)
    assert verification_signal(result).source == "verifier.tests"


def test_completion_rejects_missing_required_result() -> None:
    task_id = uuid4()
    contract = ExecutionContract(
        goal="Translate README installation",
        required_artifacts=(ArtifactRequirement(kind="document.readme_it"),),
    )
    task = Task(
        task_id=task_id,
        objective=contract.goal,
        execution_contract=contract,
        source=TaskSource.USER,
        agent_id="default",
    )
    run = Run(
        task_id=task_id,
        attempt=1,
        registry_generation=1,
        runner_id="runner.tests",
    )

    with pytest.raises(
        CompletionRequirementsError,
        match="artifact_missing:document.readme_it",
    ):
        validate_task_completion(task, run, ())


def test_completion_requires_acceptance_exit_condition() -> None:
    task_id = uuid4()
    run_id = uuid4()
    criterion = "Report is verified"
    contract = ExecutionContract(
        goal="Prepare report",
        acceptance=(criterion,),
        exit_conditions=(
            ExitCondition(
                condition_id="report-accepted",
                kind="acceptance_met",
            ),
        ),
    )
    task = Task(
        task_id=task_id,
        objective=contract.goal,
        acceptance_criteria=contract.acceptance,
        execution_contract=contract,
        source=TaskSource.USER,
        agent_id="default",
    )
    run = Run(
        run_id=run_id,
        task_id=task_id,
        attempt=1,
        registry_generation=1,
        runner_id="runner.tests",
    )

    with pytest.raises(
        CompletionRequirementsError,
        match="exit_condition_unmet:report-accepted",
    ):
        validate_task_completion(task, run, ())


def test_completion_accepts_artifact_and_explicit_exit_conditions() -> None:
    task_id = uuid4()
    run_id = uuid4()
    contract = ExecutionContract(
        goal="Produce report",
        exit_conditions=(
            ExitCondition(
                condition_id="report-created",
                kind="artifact_emitted",
                parameters={"kind": "document.readme_it"},
            ),
            ExitCondition(
                condition_id="runner-finished",
                kind="explicit_signal",
            ),
        ),
    )
    task = Task(
        task_id=task_id,
        objective=contract.goal,
        execution_contract=contract,
        source=TaskSource.USER,
        agent_id="default",
    )
    run = Run(
        run_id=run_id,
        task_id=task_id,
        attempt=1,
        registry_generation=1,
        runner_id="runner.tests",
    )
    events = (
        _event(task_id, run_id, 1, artifacts=(_artifact("report"),)),
        _event(
            task_id,
            run_id,
            2,
            event_type="exit_condition.met",
            payload={"condition_id": "runner-finished"},
        ),
    )

    package = validate_task_completion(task, run, events)

    assert len(package.artifacts) == 1


@pytest.mark.asyncio
async def test_result_projection_pages_beyond_default_event_window() -> None:
    task_id = uuid4()
    run_id = uuid4()
    artifact = _artifact("late-result")
    events = tuple(
        _event(
            task_id,
            run_id,
            sequence,
            artifacts=(artifact,) if sequence == 201 else (),
        )
        for sequence in range(1, 202)
    )

    class Reader:
        async def list_events(
            self,
            requested_task_id,
            *,
            after_sequence=0,
            limit=200,
        ):
            assert requested_task_id == task_id
            return tuple(
                event for event in events if event.sequence > after_sequence
            )[:limit]

    projection = await load_task_result_projection(Reader(), task_id)

    assert projection.artifacts[0].artifact == artifact
