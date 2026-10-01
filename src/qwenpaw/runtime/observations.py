# -*- coding: utf-8 -*-
"""Semantic runtime projections over durable Agent OS source facts."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID, uuid5

from ..kernel import (
    ActionRecord,
    ActionStatus,
    ActionStore,
    ControlCommandKind,
    ControlCommandStatus,
    ControlHistoryPort,
    ControlRecord,
    CompactionRecord,
    CompactionStatus,
    CompactionStore,
    InteractionHistoryPort,
    InteractionRecord,
    InteractionStatus,
    ModelCallRecord,
    ModelCallStatus,
    ModelCallStore,
    ObservationCategory,
    ObservationProjectionPort,
    ObservationSource,
    ObservationStage,
    ObservationStatus,
    RuntimeObservation,
)
from .actions import lite_action_store
from .model_calls import lite_model_call_store

_SOURCE_SCAN_LIMIT = 1000


def _observation_id(source_id: UUID, stage: ObservationStage) -> UUID:
    return uuid5(source_id, f"runtime-observation:{stage.value}")


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


def _model_observations(
    record: ModelCallRecord,
) -> tuple[RuntimeObservation, ...]:
    route = record.route
    attempt = record.attempt
    common = {
        "category": ObservationCategory.MODEL,
        "conversation_id": attempt.conversation_id,
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
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cost_micros": result.cost_micros,
            "cost_unknown": result.cost_unknown,
        },
        occurred_at=result.completed_at,
        **common,
    )
    return route_observation, attempt_observation, result_observation


def _action_observations(
    record: ActionRecord,
) -> tuple[RuntimeObservation, ...]:
    request = record.request
    if request.conversation_id is None:
        return ()
    common = {
        "category": ObservationCategory.ACTION,
        "conversation_id": request.conversation_id,
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
        conversation_id=request.conversation_id,
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
        "conversation_id": request.conversation_id,
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
) -> tuple[RuntimeObservation, ...]:
    command = record.command
    receipt = record.receipt
    common = {
        "category": _control_category(command.kind),
        "conversation_id": command.conversation_id,
        "invocation_id": command.target_invocation_id,
        "correlation_id": None,
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
        conversation_id=record.conversation_id,
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


async def _empty_interactions() -> Sequence[InteractionRecord]:
    return ()


async def _empty_controls() -> Sequence[ControlRecord]:
    return ()


class LiteObservationProjection(ObservationProjectionPort):
    """Derive Lite observations without creating a second fact store."""

    def __init__(
        self,
        model_calls: ModelCallStore,
        actions: ActionStore,
        compactions: CompactionStore,
        *,
        agent_id: str | None = None,
        interactions: InteractionHistoryPort | None = None,
        controls: ControlHistoryPort | None = None,
    ) -> None:
        self._model_calls = model_calls
        self._actions = actions
        self._compactions = compactions
        self._agent_id = agent_id
        self._interactions = interactions
        self._controls = controls

    async def list_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 100,
    ) -> Sequence[RuntimeObservation]:
        """Return newest observations derived from durable source facts."""
        if not conversation_id.strip():
            raise ValueError("conversation_id cannot be empty")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        if self._interactions is not None:
            if self._agent_id is None:
                raise ValueError("interaction projection requires agent_id")
            interaction_records = self._interactions.list_for_conversation(
                agent_id=self._agent_id,
                conversation_id=conversation_id,
                limit=_SOURCE_SCAN_LIMIT,
            )
        else:
            interaction_records = _empty_interactions()
        if self._controls is not None:
            if self._agent_id is None:
                raise ValueError("control projection requires agent_id")
            control_records = self._controls.list_for_conversation(
                agent_id=self._agent_id,
                conversation_id=conversation_id,
                limit=_SOURCE_SCAN_LIMIT,
            )
        else:
            control_records = _empty_controls()
        (
            model_records,
            action_records,
            compaction_records,
            resolved_interactions,
            resolved_controls,
        ) = await asyncio.gather(
            self._model_calls.list_for_conversation(
                conversation_id,
                limit=_SOURCE_SCAN_LIMIT,
            ),
            self._actions.list_for_conversation(
                conversation_id,
                limit=_SOURCE_SCAN_LIMIT,
            ),
            self._compactions.list_for_conversation(
                conversation_id,
                limit=_SOURCE_SCAN_LIMIT,
            ),
            interaction_records,
            control_records,
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
        observations.extend(
            observation
            for record in resolved_controls
            for observation in _control_observations(record)
        )
        observations.sort(
            key=lambda item: (item.occurred_at, str(item.observation_id)),
            reverse=True,
        )
        return observations[:limit]


def lite_observation_projection(
    workspace_dir: Path,
    *,
    agent_id: str | None = None,
    interactions: InteractionHistoryPort | None = None,
    controls: ControlHistoryPort | None = None,
) -> LiteObservationProjection:
    """Return the Lite semantic projection over workspace source facts."""
    from .compactions import lite_compaction_store

    return LiteObservationProjection(
        lite_model_call_store(workspace_dir),
        lite_action_store(workspace_dir),
        lite_compaction_store(workspace_dir),
        agent_id=agent_id,
        interactions=interactions,
        controls=controls,
    )


__all__ = [
    "LiteObservationProjection",
    "lite_observation_projection",
]
