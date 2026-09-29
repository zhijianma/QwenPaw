# -*- coding: utf-8 -*-
"""Artifact registry, verification projection, and completion gate."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from ..kernel.events import ExecutionEvent
from ..kernel.models import (
    ArtifactRecord,
    ArtifactRef,
    ArtifactStatus,
    EvidenceRef,
    EvidenceRecord,
    ExecutionContract,
    ResultPackage,
    Run,
    RunnerSignal,
    Task,
    VerificationResult,
    VerificationRecord,
    VerificationPolicy,
    VerificationStatus,
)

_EVENT_PAGE_SIZE = 200


class TaskEventReader(Protocol):
    """Minimum event source needed by the result read model."""

    async def list_events(
        self,
        task_id: UUID,
        *,
        after_sequence: int = 0,
        limit: int = 200,
    ) -> Sequence[ExecutionEvent]:
        """Return ordered execution events."""


class ResultProjectionIntegrityError(ValueError):
    """Raised when result references do not belong to the Task ledger."""


class CompletionRequirementsError(ValueError):
    """Raised when a Task cannot truthfully claim successful completion."""

    def __init__(self, codes: Sequence[str]) -> None:
        self.codes = tuple(dict.fromkeys(codes))
        super().__init__(",".join(self.codes))


@dataclass(frozen=True, slots=True)
class TaskResultProjection:
    """Derived result facts at one execution-event watermark."""

    artifacts: tuple[ArtifactRecord, ...]
    evidence: tuple[EvidenceRef, ...]
    verifications: tuple[VerificationResult, ...]
    evidence_records: tuple[EvidenceRecord, ...] = ()
    verification_records: tuple[VerificationRecord, ...] = ()
    met_exit_condition_ids: frozenset[str] = frozenset()

    def package(self, task_id: UUID, run_id: UUID) -> ResultPackage:
        """Assemble references for one run without copying artifact bytes."""
        ready = tuple(
            record.artifact
            for record in self.artifacts
            if record.status is ArtifactStatus.READY
        )
        artifact_ids = {artifact.artifact_id for artifact in ready}
        evidence = tuple(
            item for item in self.evidence if item.artifact_id in artifact_ids
        )
        verifications = tuple(
            item for item in self.verifications if item.run_id == run_id
        )
        return ResultPackage(
            task_id=task_id,
            run_id=run_id,
            artifacts=ready,
            evidence=evidence,
            verifications=verifications,
        )


def _artifact_version(
    record_versions: dict[str, int],
    artifact_kind: str,
    raw_version: object,
) -> int:
    inferred = record_versions.get(artifact_kind, 0) + 1
    if isinstance(raw_version, bool) or not isinstance(raw_version, int):
        version = inferred
    else:
        version = raw_version
        if version < 1:
            raise ResultProjectionIntegrityError(
                "artifact version must be positive",
            )
    record_versions[artifact_kind] = max(inferred, version)
    return version


def _artifact_status(raw_status: object) -> ArtifactStatus:
    if raw_status is None:
        return ArtifactStatus.READY
    try:
        return ArtifactStatus(str(raw_status))
    except ValueError as exc:
        raise ResultProjectionIntegrityError(
            "artifact status is invalid",
        ) from exc


def _optional_uuid(raw_value: object, field_name: str) -> UUID | None:
    if raw_value is None or raw_value == "":
        return None
    try:
        return UUID(str(raw_value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ResultProjectionIntegrityError(
            f"{field_name} is not a UUID",
        ) from exc


def _append_artifact_record(
    *,
    task_id: UUID,
    event: ExecutionEvent,
    artifact: ArtifactRef,
    records: list[ArtifactRecord],
    record_indexes: dict[UUID, int],
    versions: dict[str, int],
    used_versions: dict[str, set[int]],
) -> None:
    """Add one immutable ArtifactRef to its task-owned registry."""
    existing_index = record_indexes.get(artifact.artifact_id)
    if existing_index is not None:
        if records[existing_index].artifact != artifact:
            raise ResultProjectionIntegrityError(
                "artifact identity has conflicting values",
            )
        return
    assert event.run_id is not None
    supersedes_id = _optional_uuid(
        artifact.metadata.get("supersedes_artifact_id"),
        "supersedes_artifact_id",
    )
    if supersedes_id is not None:
        prior_index = record_indexes.get(supersedes_id)
        if prior_index is None:
            raise ResultProjectionIntegrityError(
                "artifact supersedes an unknown artifact",
            )
        prior = records[prior_index]
        if prior.artifact.kind != artifact.kind:
            raise ResultProjectionIntegrityError(
                "artifact cannot supersede another kind",
            )
        records[prior_index] = prior.model_copy(
            update={"status": ArtifactStatus.SUPERSEDED},
        )
    version = _artifact_version(
        versions,
        artifact.kind,
        artifact.metadata.get("version"),
    )
    kind_versions = used_versions.setdefault(artifact.kind, set())
    if version in kind_versions:
        raise ResultProjectionIntegrityError(
            "artifact version is duplicated within its kind",
        )
    kind_versions.add(version)
    record = ArtifactRecord(
        artifact=artifact,
        task_id=task_id,
        run_id=event.run_id,
        event_id=event.event_id,
        step_id=event.step_id,
        cause_event_id=event.cause_event_id,
        correlation_id=event.correlation_id,
        version=version,
        status=_artifact_status(artifact.metadata.get("status")),
        supersedes_artifact_id=supersedes_id,
        producer=event.source or "qwenpaw.system.unknown",
        created_at=event.occurred_at,
    )
    record_indexes[artifact.artifact_id] = len(records)
    records.append(record)


def _verification_from_event(
    task_id: UUID,
    event: ExecutionEvent,
) -> VerificationResult | None:
    """Parse one verifier event and enforce event ownership."""
    if event.event_type != "verification.completed":
        return None
    raw_result = event.payload.get("result")
    if not isinstance(raw_result, dict):
        raise ResultProjectionIntegrityError(
            "verification event is missing its result",
        )
    result = VerificationResult.model_validate(raw_result)
    if result.task_id != task_id or result.run_id != event.run_id:
        raise ResultProjectionIntegrityError(
            "verification result ownership does not match event",
        )
    return result


def _validate_result_references(
    record_indexes: dict[UUID, int],
    evidence_by_id: dict[UUID, EvidenceRef],
    verifications: Sequence[VerificationResult],
) -> None:
    """Reject references that do not resolve inside the Task ledger."""
    artifact_ids = set(record_indexes)
    for evidence in evidence_by_id.values():
        if evidence.artifact_id not in artifact_ids:
            raise ResultProjectionIntegrityError(
                "evidence references an unknown artifact",
            )
    evidence_ids = set(evidence_by_id)
    for result in verifications:
        if not set(result.artifact_ids) <= artifact_ids:
            raise ResultProjectionIntegrityError(
                "verification references an unknown artifact",
            )
        if not set(result.evidence_ids) <= evidence_ids:
            raise ResultProjectionIntegrityError(
                "verification references unknown evidence",
            )
        if any(
            not set(acceptance.evidence_ids) <= evidence_ids
            for acceptance in result.acceptance
        ):
            raise ResultProjectionIntegrityError(
                "acceptance references unknown evidence",
            )


def project_task_results(
    task_id: UUID,
    events: Sequence[ExecutionEvent],
) -> TaskResultProjection:
    """Build one strict result projection from canonical event references."""
    records: list[ArtifactRecord] = []
    record_indexes: dict[UUID, int] = {}
    versions: dict[str, int] = {}
    used_versions: dict[str, set[int]] = {}
    evidence_by_id: dict[UUID, EvidenceRef] = {}
    verifications: list[VerificationResult] = []
    evidence_records: list[EvidenceRecord] = []
    verification_records: list[VerificationRecord] = []
    met_exit_condition_ids: set[str] = set()

    for event in events:
        if event.task_id != task_id:
            raise ResultProjectionIntegrityError(
                "result event belongs to another task",
            )
        if event.run_id is None and (
            event.artifact_refs or event.evidence_refs
        ):
            raise ResultProjectionIntegrityError(
                "task-level event cannot own run result references",
            )
        for artifact in event.artifact_refs:
            _append_artifact_record(
                task_id=task_id,
                event=event,
                artifact=artifact,
                records=records,
                record_indexes=record_indexes,
                versions=versions,
                used_versions=used_versions,
            )
        for evidence in event.evidence_refs:
            existing = evidence_by_id.get(evidence.evidence_id)
            if existing is not None and existing != evidence:
                raise ResultProjectionIntegrityError(
                    "evidence identity has conflicting values",
                )
            evidence_by_id[evidence.evidence_id] = evidence
            if existing is None:
                assert event.run_id is not None
                evidence_records.append(
                    EvidenceRecord(
                        evidence=evidence,
                        task_id=task_id,
                        run_id=event.run_id,
                        event_id=event.event_id,
                        step_id=event.step_id,
                        cause_event_id=event.cause_event_id,
                        correlation_id=event.correlation_id,
                        source=event.source or "qwenpaw.system.unknown",
                    ),
                )
        result = _verification_from_event(task_id, event)
        if result is not None:
            verifications.append(result)
            assert event.run_id is not None
            verification_records.append(
                VerificationRecord(
                    verification=result,
                    task_id=task_id,
                    run_id=event.run_id,
                    event_id=event.event_id,
                    step_id=event.step_id,
                    cause_event_id=event.cause_event_id,
                    correlation_id=event.correlation_id,
                    source=event.source or result.verifier_id,
                ),
            )
        if event.event_type == "exit_condition.met":
            raw_condition_id = event.payload.get("condition_id")
            if isinstance(raw_condition_id, str) and raw_condition_id:
                met_exit_condition_ids.add(raw_condition_id)
    _validate_result_references(
        record_indexes,
        evidence_by_id,
        verifications,
    )
    return TaskResultProjection(
        artifacts=tuple(records),
        evidence=tuple(evidence_by_id.values()),
        verifications=tuple(verifications),
        evidence_records=tuple(evidence_records),
        verification_records=tuple(verification_records),
        met_exit_condition_ids=frozenset(met_exit_condition_ids),
    )


async def load_task_result_projection(
    reader: TaskEventReader,
    task_id: UUID,
    *,
    through_sequence: int | None = None,
) -> TaskResultProjection:
    """Replay the complete result ledger through one optional watermark."""
    all_events: list[ExecutionEvent] = []
    after_sequence = 0
    while True:
        events = await reader.list_events(
            task_id,
            after_sequence=after_sequence,
            limit=_EVENT_PAGE_SIZE,
        )
        all_events.extend(
            event
            for event in events
            if through_sequence is None or event.sequence <= through_sequence
        )
        if len(events) < _EVENT_PAGE_SIZE:
            break
        if (
            through_sequence is not None
            and events[-1].sequence >= through_sequence
        ):
            break
        after_sequence = events[-1].sequence
    return project_task_results(task_id, all_events)


def verification_signal(
    result: VerificationResult,
    *,
    step_id: UUID | None = None,
    cause_event_id: UUID | None = None,
    correlation_id: UUID | None = None,
) -> RunnerSignal:
    """Encode one verifier result through the canonical runner boundary."""
    return RunnerSignal(
        event_type="verification.completed",
        source=result.verifier_id,
        step_id=step_id,
        cause_event_id=cause_event_id,
        correlation_id=correlation_id,
        payload={"result": result.model_dump(mode="json")},
    )


def validate_task_completion(
    task: Task,
    run: Run,
    events: Sequence[ExecutionEvent],
) -> ResultPackage:
    """Return the authoritative package or reject an unverified success."""
    projection = project_task_results(task.task_id, events)
    return validate_result_projection(task, run, projection)


def _artifact_requirement_failures(
    package: ResultPackage,
    contract: ExecutionContract,
) -> list[str]:
    """Return stable codes for missing required artifact shapes."""
    failures = []
    for requirement in contract.required_artifacts:
        matching = [
            artifact
            for artifact in package.artifacts
            if artifact.kind == requirement.kind
            and (
                not requirement.media_types
                or artifact.media_type in requirement.media_types
            )
        ]
        if len(matching) < requirement.min_count:
            failures.append(f"artifact_missing:{requirement.kind}")
    return failures


def _select_verifications(
    package: ResultPackage,
    policy: VerificationPolicy,
) -> tuple[list[VerificationResult], list[str]]:
    """Select the latest required verifier results and missing codes."""
    latest_by_verifier = {
        result.verifier_id: result for result in package.verifications
    }
    if not policy.verifier_ids:
        selected = list(package.verifications)
        failures = (
            ["verification_missing"]
            if not selected and policy.fail_on_unverified
            else []
        )
        return selected, failures

    selected = []
    failures = []
    for verifier_id in policy.verifier_ids:
        result = latest_by_verifier.get(verifier_id)
        if result is None:
            if policy.fail_on_unverified:
                failures.append(f"verifier_missing:{verifier_id}")
            continue
        selected.append(result)
        if result.status is VerificationStatus.FAILED:
            failures.append(f"verifier_failed:{verifier_id}")
    return selected, failures


def _verification_policy_failures(
    task: Task,
    selected: Sequence[VerificationResult],
    policy: VerificationPolicy,
) -> list[str]:
    """Validate acceptance coverage and required evidence bindings."""
    failures = []
    if policy.require_all_acceptance:
        passed_criteria = {
            item.criterion
            for result in selected
            for item in result.acceptance
            if item.passed
        }
        if not set(task.acceptance_criteria) <= passed_criteria:
            failures.append("acceptance_unverified")
    if policy.require_evidence:
        selected_evidence = {
            evidence_id
            for result in selected
            for evidence_id in result.evidence_ids
        }
        if not selected_evidence:
            failures.append("verification_evidence_missing")
    return failures


def _passed_acceptance_criteria(
    package: ResultPackage,
) -> set[str]:
    """Return criteria supported by a passed verification result."""
    return {
        item.criterion
        for result in package.verifications
        if result.status is VerificationStatus.PASSED
        for item in result.acceptance
        if item.passed
    }


def satisfied_result_exit_condition_ids(
    task: Task,
    projection: TaskResultProjection,
    package: ResultPackage,
    contract: ExecutionContract,
    *,
    required: bool | None = None,
) -> frozenset[str]:
    """Return result-bound conditions satisfied by durable projections."""
    satisfied = set()
    passed_criteria = _passed_acceptance_criteria(package)
    for condition in contract.exit_conditions:
        if condition.kind == "max_iterations":
            continue
        if required is not None and condition.required is not required:
            continue
        met = False
        if condition.kind == "acceptance_met":
            raw_criterion = condition.parameters.get("criterion")
            criteria = (
                {raw_criterion}
                if isinstance(raw_criterion, str)
                else set(task.acceptance_criteria)
            )
            met = bool(criteria) and criteria <= passed_criteria
        elif condition.kind == "artifact_emitted":
            raw_kind = condition.parameters.get("kind")
            met = any(
                raw_kind is None or artifact.kind == raw_kind
                for artifact in package.artifacts
            )
        elif condition.kind == "explicit_signal":
            met = condition.condition_id in (
                projection.met_exit_condition_ids
            )
        if met:
            satisfied.add(condition.condition_id)
    return frozenset(satisfied)


def _exit_condition_failures(
    task: Task,
    projection: TaskResultProjection,
    package: ResultPackage,
    contract: ExecutionContract,
) -> list[str]:
    """Evaluate required result-bound conditions before success."""
    satisfied = satisfied_result_exit_condition_ids(
        task,
        projection,
        package,
        contract,
        required=True,
    )
    failures = []
    for condition in contract.exit_conditions:
        if not condition.required or condition.kind == "max_iterations":
            continue
        if condition.condition_id not in satisfied:
            failures.append(
                f"exit_condition_unmet:{condition.condition_id}",
            )
    return failures


def validate_result_projection(
    task: Task,
    run: Run,
    projection: TaskResultProjection,
) -> ResultPackage:
    """Apply the immutable Execution Contract to a result projection."""
    package = projection.package(task.task_id, run.run_id)
    contract = task.execution_contract
    if contract is None:
        return package

    failures = _artifact_requirement_failures(package, contract)
    failures.extend(
        _exit_condition_failures(task, projection, package, contract),
    )

    policy = contract.verification_policy
    if policy is None:
        if failures:
            raise CompletionRequirementsError(failures)
        return package

    selected, selection_failures = _select_verifications(package, policy)
    failures.extend(selection_failures)
    failures.extend(_verification_policy_failures(task, selected, policy))
    if failures:
        raise CompletionRequirementsError(failures)
    return package


__all__ = [
    "CompletionRequirementsError",
    "ResultProjectionIntegrityError",
    "TaskResultProjection",
    "load_task_result_projection",
    "project_task_results",
    "satisfied_result_exit_condition_ids",
    "validate_result_projection",
    "validate_task_completion",
    "verification_signal",
]
