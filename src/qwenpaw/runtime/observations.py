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
    if record.result is None:
        return (intent,)
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
    return intent, evidence


class LiteObservationProjection(ObservationProjectionPort):
    """Derive Lite observations without creating a second fact store."""

    def __init__(
        self,
        model_calls: ModelCallStore,
        actions: ActionStore,
    ) -> None:
        self._model_calls = model_calls
        self._actions = actions

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
        model_records, action_records = await asyncio.gather(
            self._model_calls.list_for_conversation(
                conversation_id,
                limit=_SOURCE_SCAN_LIMIT,
            ),
            self._actions.list_for_conversation(
                conversation_id,
                limit=_SOURCE_SCAN_LIMIT,
            ),
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
        observations.sort(
            key=lambda item: (item.occurred_at, str(item.observation_id)),
            reverse=True,
        )
        return observations[:limit]


def lite_observation_projection(
    workspace_dir: Path,
) -> LiteObservationProjection:
    """Return the Lite semantic projection over workspace source facts."""
    return LiteObservationProjection(
        lite_model_call_store(workspace_dir),
        lite_action_store(workspace_dir),
    )


__all__ = [
    "LiteObservationProjection",
    "lite_observation_projection",
]
