# -*- coding: utf-8 -*-
"""Contract tests for immutable kernel domain models."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from qwenpaw.kernel.models import (
    ActorRef,
    ActorType,
    ApprovalDecision,
    ApprovalDecisionValue,
    ApprovalRequest,
    ArtifactRef,
    AutonomyLevel,
    CapabilityBundle,
    CapabilityContribution,
    CapabilityDescriptor,
    CapabilityProviderKind,
    EvidenceRef,
    ExecutionBudget,
    ExecutionContract,
    ExitCondition,
    Plan,
    PlanStep,
    Proposal,
    RestartPolicy,
    RiskLevel,
    Run,
    RunStatus,
    Task,
    TaskOrder,
    TaskSource,
    TaskStatus,
    VerificationPolicy,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_task_is_immutable_and_round_trips_as_json() -> None:
    now = _now()
    task = Task(
        objective="Prepare the release summary",
        source=TaskSource.USER,
        agent_id="default",
        constraints=("Do not publish",),
        acceptance_criteria=("Summary is stored locally",),
        created_at=now,
        updated_at=now,
    )

    restored = Task.model_validate_json(task.model_dump_json())

    assert restored == task
    assert restored.status is TaskStatus.CREATED
    with pytest.raises(ValidationError):
        restored.status = TaskStatus.RUNNING


def test_execution_contract_round_trips_for_interactive_work() -> None:
    contract = ExecutionContract(
        goal="Prepare the release summary",
        acceptance=("Summary is stored locally",),
    )
    task = Task(
        objective=contract.goal,
        source=TaskSource.USER,
        agent_id="default",
        acceptance_criteria=contract.acceptance,
        execution_contract=contract,
    )

    restored = Task.model_validate_json(task.model_dump_json())

    assert restored.execution_contract == contract


def test_unattended_contract_fails_closed_without_reliability_fields() -> None:
    with pytest.raises(ValidationError, match="unattended execution"):
        ExecutionContract(
            goal="Publish a verified report",
            autonomy_level=AutonomyLevel.UNATTENDED,
        )


def test_complete_unattended_contract_is_valid() -> None:
    contract = ExecutionContract(
        goal="Publish a verified report",
        acceptance=("Report is published",),
        autonomy_level=AutonomyLevel.UNATTENDED,
        permission_scope="reports.publish",
        budget=ExecutionBudget(
            max_duration_seconds=600,
            max_tokens=20_000,
            max_cost_micros=500_000,
            max_tool_calls=50,
            max_retries=2,
        ),
        exit_conditions=(
            ExitCondition(
                condition_id="acceptance",
                kind="acceptance_met",
            ),
        ),
        verification_policy=VerificationPolicy(
            verifier_ids=("report.verifier",),
            require_evidence=True,
        ),
    )

    assert contract.autonomy_level is AutonomyLevel.UNATTENDED


def test_max_iterations_exit_condition_requires_positive_limit() -> None:
    with pytest.raises(ValidationError, match="positive integer"):
        ExitCondition(
            condition_id="iteration-limit",
            kind="max_iterations",
        )

    condition = ExitCondition(
        condition_id="iteration-limit",
        kind="max_iterations",
        parameters={"limit": 8},
    )

    assert condition.parameters["limit"] == 8


def test_acceptance_exit_condition_references_contract_criterion() -> None:
    with pytest.raises(ValidationError, match="must belong"):
        ExecutionContract(
            goal="Verify report",
            acceptance=("Report exists",),
            exit_conditions=(
                ExitCondition(
                    condition_id="accepted",
                    kind="acceptance_met",
                    parameters={"criterion": "Different criterion"},
                ),
            ),
        )


def test_task_rejects_execution_contract_drift() -> None:
    contract = ExecutionContract(
        goal="Prepare report",
        acceptance=("Report exists",),
    )

    with pytest.raises(ValidationError, match="goal must match"):
        Task(
            objective="Prepare another report",
            source=TaskSource.USER,
            agent_id="default",
            acceptance_criteria=contract.acceptance,
            execution_contract=contract,
        )

    with pytest.raises(ValidationError, match="acceptance must match"):
        TaskOrder(
            task_id=uuid4(),
            objective=contract.goal,
            acceptance_criteria=("Different result",),
            execution_contract=contract,
        )


def test_public_models_serialize_version_and_load_legacy_payloads() -> None:
    task = Task(
        objective="Version the public contract",
        source=TaskSource.USER,
        agent_id="default",
    )

    payload = task.model_dump(mode="json")
    assert payload["schema"] == "qwenpaw.kernel-model.v1"

    legacy_payload = dict(payload)
    legacy_payload.pop("schema")
    restored = Task.model_validate(legacy_payload)
    assert restored.schema_id == "qwenpaw.kernel-model.v1"


def test_public_models_reject_unknown_fields_and_naive_datetimes() -> None:
    now = _now()
    with pytest.raises(ValidationError):
        Task(
            objective="Inspect a workspace",
            source=TaskSource.USER,
            agent_id="default",
            created_at=now,
            updated_at=now,
            unexpected=True,
        )

    with pytest.raises(ValidationError):
        Task(
            objective="Inspect a workspace",
            source=TaskSource.USER,
            agent_id="default",
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )


def test_task_rejects_updated_at_before_created_at() -> None:
    now = _now()
    with pytest.raises(ValidationError, match="updated_at"):
        Task(
            objective="Inspect a workspace",
            source=TaskSource.USER,
            agent_id="default",
            created_at=now,
            updated_at=now - timedelta(seconds=1),
        )


def test_plan_requires_unique_steps_and_existing_dependencies() -> None:
    task_id = uuid4()
    first_id = uuid4()
    second_id = uuid4()
    plan = Plan(
        task_id=task_id,
        revision=1,
        steps=(
            PlanStep(
                step_id=first_id,
                title="Inspect",
                objective="Inspect the target",
            ),
            PlanStep(
                step_id=second_id,
                title="Summarize",
                objective="Summarize the evidence",
                depends_on=(first_id,),
            ),
        ),
    )

    assert plan.steps[1].depends_on == (first_id,)

    with pytest.raises(ValidationError, match="unique"):
        Plan(
            task_id=task_id,
            revision=1,
            steps=(
                PlanStep(
                    step_id=first_id,
                    title="One",
                    objective="First",
                ),
                PlanStep(
                    step_id=first_id,
                    title="Two",
                    objective="Second",
                ),
            ),
        )

    with pytest.raises(ValidationError, match="unknown step"):
        Plan(
            task_id=task_id,
            revision=1,
            steps=(
                PlanStep(
                    title="Broken",
                    objective="Reference a missing dependency",
                    depends_on=(uuid4(),),
                ),
            ),
        )


def test_plan_rejects_dependency_cycles() -> None:
    first_id = uuid4()
    second_id = uuid4()

    with pytest.raises(ValidationError, match="cycle"):
        Plan(
            task_id=uuid4(),
            revision=1,
            steps=(
                PlanStep(
                    step_id=first_id,
                    title="First",
                    objective="First step",
                    depends_on=(second_id,),
                ),
                PlanStep(
                    step_id=second_id,
                    title="Second",
                    objective="Second step",
                    depends_on=(first_id,),
                ),
            ),
        )


def test_run_requires_consistent_timestamps() -> None:
    now = _now()
    with pytest.raises(ValidationError, match="finished_at"):
        Run(
            task_id=uuid4(),
            attempt=1,
            status=RunStatus.SUCCEEDED,
            registry_generation=1,
            runner_id="local",
            started_at=now,
            finished_at=now - timedelta(seconds=1),
        )


def test_artifact_evidence_and_capability_contracts() -> None:
    artifact = ArtifactRef(
        kind="report",
        uri="qwenpaw://artifacts/report.md",
        media_type="text/markdown",
        content_hash=f"sha256:{'a' * 64}",
        size_bytes=42,
    )
    evidence = EvidenceRef(
        artifact_id=artifact.artifact_id,
        claim="The report was generated",
        producer="local-runner",
        captured_at=_now(),
    )
    capability = CapabilityDescriptor(
        capability_id="runner.local",
        slot="runner",
        provider_plugin_id="qwenpaw.core",
        version="1.0.0",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        restart_policy=RestartPolicy.HOT,
    )

    assert evidence.artifact_id == artifact.artifact_id
    assert capability.slot == "runner"
    assert capability.provider_id == "qwenpaw.core"
    assert capability.provider_plugin_id == "qwenpaw.core"
    assert capability.provider_kind is CapabilityProviderKind.PLUGIN


def test_capability_bundle_supports_system_and_plugin_providers() -> None:
    bundle = CapabilityBundle(
        provider_id="qwenpaw.system",
        provider_kind=CapabilityProviderKind.SYSTEM,
        version="1.0.0",
        contributions=(
            CapabilityContribution(
                contribution_id="console-agent",
                slot="runner",
                entrypoint="qwenpaw.tasks.system:ConsoleAgentRunner",
            ),
        ),
    )

    restored = CapabilityBundle.model_validate_json(
        bundle.model_dump_json(),
    )

    assert restored == bundle
    assert restored.contributions[0].slot == "runner"

    with pytest.raises(ValidationError, match="unique per provider"):
        CapabilityBundle(
            provider_id="qwenpaw.system",
            provider_kind=CapabilityProviderKind.SYSTEM,
            version="1.0.0",
            contributions=(
                bundle.contributions[0],
                bundle.contributions[0],
            ),
        )


def test_proposal_approval_and_task_order_are_separate_contracts() -> None:
    actor = ActorRef(type=ActorType.USER, id="local-user")
    proposal = Proposal(
        source="sensor.calendar",
        objective="Resolve a calendar conflict",
        rationale_summary="Two accepted events overlap",
        risk=RiskLevel.MEDIUM,
        requested_capabilities=("calendar.write",),
    )
    task_id = uuid4()
    approval = ApprovalRequest(
        task_id=task_id,
        action="calendar.reschedule",
        risk=proposal.risk,
        requester=actor,
        policy="strict",
        redacted_arguments={"event_id": "[REDACTED]"},
        created_at=_now(),
    )
    decision = ApprovalDecision(
        approval_id=approval.approval_id,
        decision=ApprovalDecisionValue.APPROVED,
        actor=actor,
        scope="exact",
        reason="Approved in task workbench",
        decided_at=_now(),
    )
    order = TaskOrder(
        task_id=task_id,
        objective=proposal.objective,
        constraints=("Use the approved event only",),
        requested_capabilities=proposal.requested_capabilities,
        approval_ids=(decision.approval_id,),
    )

    assert proposal.proposal_id != approval.approval_id
    assert order.approval_ids == (approval.approval_id,)
