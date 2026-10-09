# -*- coding: utf-8 -*-
"""Semantic runtime projections over durable Agent OS source facts."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from ..kernel import (
    ActionRecord,
    ActionStatus,
    ActionStore,
    ArtifactRecord,
    ArtifactStatus,
    ControlCommandKind,
    ControlCommandStatus,
    ControlHistoryPort,
    ControlRecord,
    CompactionRecord,
    CompactionStatus,
    CompactionStore,
    ConversationArtifactHistoryPort,
    ConversationArtifactRecord,
    ConversationTaskResultRecords,
    InteractionHistoryPort,
    InteractionRecord,
    InteractionStatus,
    ModelCallRecord,
    ModelCallStatus,
    ModelCallStore,
    ModelRecoveryHistoryPort,
    ModelResourceWait,
    ModelStepContinuation,
    ModelStepContinuationStatus,
    ObservationCategory,
    ObservationPage,
    ObservationProjectionPort,
    ObservationSource,
    ObservationStage,
    ObservationStatus,
    RuntimeObservation,
    ResourceWaitStatus,
    SubmissionHistoryPort,
    SubmissionStatus,
    TERMINAL_SUBMISSION_STATUSES,
    TurnSubmission,
    TaskResultHistoryPort,
    EvidenceRecord,
    VerificationHistoryPort,
    VerificationRecord,
    VerificationStatus,
)
from .actions import lite_action_store
from .model_calls import lite_model_call_store
from .observation_index import LiteObservationIndex


def _observation_id(source_id: UUID, stage: ObservationStage) -> UUID:
    return uuid5(source_id, f"runtime-observation:{stage.value}")


def _task_result_observation_id(
    event_id: UUID,
    result_id: UUID,
    category: ObservationCategory,
) -> UUID:
    return uuid5(
        event_id,
        f"runtime-observation:{category.value}:{result_id}",
    )


def _optional_uuid(value: UUID | None) -> str | None:
    return str(value) if value is not None else None


def _result_status(status: ModelCallStatus) -> ObservationStatus:
    return {
        ModelCallStatus.SUCCEEDED: ObservationStatus.SUCCEEDED,
        ModelCallStatus.FAILED: ObservationStatus.FAILED,
        ModelCallStatus.CANCELLED: ObservationStatus.CANCELLED,
    }[status]


def _action_status(status: ActionStatus) -> ObservationStatus:
    return {
        ActionStatus.SUCCEEDED: ObservationStatus.SUCCEEDED,
        ActionStatus.FAILED: ObservationStatus.FAILED,
        ActionStatus.PARTIAL: ObservationStatus.PARTIAL,
        ActionStatus.UNKNOWN: ObservationStatus.UNKNOWN,
        ActionStatus.CANCELLED: ObservationStatus.CANCELLED,
        ActionStatus.DENIED: ObservationStatus.DENIED,
    }[status]


def _interaction_status(status: InteractionStatus) -> ObservationStatus:
    return {
        InteractionStatus.RESOLVED: ObservationStatus.RESOLVED,
        InteractionStatus.EXPIRED: ObservationStatus.EXPIRED,
        InteractionStatus.CANCELLED: ObservationStatus.CANCELLED,
    }[status]


def _control_status(status: ControlCommandStatus) -> ObservationStatus:
    return {
        ControlCommandStatus.ACCEPTED: ObservationStatus.ACCEPTED,
        ControlCommandStatus.APPLIED: ObservationStatus.APPLIED,
        ControlCommandStatus.REJECTED: ObservationStatus.REJECTED,
        ControlCommandStatus.CONFLICT: ObservationStatus.CONFLICT,
    }[status]


def _control_category(
    kind: ControlCommandKind,
) -> ObservationCategory:
    if kind in {
        ControlCommandKind.INTERRUPT_CURRENT,
        ControlCommandKind.STOP_AND_CLEAR,
    }:
        return ObservationCategory.INTERRUPT
    return ObservationCategory.CONTROL


def _compaction_status(status: CompactionStatus) -> ObservationStatus:
    return {
        CompactionStatus.SUCCEEDED: ObservationStatus.SUCCEEDED,
        CompactionStatus.FAILED: ObservationStatus.FAILED,
    }[status]


def _verification_status(status: VerificationStatus) -> ObservationStatus:
    return {
        VerificationStatus.PASSED: ObservationStatus.SUCCEEDED,
        VerificationStatus.FAILED: ObservationStatus.FAILED,
    }[status]


def _artifact_status(status: ArtifactStatus) -> ObservationStatus:
    return {
        ArtifactStatus.DRAFT: ObservationStatus.RECORDED,
        ArtifactStatus.READY: ObservationStatus.SUCCEEDED,
        ArtifactStatus.SUPERSEDED: ObservationStatus.RECORDED,
        ArtifactStatus.REJECTED: ObservationStatus.REJECTED,
    }[status]


def _model_observations(
    record: ModelCallRecord,
) -> tuple[RuntimeObservation, ...]:
    route = record.route
    attempt = record.attempt
    common = {
        "category": ObservationCategory.MODEL,
        "chat_id": attempt.chat_id,
        "invocation_id": attempt.invocation_id,
        "correlation_id": attempt.correlation_id,
        "registry_generation": attempt.registry_generation,
    }
    if attempt.conversation_id is None:
        return ()
    route_observation = RuntimeObservation(
        observation_id=_observation_id(
            route.route_decision_id,
            ObservationStage.POLICY,
        ),
        stage=ObservationStage.POLICY,
        status=ObservationStatus.RECORDED,
        source=ObservationSource(
            source_type="qwenpaw.model.route-decision",
            source_id=str(route.route_decision_id),
        ),
        title="Model route selected",
        facts={
            "attempt_id": str(route.attempt_id),
            "context_manifest_id": str(route.context_manifest_id),
            "model_call_index": route.model_call_index,
            "attempt_index": route.attempt_index,
            "requested_provider_id": route.requested_provider_id,
            "requested_model_id": route.requested_model_id,
            "provider_id": route.provider_id,
            "model_id": route.model_id,
            "reason": route.reason.value,
            "previous_attempt_id": _optional_uuid(
                route.previous_attempt_id,
            ),
            "policy_id": route.policy_id,
            "policy_version": route.policy_version,
        },
        occurred_at=route.decided_at,
        **common,
    )
    attempt_observation = RuntimeObservation(
        observation_id=_observation_id(
            attempt.attempt_id,
            ObservationStage.EXECUTION,
        ),
        stage=ObservationStage.EXECUTION,
        status=ObservationStatus.STARTED,
        source=ObservationSource(
            source_type="qwenpaw.model.attempt",
            source_id=str(attempt.attempt_id),
        ),
        title="Model attempt started",
        facts={
            "attempt_id": str(attempt.attempt_id),
            "model_call_index": attempt.model_call_index,
            "attempt_index": attempt.attempt_index,
            "provider_id": attempt.provider_id,
            "model_id": attempt.model_id,
            "transport_protocol": (attempt.transport_contract.protocol.value),
            "stream_resume_mode": (
                attempt.transport_contract.resume_mode.value
            ),
            "route_affinity": (
                attempt.transport_contract.route_affinity.value
            ),
            "transport_fallback": (attempt.transport_contract.fallback.value),
            "context_window_tokens": attempt.context_window_tokens,
            "compaction_threshold": attempt.compaction_threshold,
            "adapter_id": attempt.adapter_id,
            "adapter_version": attempt.adapter_version,
            "formatter_id": attempt.formatter_id,
            "formatter_version": attempt.formatter_version,
        },
        occurred_at=attempt.started_at,
        **common,
    )
    if record.result is None:
        return route_observation, attempt_observation
    result = record.result
    result_observation = RuntimeObservation(
        observation_id=_observation_id(
            result.attempt_id,
            ObservationStage.EVIDENCE,
        ),
        stage=ObservationStage.EVIDENCE,
        status=_result_status(result.status),
        source=ObservationSource(
            source_type="qwenpaw.model.result",
            source_id=str(result.attempt_id),
        ),
        title="Model request completed",
        facts={
            "attempt_id": str(result.attempt_id),
            "error_kind": result.error_kind,
            "retryable": result.retryable,
            "emitted_content": result.emitted_content,
            "output_boundary": (
                result.output_boundary.value
                if result.output_boundary is not None
                else None
            ),
            "failure_class": (
                result.failure_class.value
                if result.failure_class is not None
                else None
            ),
            "recovery_disposition": (
                result.recovery_disposition.value
                if result.recovery_disposition is not None
                else None
            ),
            "transport_recovery_mode": (
                result.transport_recovery_mode.value
                if result.transport_recovery_mode is not None
                else None
            ),
            "transport_validation_reason": (
                result.transport_validation_reason.value
                if result.transport_validation_reason is not None
                else None
            ),
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "usage_measurement": result.usage_measurement,
            "cache_read_tokens": result.cache_read_tokens,
            "cache_write_tokens": result.cache_write_tokens,
            "cache_eligible_input_tokens": (
                result.cache_eligible_input_tokens
            ),
            "cache_observed": result.cache_observed,
            "cost_micros": result.cost_micros,
            "cost_unknown": result.cost_unknown,
        },
        occurred_at=result.completed_at,
        **common,
    )
    return route_observation, attempt_observation, result_observation


def _model_step_status(
    status: ModelStepContinuationStatus,
) -> ObservationStatus:
    return {
        ModelStepContinuationStatus.READY: ObservationStatus.PENDING,
        ModelStepContinuationStatus.DISPATCHED: ObservationStatus.ACCEPTED,
        ModelStepContinuationStatus.CANCELLED: ObservationStatus.CANCELLED,
        ModelStepContinuationStatus.ACTION_RECONCILIATION_REQUIRED: (
            ObservationStatus.BLOCKED
        ),
        ModelStepContinuationStatus.RECOVERY_EXHAUSTED: (
            ObservationStatus.FAILED
        ),
    }[status]


def _model_step_observation(
    continuation: ModelStepContinuation,
) -> RuntimeObservation:
    return RuntimeObservation(
        observation_id=_observation_id(
            continuation.continuation_id,
            ObservationStage.EXECUTION,
        ),
        category=ObservationCategory.MODEL,
        stage=ObservationStage.EXECUTION,
        status=_model_step_status(continuation.status),
        source=ObservationSource(
            source_type="qwenpaw.model.step-continuation",
            source_id=str(continuation.continuation_id),
        ),
        chat_id=continuation.conversation_id,
        invocation_id=continuation.invocation_id,
        correlation_id=continuation.correlation_id,
        title="Partial model step recovery",
        facts={
            "continuation_id": str(continuation.continuation_id),
            "attempt_id": str(continuation.attempt_id),
            "output_boundary": continuation.output_boundary.value,
            "recovery_status": continuation.status.value,
            "reconciliation_reason": (
                continuation.reconciliation.reason.value
                if continuation.reconciliation is not None
                else None
            ),
            "reconciliation_action_count": (
                continuation.reconciliation.action_count
                if continuation.reconciliation is not None
                else 0
            ),
            "reconciliation_pending_result_count": (
                continuation.reconciliation.pending_result_count
                if continuation.reconciliation is not None
                else 0
            ),
            "reconciliation_uncertain_side_effect_count": (
                continuation.reconciliation.uncertain_side_effect_count
                if continuation.reconciliation is not None
                else 0
            ),
            "reconciliation_terminal_result_count": (
                continuation.reconciliation.terminal_result_count
                if continuation.reconciliation is not None
                else 0
            ),
            "context_checkpoint_id": (
                str(continuation.context_checkpoint.checkpoint_id)
                if continuation.context_checkpoint is not None
                else None
            ),
            "context_checkpoint_action_count": (
                continuation.context_checkpoint.action_count
                if continuation.context_checkpoint is not None
                else 0
            ),
            "submission_id": _optional_uuid(continuation.submission_id),
            "revision": continuation.revision,
        },
        occurred_at=continuation.updated_at,
    )


def _model_resource_wait_status(
    status: ResourceWaitStatus,
) -> ObservationStatus:
    return {
        ResourceWaitStatus.WAITING: ObservationStatus.PENDING,
        ResourceWaitStatus.READY: ObservationStatus.RUNNING,
        ResourceWaitStatus.DISPATCHED: ObservationStatus.ACCEPTED,
        ResourceWaitStatus.CANCELLED: ObservationStatus.CANCELLED,
        ResourceWaitStatus.RECOVERY_EXHAUSTED: ObservationStatus.FAILED,
    }[status]


def _model_resource_wait_observation(
    wait: ModelResourceWait,
) -> RuntimeObservation:
    return RuntimeObservation(
        observation_id=_observation_id(
            wait.wait_id,
            ObservationStage.EXECUTION,
        ),
        category=ObservationCategory.MODEL,
        stage=ObservationStage.EXECUTION,
        status=_model_resource_wait_status(wait.status),
        source=ObservationSource(
            source_type="qwenpaw.model.resource-wait",
            source_id=str(wait.wait_id),
        ),
        chat_id=wait.conversation_id,
        invocation_id=wait.invocation_id,
        correlation_id=wait.correlation_id,
        title="Model resource recovery",
        facts={
            "wait_id": str(wait.wait_id),
            "attempt_id": str(wait.attempt_id),
            "provider_id": wait.provider_id,
            "model_id": wait.model_id,
            "failure_class": wait.failure_class.value,
            "trigger": wait.trigger.value,
            "recovery_status": wait.status.value,
            "not_before": (
                wait.not_before.isoformat()
                if wait.not_before is not None
                else None
            ),
            "submission_id": _optional_uuid(wait.submission_id),
            "revision": wait.revision,
        },
        occurred_at=wait.updated_at,
    )


def _action_observations(
    record: ActionRecord,
) -> tuple[RuntimeObservation, ...]:
    request = record.request
    if request.conversation_id is None:
        return ()
    common = {
        "category": ObservationCategory.ACTION,
        "chat_id": request.chat_id,
        "invocation_id": request.invocation_id,
        "correlation_id": request.correlation_id,
        "registry_generation": request.registry_generation,
    }
    intent = RuntimeObservation(
        observation_id=_observation_id(
            request.action_id,
            ObservationStage.INTENT,
        ),
        stage=ObservationStage.INTENT,
        status=ObservationStatus.RECORDED,
        source=ObservationSource(
            source_type="qwenpaw.action.request",
            source_id=str(request.action_id),
        ),
        title="Action requested",
        facts={
            "action_id": str(request.action_id),
            "capability_id": request.capability_id,
            "kind": request.kind.value,
            "action_name": request.action_name,
            "effect": request.effect.value,
            "risk": request.risk.value,
            "reversible": request.reversible,
            "idempotency_mode": request.idempotency_mode.value,
            "attempt": request.attempt,
            "retry_max_attempts": request.retry_policy.max_attempts,
            "retry_root_action_id": _optional_uuid(
                request.retry_root_action_id,
            ),
            "retry_of_action_id": _optional_uuid(
                request.retry_of_action_id,
            ),
            "policy_decision": request.policy_decision,
            "environment_resolution_id": (
                str(request.environment_ref.resolution_id)
                if request.environment_ref is not None
                else None
            ),
            "approval_id": _optional_uuid(request.approval_id),
        },
        occurred_at=request.requested_at,
        **common,
    )
    guardrail = RuntimeObservation(
        observation_id=_observation_id(
            request.action_id,
            ObservationStage.POLICY,
        ),
        category=ObservationCategory.GUARDRAIL,
        stage=ObservationStage.POLICY,
        status=ObservationStatus.RECORDED,
        source=ObservationSource(
            source_type="qwenpaw.action.policy-decision",
            source_id=str(request.action_id),
        ),
        chat_id=request.chat_id,
        invocation_id=request.invocation_id,
        correlation_id=request.correlation_id,
        registry_generation=request.registry_generation,
        title="Action policy decided",
        facts={
            "action_id": str(request.action_id),
            "capability_id": request.capability_id,
            "kind": request.kind.value,
            "effect": request.effect.value,
            "risk": request.risk.value,
            "reversible": request.reversible,
            "policy_decision": request.policy_decision,
            "approval_ids": [
                str(link.approval_id) for link in record.approval_links
            ],
            "approval_sources": [
                link.source.value for link in record.approval_links
            ],
        },
        occurred_at=request.requested_at,
    )
    if record.result is None:
        return intent, guardrail
    result = record.result
    evidence = RuntimeObservation(
        observation_id=_observation_id(
            result.action_id,
            ObservationStage.EVIDENCE,
        ),
        stage=ObservationStage.EVIDENCE,
        status=_action_status(result.status),
        source=ObservationSource(
            source_type="qwenpaw.action.result",
            source_id=str(result.action_id),
        ),
        title="Action completed",
        facts={
            "action_id": str(result.action_id),
            "observation_digest": result.observation_digest,
            "artifact_ids": [
                str(reference.artifact_id)
                for reference in result.artifact_refs
            ],
            "evidence_ids": [
                str(reference.evidence_id)
                for reference in result.evidence_refs
            ],
            "approval_ids": [
                str(approval_id) for approval_id in result.approval_ids
            ],
            "error_code": result.error_code,
            "retryable": result.retryable,
            "retry_disposition": (
                result.retry_decision.disposition.value
                if result.retry_decision is not None
                else None
            ),
            "retry_reason": (
                result.retry_decision.reason.value
                if result.retry_decision is not None
                else None
            ),
            "retry_policy_id": (
                result.retry_decision.policy_id
                if result.retry_decision is not None
                else None
            ),
            "retry_max_attempts": (
                result.retry_decision.max_attempts
                if result.retry_decision is not None
                else None
            ),
            "retry_next_attempt": (
                result.retry_decision.next_attempt
                if result.retry_decision is not None
                else None
            ),
            "retry_after_seconds": (
                result.retry_decision.retry_after_seconds
                if result.retry_decision is not None
                else None
            ),
            "retry_input_checkpoint_id": (
                str(result.retry_decision.input_checkpoint_id)
                if result.retry_decision is not None
                and result.retry_decision.input_checkpoint_id is not None
                else None
            ),
            "side_effect_status": (
                result.side_effect_status.value
                if result.side_effect_status is not None
                else None
            ),
        },
        occurred_at=result.completed_at,
        **common,
    )
    return intent, guardrail, evidence


def _interaction_observations(
    record: InteractionRecord,
) -> tuple[RuntimeObservation, ...]:
    request = record.request
    common = {
        "category": ObservationCategory.HITL,
        "chat_id": request.chat_id,
        "invocation_id": request.invocation_id,
        "correlation_id": request.correlation_id,
        "registry_generation": None,
    }
    opened = RuntimeObservation(
        observation_id=_observation_id(
            request.interaction_id,
            ObservationStage.INTENT,
        ),
        stage=ObservationStage.INTENT,
        status=ObservationStatus.RECORDED,
        source=ObservationSource(
            source_type="qwenpaw.interaction.request",
            source_id=str(request.interaction_id),
        ),
        title="Human interaction opened",
        facts={
            "interaction_id": str(request.interaction_id),
            "kind": request.kind.value,
            "mode": request.mode.value,
            "user_input_reason": (
                request.user_input_reason.value
                if request.user_input_reason is not None
                else None
            ),
            "source_id": _optional_uuid(request.source_id),
            "option_ids": [option.option_id for option in request.options],
            "has_response_schema": bool(request.response_schema),
            "expires_at": (
                request.expires_at.isoformat()
                if request.expires_at is not None
                else None
            ),
        },
        occurred_at=request.created_at,
        **common,
    )
    if record.resolution is None:
        return (opened,)
    resolution = record.resolution
    response = resolution.response
    resolved = RuntimeObservation(
        observation_id=_observation_id(
            resolution.interaction_id,
            ObservationStage.EVIDENCE,
        ),
        stage=ObservationStage.EVIDENCE,
        status=_interaction_status(resolution.status),
        source=ObservationSource(
            source_type="qwenpaw.interaction.resolution",
            source_id=str(resolution.interaction_id),
        ),
        title="Human interaction resolved",
        facts={
            "interaction_id": str(resolution.interaction_id),
            "revision": resolution.revision,
            "selected_option_ids": (
                list(response.selected_option_ids)
                if response is not None
                else []
            ),
            "has_text_response": bool(response and response.text),
            "has_structured_response": bool(response and response.values),
        },
        occurred_at=resolution.resolved_at,
        **common,
    )
    return opened, resolved


def _control_observations(
    record: ControlRecord,
    *,
    invocation_correlations: dict[UUID, UUID],
    submission_correlations: dict[UUID, UUID],
) -> tuple[RuntimeObservation, ...]:
    command = record.command
    receipt = record.receipt
    correlation_id = (
        invocation_correlations.get(command.target_invocation_id)
        if command.target_invocation_id is not None
        else None
    )
    if correlation_id is None and command.target_submission_id is not None:
        correlation_id = submission_correlations.get(
            command.target_submission_id,
        )
    if correlation_id is None and command.ordered_submission_ids:
        ordered_correlations = {
            submission_correlations.get(submission_id)
            for submission_id in command.ordered_submission_ids
        }
        ordered_correlations.discard(None)
        if len(ordered_correlations) == 1:
            correlation_id = ordered_correlations.pop()
    common = {
        "category": _control_category(command.kind),
        "chat_id": command.chat_id,
        "invocation_id": command.target_invocation_id,
        "correlation_id": correlation_id,
        "registry_generation": None,
    }
    requested = RuntimeObservation(
        observation_id=_observation_id(
            command.command_id,
            ObservationStage.INTENT,
        ),
        stage=ObservationStage.INTENT,
        status=ObservationStatus.RECORDED,
        source=ObservationSource(
            source_type="qwenpaw.control.command",
            source_id=str(command.command_id),
        ),
        title="Runtime control requested",
        facts={
            "command_id": str(command.command_id),
            "kind": command.kind.value,
            "expected_revision": command.expected_revision,
            "target_submission_id": _optional_uuid(
                command.target_submission_id,
            ),
            "target_invocation_id": _optional_uuid(
                command.target_invocation_id,
            ),
            "has_instruction": command.instruction is not None,
            "ordered_submission_ids": [
                str(submission_id)
                for submission_id in command.ordered_submission_ids
            ],
            "ordered_submission_count": len(
                command.ordered_submission_ids,
            ),
        },
        occurred_at=command.requested_at,
        **common,
    )
    resolved = RuntimeObservation(
        observation_id=_observation_id(
            command.command_id,
            ObservationStage.EVIDENCE,
        ),
        stage=ObservationStage.EVIDENCE,
        status=_control_status(receipt.status),
        source=ObservationSource(
            source_type="qwenpaw.control.receipt",
            source_id=str(receipt.receipt_id),
        ),
        title="Runtime control resolved",
        facts={
            "command_id": str(command.command_id),
            "kind": command.kind.value,
            "revision": receipt.revision,
            "applied_at_safe_point": (
                receipt.applied_at_safe_point.value
                if receipt.applied_at_safe_point is not None
                else None
            ),
            "has_detail": bool(receipt.detail),
        },
        occurred_at=receipt.recorded_at,
        **common,
    )
    return requested, resolved


def _compaction_observation(record: CompactionRecord) -> RuntimeObservation:
    return RuntimeObservation(
        observation_id=_observation_id(
            record.compaction_id,
            ObservationStage.EVIDENCE,
        ),
        category=ObservationCategory.COMPACTION,
        stage=ObservationStage.EVIDENCE,
        status=_compaction_status(record.status),
        source=ObservationSource(
            source_type="qwenpaw.context.compaction",
            source_id=str(record.compaction_id),
        ),
        chat_id=record.conversation_id,
        invocation_id=record.invocation_id,
        correlation_id=record.correlation_id,
        registry_generation=record.registry_generation,
        title=(
            "Context compaction completed"
            if record.status is CompactionStatus.SUCCEEDED
            else "Context compaction failed"
        ),
        facts={
            "compaction_id": str(record.compaction_id),
            "strategy_id": record.strategy_id,
            "trigger": record.trigger.value,
            "before_message_count": record.before_message_count,
            "after_message_count": record.after_message_count,
            "evicted_messages": record.evicted_messages,
            "folded_items": record.folded_items,
            "context_changed": record.context_changed,
            "summary_changed": record.summary_changed,
            "error_code": record.error_code,
        },
        occurred_at=record.completed_at,
    )


def _verification_observation(
    record: VerificationRecord,
    conversation_id: str,
) -> RuntimeObservation:
    result = record.verification
    passed = sum(1 for item in result.acceptance if item.passed)
    return RuntimeObservation(
        observation_id=_observation_id(
            record.event_id,
            ObservationStage.EVIDENCE,
        ),
        category=ObservationCategory.VERIFICATION,
        stage=ObservationStage.EVIDENCE,
        status=_verification_status(result.status),
        source=ObservationSource(
            source_type="qwenpaw.task.verification-record",
            source_id=str(record.event_id),
        ),
        task_id=record.task_id,
        run_id=record.run_id,
        chat_id=conversation_id,
        invocation_id=record.invocation_id,
        correlation_id=record.correlation_id,
        registry_generation=record.registry_generation,
        title=(
            "Acceptance verification passed"
            if result.status is VerificationStatus.PASSED
            else "Acceptance verification failed"
        ),
        facts={
            "verification_id": str(result.verification_id),
            "verifier_id": result.verifier_id,
            "acceptance_total": len(result.acceptance),
            "acceptance_passed": passed,
            "acceptance_failed": len(result.acceptance) - passed,
            "artifact_count": len(result.artifact_ids),
            "evidence_count": len(result.evidence_ids),
        },
        occurred_at=record.occurred_at or result.created_at,
    )


def _task_artifact_observation(
    record: ArtifactRecord,
    conversation_id: str,
) -> RuntimeObservation:
    artifact = record.artifact
    return RuntimeObservation(
        observation_id=_task_result_observation_id(
            record.event_id,
            artifact.artifact_id,
            ObservationCategory.ARTIFACT,
        ),
        category=ObservationCategory.ARTIFACT,
        stage=ObservationStage.EVIDENCE,
        status=_artifact_status(record.status),
        source=ObservationSource(
            source_type="qwenpaw.task.artifact-record",
            source_id=str(record.event_id),
        ),
        task_id=record.task_id,
        run_id=record.run_id,
        chat_id=conversation_id,
        correlation_id=record.correlation_id,
        title="Task artifact registered",
        facts={
            "artifact_id": str(artifact.artifact_id),
            "event_id": str(record.event_id),
            "step_id": _optional_uuid(record.step_id),
            "cause_event_id": _optional_uuid(record.cause_event_id),
            "kind": artifact.kind,
            "media_type": artifact.media_type,
            "size_bytes": artifact.size_bytes,
            "content_hash": artifact.content_hash,
            "version": record.version,
            "artifact_status": record.status.value,
            "supersedes_artifact_id": _optional_uuid(
                record.supersedes_artifact_id,
            ),
            "producer": record.producer,
        },
        occurred_at=record.created_at,
    )


def _task_evidence_observation(
    record: EvidenceRecord,
    conversation_id: str,
) -> RuntimeObservation:
    evidence = record.evidence
    return RuntimeObservation(
        observation_id=_task_result_observation_id(
            record.event_id,
            evidence.evidence_id,
            ObservationCategory.EVIDENCE,
        ),
        category=ObservationCategory.EVIDENCE,
        stage=ObservationStage.EVIDENCE,
        status=ObservationStatus.SUCCEEDED,
        source=ObservationSource(
            source_type="qwenpaw.task.evidence-record",
            source_id=str(record.event_id),
        ),
        task_id=record.task_id,
        run_id=record.run_id,
        chat_id=conversation_id,
        correlation_id=record.correlation_id,
        title="Task evidence registered",
        facts={
            "evidence_id": str(evidence.evidence_id),
            "artifact_id": str(evidence.artifact_id),
            "event_id": str(record.event_id),
            "step_id": _optional_uuid(record.step_id),
            "cause_event_id": _optional_uuid(record.cause_event_id),
            "producer": evidence.producer,
            "source": record.source,
            "has_claim": bool(evidence.claim),
        },
        occurred_at=evidence.captured_at,
    )


def _submission_status(status: SubmissionStatus) -> ObservationStatus:
    return {
        SubmissionStatus.SUCCEEDED: ObservationStatus.SUCCEEDED,
        SubmissionStatus.FAILED: ObservationStatus.FAILED,
        SubmissionStatus.INTERRUPTED: ObservationStatus.CANCELLED,
        SubmissionStatus.CANCELLED: ObservationStatus.CANCELLED,
    }[status]


def _submission_observations(
    submission: TurnSubmission,
) -> tuple[RuntimeObservation, ...]:
    intent = RuntimeObservation(
        observation_id=_observation_id(
            submission.submission_id,
            ObservationStage.INTENT,
        ),
        category=ObservationCategory.CONTROL,
        stage=ObservationStage.INTENT,
        status=ObservationStatus.RECORDED,
        source=ObservationSource(
            source_type="qwenpaw.control.submission",
            source_id=str(submission.submission_id),
        ),
        chat_id=submission.chat_id,
        correlation_id=submission.correlation_id,
        title="Conversation input accepted",
        facts={
            "submission_id": str(submission.submission_id),
            "sequence": submission.sequence,
            "priority": submission.priority,
            "artifact_count": len(submission.artifact_refs),
            "input_kind": (
                submission.input_envelope.kind
                if submission.input_envelope is not None
                else None
            ),
        },
        occurred_at=submission.created_at,
    )
    if submission.status not in TERMINAL_SUBMISSION_STATUSES:
        return (intent,)
    terminal = RuntimeObservation(
        observation_id=_observation_id(
            submission.submission_id,
            ObservationStage.EVIDENCE,
        ),
        category=ObservationCategory.CONTROL,
        stage=ObservationStage.EVIDENCE,
        status=_submission_status(submission.status),
        source=ObservationSource(
            source_type="qwenpaw.control.submission",
            source_id=str(submission.submission_id),
        ),
        chat_id=submission.chat_id,
        invocation_id=submission.invocation_id,
        correlation_id=submission.correlation_id,
        title="Conversation execution completed",
        facts={
            "submission_id": str(submission.submission_id),
            "sequence": submission.sequence,
            "queue_position": submission.queue_position,
            "submission_status": submission.status.value,
            "invocation_id": (
                str(submission.invocation_id)
                if submission.invocation_id is not None
                else None
            ),
        },
        occurred_at=submission.updated_at,
    )
    return intent, terminal


def _conversation_artifact_observations(
    record: ConversationArtifactRecord,
) -> tuple[RuntimeObservation, RuntimeObservation]:
    common = {
        "chat_id": record.conversation_id,
        "invocation_id": record.invocation_id,
        "correlation_id": record.correlation_id,
        "registry_generation": record.registry_generation,
        "occurred_at": record.created_at,
    }
    artifact = record.artifact
    evidence = record.evidence
    source = ObservationSource(
        source_type="qwenpaw.conversation.artifact-record",
        source_id=str(record.record_id),
    )
    artifact_observation = RuntimeObservation(
        observation_id=_observation_id(
            artifact.artifact_id,
            ObservationStage.EVIDENCE,
        ),
        category=ObservationCategory.ARTIFACT,
        stage=ObservationStage.EVIDENCE,
        status=ObservationStatus.SUCCEEDED,
        source=source,
        title="Artifact registered",
        facts={
            "artifact_id": str(artifact.artifact_id),
            "evidence_id": str(evidence.evidence_id),
            "kind": artifact.kind,
            "media_type": artifact.media_type,
            "size_bytes": artifact.size_bytes,
            "content_hash": artifact.content_hash,
        },
        **common,
    )
    evidence_observation = RuntimeObservation(
        observation_id=_observation_id(
            evidence.evidence_id,
            ObservationStage.EVIDENCE,
        ),
        category=ObservationCategory.EVIDENCE,
        stage=ObservationStage.EVIDENCE,
        status=ObservationStatus.SUCCEEDED,
        source=source,
        title="Evidence registered",
        facts={
            "evidence_id": str(evidence.evidence_id),
            "artifact_id": str(artifact.artifact_id),
            "producer": evidence.producer,
            "has_claim": bool(evidence.claim),
        },
        **common,
    )
    return artifact_observation, evidence_observation


async def _empty_interactions() -> Sequence[InteractionRecord]:
    return ()


async def _empty_controls() -> Sequence[ControlRecord]:
    return ()


async def _empty_verifications() -> Sequence[VerificationRecord]:
    return ()


async def _empty_submissions() -> Sequence[TurnSubmission]:
    return ()


async def _empty_model_steps() -> Sequence[ModelStepContinuation]:
    return ()


async def _empty_model_resource_waits() -> Sequence[ModelResourceWait]:
    return ()


async def _empty_conversation_artifacts() -> Sequence[
    ConversationArtifactRecord
]:
    return ()


async def _empty_task_results(
    conversation_id: str,
) -> ConversationTaskResultRecords:
    return ConversationTaskResultRecords(conversation_id=conversation_id)


async def _scan_source(
    source: object,
    *args: object,
    **kwargs: object,
) -> Sequence[Any]:
    scanner = getattr(source, "scan_for_conversation", None)
    if scanner is None:
        raise TypeError(
            f"{type(source).__name__} cannot rebuild the observation index",
        )
    return await scanner(*args, **kwargs)


async def _scan_submissions(
    source: object,
    *,
    agent_id: str,
    conversation_id: str,
) -> Sequence[TurnSubmission]:
    scanner = getattr(source, "scan_submissions_for_conversation", None)
    if scanner is None:
        raise TypeError(
            f"{type(source).__name__} cannot rebuild submission activity",
        )
    return await scanner(
        agent_id=agent_id,
        conversation_id=conversation_id,
    )


class LiteObservationProjection(ObservationProjectionPort):
    """Derive Lite observations without creating a second fact store."""

    def __init__(
        self,
        model_calls: ModelCallStore,
        actions: ActionStore,
        compactions: CompactionStore,
        index: LiteObservationIndex,
        *,
        agent_id: str | None = None,
        interactions: InteractionHistoryPort | None = None,
        controls: ControlHistoryPort | None = None,
        submissions: SubmissionHistoryPort | None = None,
        conversation_artifacts: ConversationArtifactHistoryPort | None = None,
        task_results: TaskResultHistoryPort | None = None,
        verifications: VerificationHistoryPort | None = None,
        model_recovery: ModelRecoveryHistoryPort | None = None,
    ) -> None:
        self._model_calls = model_calls
        self._actions = actions
        self._compactions = compactions
        self._index = index
        self._agent_id = agent_id
        self._interactions = interactions
        self._controls = controls
        self._submissions = submissions
        self._conversation_artifacts = conversation_artifacts
        self._task_results = task_results
        self._verifications = verifications
        self._model_recovery = model_recovery

    async def list_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 100,
    ) -> Sequence[RuntimeObservation]:
        """Return newest observations derived from durable source facts."""
        page = await self.page_for_conversation(
            conversation_id,
            limit=limit,
        )
        return list(page.items)

    async def scan_for_conversation(
        self,
        conversation_id: str,
    ) -> tuple[RuntimeObservation, ...]:
        """Return all current observations for correlation replay."""
        return await self._load_observations(conversation_id)

    async def _load_observations(
        self,
        conversation_id: str,
    ) -> tuple[RuntimeObservation, ...]:
        """Project every Lite source fact for index synchronization."""
        if not conversation_id.strip():
            raise ValueError("conversation_id cannot be empty")
        if self._interactions is not None:
            if self._agent_id is None:
                raise ValueError("interaction projection requires agent_id")
            interaction_records = _scan_source(
                self._interactions,
                agent_id=self._agent_id,
                conversation_id=conversation_id,
            )
        else:
            interaction_records = _empty_interactions()
        if self._controls is not None:
            if self._agent_id is None:
                raise ValueError("control projection requires agent_id")
            control_records = _scan_source(
                self._controls,
                agent_id=self._agent_id,
                conversation_id=conversation_id,
            )
        else:
            control_records = _empty_controls()
        if self._submissions is not None:
            if self._agent_id is None:
                raise ValueError("submission projection requires agent_id")
            submission_records = _scan_submissions(
                self._submissions,
                agent_id=self._agent_id,
                conversation_id=conversation_id,
            )
        else:
            submission_records = _empty_submissions()
        conversation_artifact_records = (
            _scan_source(
                self._conversation_artifacts,
                conversation_id,
            )
            if self._conversation_artifacts is not None
            else _empty_conversation_artifacts()
        )
        task_result_records = (
            self._task_results.read_for_conversation(conversation_id)
            if self._task_results is not None
            else _empty_task_results(conversation_id)
        )
        verification_records = (
            _scan_source(
                self._verifications,
                conversation_id,
            )
            if self._verifications is not None and self._task_results is None
            else _empty_verifications()
        )
        model_step_records = (
            self._model_recovery.scan_model_steps_for_conversation(
                conversation_id,
            )
            if self._model_recovery is not None
            else _empty_model_steps()
        )
        model_resource_wait_records = (
            self._model_recovery.scan_model_resource_waits_for_conversation(
                conversation_id,
            )
            if self._model_recovery is not None
            else _empty_model_resource_waits()
        )
        (
            model_records,
            action_records,
            compaction_records,
            resolved_interactions,
            resolved_controls,
            resolved_submissions,
            resolved_conversation_artifacts,
            resolved_task_results,
            resolved_verifications,
            resolved_model_steps,
            resolved_model_resource_waits,
        ) = await asyncio.gather(
            _scan_source(
                self._model_calls,
                conversation_id,
            ),
            _scan_source(
                self._actions,
                conversation_id,
            ),
            _scan_source(
                self._compactions,
                conversation_id,
            ),
            interaction_records,
            control_records,
            submission_records,
            conversation_artifact_records,
            task_result_records,
            verification_records,
            model_step_records,
            model_resource_wait_records,
        )
        observations = [
            observation
            for record in model_records
            for observation in _model_observations(record)
        ]
        observations.extend(
            observation
            for record in action_records
            for observation in _action_observations(record)
        )
        observations.extend(
            _compaction_observation(record) for record in compaction_records
        )
        observations.extend(
            observation
            for record in resolved_interactions
            for observation in _interaction_observations(record)
        )
        invocation_correlations = {
            submission.invocation_id: submission.correlation_id
            for submission in resolved_submissions
            if submission.invocation_id is not None
        }
        submission_correlations = {
            submission.submission_id: submission.correlation_id
            for submission in resolved_submissions
        }
        observations.extend(
            observation
            for record in resolved_controls
            for observation in _control_observations(
                record,
                invocation_correlations=invocation_correlations,
                submission_correlations=submission_correlations,
            )
        )
        observations.extend(
            observation
            for record in resolved_submissions
            for observation in _submission_observations(record)
        )
        task_artifact_ids = {
            str(record.artifact.artifact_id)
            for record in resolved_task_results.artifacts
        }
        task_evidence_ids = {
            str(record.evidence.evidence_id)
            for record in resolved_task_results.evidence
        }
        observations.extend(
            observation
            for record in resolved_conversation_artifacts
            for observation in _conversation_artifact_observations(record)
            if not (
                observation.category is ObservationCategory.ARTIFACT
                and observation.facts["artifact_id"] in task_artifact_ids
            )
            and not (
                observation.category is ObservationCategory.EVIDENCE
                and observation.facts["evidence_id"] in task_evidence_ids
            )
        )
        observations.extend(
            _task_artifact_observation(record, conversation_id)
            for record in resolved_task_results.artifacts
        )
        observations.extend(
            _task_evidence_observation(record, conversation_id)
            for record in resolved_task_results.evidence
        )
        observations.extend(
            _verification_observation(record, conversation_id)
            for record in (
                resolved_task_results.verifications or resolved_verifications
            )
        )
        observations.extend(
            _model_step_observation(record) for record in resolved_model_steps
        )
        observations.extend(
            _model_resource_wait_observation(record)
            for record in resolved_model_resource_waits
        )
        observations.sort(
            key=lambda item: (item.occurred_at, str(item.observation_id)),
            reverse=True,
        )
        return tuple(observations)

    async def page_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> ObservationPage:
        """Return one stable page without copying source facts."""
        if not conversation_id.strip():
            raise ValueError("conversation_id cannot be empty")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        observations = await self._load_observations(conversation_id)
        entries, next_cursor = await self._index.sync_and_page(
            conversation_id,
            observations,
            limit=limit,
            cursor=cursor,
        )
        by_id = {str(item.observation_id): item for item in observations}
        missing = [
            entry.observation_id
            for entry in entries
            if entry.observation_id not in by_id
        ]
        if missing:
            observations = await self._load_observations(conversation_id)
            by_id = {str(item.observation_id): item for item in observations}
            missing = [item for item in missing if item not in by_id]
        if missing:
            raise RuntimeError(
                "observation index references unavailable source facts",
            )
        return ObservationPage(
            items=tuple(by_id[item.observation_id] for item in entries),
            next_cursor=next_cursor,
        )


def lite_observation_projection(
    workspace_dir: Path,
    *,
    agent_id: str | None = None,
    interactions: InteractionHistoryPort | None = None,
    controls: ControlHistoryPort | None = None,
    submissions: SubmissionHistoryPort | None = None,
    conversation_artifacts: ConversationArtifactHistoryPort | None = None,
    task_results: TaskResultHistoryPort | None = None,
    verifications: VerificationHistoryPort | None = None,
    model_recovery: ModelRecoveryHistoryPort | None = None,
) -> LiteObservationProjection:
    """Return the Lite semantic projection over workspace source facts."""
    from .compactions import lite_compaction_store

    return LiteObservationProjection(
        lite_model_call_store(workspace_dir),
        lite_action_store(workspace_dir),
        lite_compaction_store(workspace_dir),
        LiteObservationIndex(
            Path(workspace_dir) / ".qwenpaw" / "lite" / "observations.sqlite3",
        ),
        agent_id=agent_id,
        interactions=interactions,
        controls=controls,
        submissions=submissions,
        conversation_artifacts=conversation_artifacts,
        task_results=task_results,
        verifications=verifications,
        model_recovery=model_recovery,
    )


__all__ = [
    "LiteObservationProjection",
    "lite_observation_projection",
]
