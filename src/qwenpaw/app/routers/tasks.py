# -*- coding: utf-8 -*-
"""Stable HTTP surface for durable Lite tasks."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from starlette.responses import Response, StreamingResponse

from ...kernel.models import (
    ActorRef,
    ActorType,
    ApprovalStatus,
    ArtifactRenderDisposition,
)
from ...kernel.state_machine import InvalidTaskTransition
from ...tasks.artifacts import ArtifactIntegrityError
from ...tasks.artifact_application import (
    ArtifactContentNotFoundError,
    TaskArtifactNotFoundError,
)
from ...tasks.application import (
    CreateTaskCommand,
    InvalidTaskProjectDirectoryError,
)
from ...tasks.approval_application import (
    DecideTaskApprovalCommand,
    ProposalNotAvailableError,
    TaskRuntimeLostError,
)
from ...tasks.ledger import TaskVersionConflictError
from ...tasks.runner import (
    RunnerCapabilityUnavailableError,
    TaskExecutionBudgetExceededError,
)
from ...tasks.runtime import (
    PlannerCapabilityUnavailableError,
    StrategyCapabilityUnavailableError,
    TaskExecutionStillAttachedError,
)
from ...tasks.renderers import (
    ArtifactPreviewTooLargeError,
    ArtifactRendererUnavailableError,
)
from ...tasks.sensors import (
    SensorCapabilityUnavailableError,
    SensorProposalLimitError,
    SensorTriggerPayloadLimitError,
)
from ...tasks.service import (
    ApprovalAlreadyResolvedError,
    ApprovalNotFoundError,
    CheckpointNotResumableError,
    IdempotencyRequestConflictError,
    SideEffectNotRecoverableError,
    SideEffectRecoveryRequiredError,
    TaskNotFoundError,
)
from ...tasks.side_effect_application import (
    AuthorizeSideEffectRetryCommand,
)
from ...tasks.workbench import ApprovalWorkbenchItem
from ..task_api_errors import TaskApiError
from ..task_http_contracts import (
    ApprovalDecisionRequest,
    CreateTaskRequest,
    SideEffectRetryRequest,
    TaskEventPageResponse,
    TaskListResponse,
)
from ..task_runtime import task_application_bindings

router = APIRouter(prefix="/tasks", tags=["tasks"])
logger = logging.getLogger(__name__)


def _problem(status: int, code: str, detail: str) -> TaskApiError:
    return TaskApiError(status, code, detail)


@router.post("", status_code=201)
async def create_task(
    body: CreateTaskRequest,
    request: Request,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> dict:
    """Create one durable task without starting execution implicitly."""
    application = (await task_application_bindings(request)).tasks
    try:
        task = await application.create(
            CreateTaskCommand(
                objective=body.objective,
                source=body.source,
                constraints=body.constraints,
                acceptance_criteria=body.acceptance_criteria,
                execution_contract=body.execution_contract,
                project_dir=body.project_dir,
                runner_id=body.runner_id,
                strategy_id=body.strategy_id,
                approval_level=body.approval_level,
            ),
            idempotency_key=idempotency_key,
        )
    except InvalidTaskProjectDirectoryError as exc:
        raise _problem(
            400,
            "invalid_project_dir",
            "Task project directory does not exist",
        ) from exc
    except IdempotencyRequestConflictError as exc:
        raise _problem(
            409,
            "idempotency_conflict",
            "Idempotency-Key was used for another request",
        ) from exc
    return task.model_dump(mode="json")


@router.get("", response_model=TaskListResponse)
async def list_tasks(
    request: Request,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> TaskListResponse:
    """Return a cursor-ready page of task projections."""
    page = await (await task_application_bindings(request)).tasks.list(
        cursor=cursor,
        limit=limit,
    )
    return TaskListResponse(
        items=page.items,
        next_cursor=page.next_cursor,
    )


@router.get("/capabilities")
async def list_task_capabilities(
    request: Request,
    slot: Annotated[
        str,
        Query(pattern=r"^[a-z][a-z0-9_.-]*$"),
    ] = "strategy",
) -> dict:
    """List executable capabilities from one current registry generation."""
    catalog = await (
        await task_application_bindings(request)
    ).capabilities.list(slot=slot)
    return {
        "registry_generation": catalog.registry_generation,
        "items": [
            descriptor.model_dump(mode="json") for descriptor in catalog.items
        ],
    }


@router.get("/capabilities/compatibility")
async def task_capability_compatibility(request: Request) -> dict:
    """Return agent-scoped legacy capability usage diagnostics."""
    bindings = await task_application_bindings(request)
    report = await bindings.capability_compatibility.report(
        agent_id=bindings.workspace.agent_id,
    )
    return report.model_dump(mode="json")


@router.post("/sensors/{capability_id}/poll", status_code=202)
async def poll_sensor(capability_id: str, request: Request) -> dict:
    """Poll one contributed sensor through the approval-gated ingress."""
    try:
        tasks = await (await task_application_bindings(request)).sensors.poll(
            capability_id,
        )
    except SensorCapabilityUnavailableError as exc:
        raise _problem(
            409,
            "sensor_unavailable",
            str(exc),
        ) from exc
    except SensorProposalLimitError as exc:
        raise _problem(
            422,
            "sensor_proposal_limit",
            str(exc),
        ) from exc
    except SensorTriggerPayloadLimitError as exc:
        raise _problem(
            422,
            "sensor_trigger_payload_limit",
            str(exc),
        ) from exc
    return {
        "items": [task.model_dump(mode="json") for task in tasks],
    }


@router.get("/{task_id}")
async def get_task(task_id: UUID, request: Request) -> dict:
    """Return a task and all of its execution attempts."""
    try:
        detail = await (await task_application_bindings(request)).tasks.detail(
            task_id,
        )
    except TaskNotFoundError as exc:
        raise _problem(404, "task_not_found", "Task does not exist") from exc
    return {
        "task": detail.task.model_dump(mode="json"),
        "runs": [run.model_dump(mode="json") for run in detail.runs],
        "plan": (detail.plan.model_dump(mode="json") if detail.plan else None),
    }


@router.get("/{task_id}/projection")
async def get_task_projection(task_id: UUID, request: Request) -> dict:
    """Return the authoritative bounded Task Workbench read model."""
    bindings = await task_application_bindings(request)
    try:
        workbench = await bindings.tasks.workbench(task_id)
    except TaskNotFoundError as exc:
        raise _problem(
            404,
            "task_not_found",
            "Task does not exist",
        ) from exc
    task = workbench.task
    active_run = workbench.active_run
    result_projection = workbench.results
    artifact_projections = await bindings.artifacts.describe(
        tuple(record.artifact for record in result_projection.artifacts),
    )
    artifact_items = [
        projection.to_public_dict() for projection in artifact_projections
    ]
    capabilities = []
    if active_run is not None:
        capabilities.append(
            {
                "capability_id": active_run.runner_id,
                "slot": "runner",
                "registry_generation": active_run.registry_generation,
            },
        )
        if active_run.strategy_id is not None:
            capabilities.append(
                {
                    "capability_id": active_run.strategy_id,
                    "slot": "strategy",
                    "registry_generation": active_run.registry_generation,
                },
            )
    renderer_capabilities = {
        (
            projection.preview.renderer_id,
            projection.preview.registry_generation,
        )
        for projection in artifact_projections
        if projection.preview.renderer_id is not None
    }
    capabilities.extend(
        {
            "capability_id": renderer_id,
            "slot": "artifact.renderer",
            "registry_generation": generation,
        }
        for renderer_id, generation in sorted(renderer_capabilities)
    )
    return {
        "task": task.model_dump(mode="json"),
        "active_run": (
            active_run.model_dump(mode="json") if active_run else None
        ),
        "runs": [run.model_dump(mode="json") for run in workbench.runs],
        "latest_plan": (
            workbench.latest_plan.model_dump(mode="json")
            if workbench.latest_plan
            else None
        ),
        "conversation_messages": [
            item.to_public_dict() for item in workbench.conversation_messages
        ],
        "tool_activities": [
            event.model_dump(mode="json")
            for event in workbench.tool_activities
        ],
        "pending_approvals": [
            item.to_public_dict() for item in workbench.pending_approvals
        ],
        "recent_decisions": [
            item.to_public_dict() for item in workbench.recent_decisions
        ],
        "artifacts": artifact_items,
        "artifact_registry": [
            record.model_dump(mode="json")
            for record in result_projection.artifacts
        ],
        "evidence": [
            item.model_dump(mode="json") for item in result_projection.evidence
        ],
        "evidence_registry": [
            record.model_dump(mode="json")
            for record in result_projection.evidence_records
        ],
        "verifications": [
            result.model_dump(mode="json")
            for result in result_projection.verifications
        ],
        "verification_registry": [
            record.model_dump(mode="json")
            for record in result_projection.verification_records
        ],
        "result_package": (
            result_projection.package(
                task.task_id,
                active_run.run_id,
            ).model_dump(mode="json")
            if active_run
            else None
        ),
        "usage": workbench.usage.model_dump(mode="json"),
        "checkpoint": (
            workbench.checkpoint.model_dump(mode="json")
            if workbench.checkpoint
            else None
        ),
        "capabilities": capabilities,
        "last_sequence": workbench.last_sequence,
    }


@router.post("/{task_id}/start", status_code=202)
async def start_task(task_id: UUID, request: Request) -> dict:
    """Plan and execute a created task through the Console Agent runtime."""
    bindings = await task_application_bindings(request)
    try:
        result = await bindings.execution.start(task_id)
    except TaskNotFoundError as exc:
        raise _problem(404, "task_not_found", "Task does not exist") from exc
    except InvalidTaskTransition as exc:
        raise _problem(
            409,
            "invalid_task_transition",
            "Task cannot be started in its current state",
        ) from exc
    except RunnerCapabilityUnavailableError as exc:
        raise _problem(
            409,
            "runner_unavailable",
            str(exc),
        ) from exc
    except PlannerCapabilityUnavailableError as exc:
        raise _problem(
            409,
            "planner_unavailable",
            str(exc),
        ) from exc
    except StrategyCapabilityUnavailableError as exc:
        raise _problem(
            409,
            "strategy_unavailable",
            str(exc),
        ) from exc
    return {
        "task": result.task.model_dump(mode="json"),
        "run": result.run.model_dump(mode="json"),
    }


@router.post("/{task_id}/cancel")
async def cancel_task(
    task_id: UUID,
    request: Request,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> dict:
    """Cancel a non-terminal task."""
    bindings = await task_application_bindings(request)
    try:
        task = await bindings.execution.cancel(
            task_id,
            idempotency_key=idempotency_key,
        )
    except TaskNotFoundError as exc:
        raise _problem(404, "task_not_found", "Task does not exist") from exc
    except InvalidTaskTransition as exc:
        raise _problem(
            409,
            "invalid_task_transition",
            "Task cannot be cancelled in its current state",
        ) from exc
    except IdempotencyRequestConflictError as exc:
        raise _problem(
            409,
            "idempotency_conflict",
            "Idempotency-Key was used for another request",
        ) from exc
    return task.model_dump(mode="json")


@router.post("/{task_id}/resume")
async def resume_task(
    task_id: UUID,
    request: Request,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> dict:
    """Create a new attempt from the latest safe checkpoint."""
    bindings = await task_application_bindings(request)
    try:
        result = await bindings.execution.resume(
            task_id,
            idempotency_key=idempotency_key,
        )
    except TaskNotFoundError as exc:
        raise _problem(404, "task_not_found", "Task does not exist") from exc
    except CheckpointNotResumableError as exc:
        raise _problem(
            409,
            "checkpoint_not_resumable",
            "No safe checkpoint is available",
        ) from exc
    except SideEffectRecoveryRequiredError as exc:
        raise _problem(
            409,
            "side_effect_recovery_required",
            "Uncertain side effects must be reviewed before resuming",
        ) from exc
    except TaskExecutionBudgetExceededError as exc:
        raise _problem(
            409,
            "execution_budget_exhausted",
            "Task execution retry budget is exhausted",
        ) from exc
    except TaskExecutionStillAttachedError as exc:
        raise _problem(
            409,
            "task_runtime_attached",
            "Task is still executing in this process",
        ) from exc
    except RunnerCapabilityUnavailableError as exc:
        raise _problem(
            409,
            "runner_unavailable",
            str(exc),
        ) from exc
    except StrategyCapabilityUnavailableError as exc:
        raise _problem(
            409,
            "strategy_unavailable",
            str(exc),
        ) from exc
    except InvalidTaskTransition as exc:
        raise _problem(
            409,
            "invalid_task_transition",
            "Task cannot be resumed in its current state",
        ) from exc
    except IdempotencyRequestConflictError as exc:
        raise _problem(
            409,
            "idempotency_conflict",
            "Idempotency-Key was used for another request",
        ) from exc
    return {
        "task": result.task.model_dump(mode="json"),
        "run": result.run.model_dump(mode="json"),
    }


@router.get("/{task_id}/events", response_model=TaskEventPageResponse)
async def list_task_events(
    task_id: UUID,
    request: Request,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> TaskEventPageResponse:
    """Return an ordered event page for timeline rendering."""
    try:
        page = await (await task_application_bindings(request)).events.page(
            task_id,
            after_sequence=after_sequence,
            limit=limit,
        )
    except TaskNotFoundError as exc:
        raise _problem(
            404,
            "task_not_found",
            "Task does not exist",
        ) from exc
    return TaskEventPageResponse(
        items=page.items,
        next_sequence=page.next_sequence,
    )


@router.get("/{task_id}/stream")
async def stream_task_events(
    task_id: UUID,
    request: Request,
    last_event_id: Annotated[
        str | None,
        Header(alias="Last-Event-ID"),
    ] = None,
    after_sequence: Annotated[int | None, Query(ge=0)] = None,
) -> StreamingResponse:
    """Replay committed events and follow the task until terminal."""
    try:
        header_sequence = int(last_event_id or 0)
    except ValueError as exc:
        raise _problem(
            400,
            "invalid_last_event_id",
            "Last-Event-ID must be an integer",
        ) from exc
    if (
        after_sequence is not None
        and last_event_id is not None
        and after_sequence != header_sequence
    ):
        raise _problem(
            400,
            "invalid_event_cursor",
            "after_sequence conflicts with Last-Event-ID",
        )
    starting_sequence = (
        after_sequence if after_sequence is not None else header_sequence
    )
    application = (await task_application_bindings(request)).events
    try:
        await application.page(
            task_id,
            after_sequence=starting_sequence,
            limit=1,
        )
    except TaskNotFoundError as exc:
        raise _problem(
            404,
            "task_not_found",
            "Task does not exist",
        ) from exc

    async def generate() -> AsyncIterator[str]:
        async for event in application.follow(
            task_id,
            after_sequence=starting_sequence,
        ):
            payload = json.dumps(
                event.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            yield f"id: {event.sequence}\ndata: {payload}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@router.post("/{task_id}/approvals/{approval_id}/decision")
async def decide_approval(
    task_id: UUID,
    approval_id: UUID,
    body: ApprovalDecisionRequest,
    request: Request,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> dict:
    """Persist a user decision and continue or stop the current run."""
    try:
        decision = await (
            await task_application_bindings(request)
        ).approvals.decide(
            DecideTaskApprovalCommand(
                task_id=task_id,
                approval_id=approval_id,
                decision=body.decision,
                actor=ActorRef(type=ActorType.USER, id="local-user"),
                reason=body.reason,
                scope=body.scope,
                idempotency_key=idempotency_key,
            ),
        )
    except TaskRuntimeLostError as exc:
        raise _problem(
            409,
            "task_runtime_lost",
            "The original runtime was lost; resume the Task from its "
            "approval checkpoint",
        ) from exc
    except ProposalNotAvailableError as exc:
        raise _problem(
            409,
            "proposal_not_available",
            "Approved task does not contain a valid proposal",
        ) from exc
    except (ApprovalNotFoundError, TaskNotFoundError) as exc:
        raise _problem(
            404,
            "approval_not_found",
            "Approval does not exist",
        ) from exc
    except ApprovalAlreadyResolvedError as exc:
        raise _problem(
            409,
            "approval_already_resolved",
            "Approval already has a decision",
        ) from exc
    except (InvalidTaskTransition, TaskVersionConflictError) as exc:
        raise _problem(
            409,
            "task_version_conflict",
            "Task changed before the decision was committed",
        ) from exc
    except IdempotencyRequestConflictError as exc:
        raise _problem(
            409,
            "idempotency_conflict",
            "Idempotency-Key was used for another request",
        ) from exc
    return decision.model_dump(mode="json")


@router.get("/{task_id}/approvals")
async def list_task_approvals(
    task_id: UUID,
    request: Request,
    run_id: UUID | None = None,
    status: ApprovalStatus | None = None,
) -> dict:
    """Return authoritative task-scoped approval projections."""
    try:
        records = await (
            await task_application_bindings(request)
        ).approvals.list(
            task_id,
            run_id=run_id,
            status=status,
        )
    except TaskNotFoundError as exc:
        raise _problem(
            404,
            "task_not_found",
            "Task does not exist",
        ) from exc
    items = [
        ApprovalWorkbenchItem(
            request=approval,
            decision=decision,
        ).to_public_dict()
        for approval, decision in records
    ]
    return {"items": items}


@router.get("/{task_id}/artifacts")
async def list_task_artifacts(task_id: UUID, request: Request) -> dict:
    """Return deduplicated artifact references from the event ledger."""
    bindings = await task_application_bindings(request)
    try:
        workbench = await bindings.tasks.workbench(task_id)
    except TaskNotFoundError as exc:
        raise _problem(404, "task_not_found", "Task does not exist") from exc
    result_projection = workbench.results
    artifact_items = [
        projection.to_public_dict()
        for projection in await bindings.artifacts.describe(
            tuple(record.artifact for record in result_projection.artifacts),
        )
    ]
    return {
        "items": artifact_items,
        "registry": [
            record.model_dump(mode="json")
            for record in result_projection.artifacts
        ],
        "evidence": [
            item.model_dump(mode="json") for item in result_projection.evidence
        ],
        "evidence_registry": [
            record.model_dump(mode="json")
            for record in result_projection.evidence_records
        ],
        "verifications": [
            result.model_dump(mode="json")
            for result in result_projection.verifications
        ],
        "verification_registry": [
            record.model_dump(mode="json")
            for record in result_projection.verification_records
        ],
    }


@router.get("/{task_id}/side-effects")
async def list_task_side_effects(
    task_id: UUID,
    request: Request,
    run_id: UUID | None = None,
) -> dict:
    """Return authoritative side-effect records for recovery UI."""
    application = (await task_application_bindings(request)).side_effects
    try:
        records = await application.list(task_id, run_id=run_id)
    except TaskNotFoundError as exc:
        raise _problem(404, "task_not_found", "Task does not exist") from exc
    return {
        "items": [record.model_dump(mode="json") for record in records],
    }


@router.post("/{task_id}/side-effects/{record_id}/retry")
async def authorize_side_effect_retry(
    task_id: UUID,
    record_id: UUID,
    body: SideEffectRetryRequest,
    request: Request,
) -> dict:
    """Acknowledge that an uncertain side effect is safe to retry."""
    application = (await task_application_bindings(request)).side_effects
    try:
        record = await application.authorize_retry(
            AuthorizeSideEffectRetryCommand(
                task_id=task_id,
                record_id=record_id,
                actor=ActorRef(type=ActorType.USER, id="local-user"),
                reason=body.reason,
            ),
        )
    except KeyError as exc:
        raise _problem(
            404,
            "side_effect_not_found",
            "Side effect does not belong to this Task",
        ) from exc
    except SideEffectNotRecoverableError as exc:
        raise _problem(
            409,
            "side_effect_not_recoverable",
            "Only uncertain side effects can authorize a retry",
        ) from exc
    return record.model_dump(mode="json")


@router.get("/{task_id}/artifacts/{artifact_id}/content")
async def get_task_artifact_content(
    task_id: UUID,
    artifact_id: UUID,
    request: Request,
    disposition: Annotated[
        str,
        Query(pattern=r"^(inline|attachment)$"),
    ] = "inline",
) -> Response:
    """Read a task-owned artifact through its verified content digest."""
    bindings = await task_application_bindings(request)
    try:
        rendered_artifact = await bindings.artifacts.render(
            task_id,
            artifact_id,
            disposition=ArtifactRenderDisposition(disposition),
        )
    except TaskNotFoundError as exc:
        raise _problem(
            404,
            "task_not_found",
            "Task does not exist",
        ) from exc
    except TaskArtifactNotFoundError as exc:
        raise _problem(
            404,
            "artifact_not_found",
            "Artifact does not belong to this task",
        ) from exc
    except ArtifactContentNotFoundError as exc:
        raise _problem(
            404,
            "artifact_content_not_found",
            "Artifact content is unavailable",
        ) from exc
    except ArtifactIntegrityError as exc:
        raise _problem(
            409,
            "artifact_integrity_error",
            "Artifact content failed integrity verification",
        ) from exc
    except ArtifactPreviewTooLargeError as exc:
        raise _problem(
            413,
            "artifact_preview_too_large",
            "Artifact is too large for inline preview",
        ) from exc
    except ArtifactRendererUnavailableError as exc:
        if exc.failures:
            raise _problem(
                503,
                "artifact_renderer_failed",
                "Artifact renderers failed without a safe fallback",
            ) from exc
        raise _problem(
            415,
            "artifact_preview_unsupported",
            "No safe renderer supports this artifact",
        ) from exc

    rendered = rendered_artifact.result
    encoded_filename = quote(rendered.filename, safe="")
    headers = {
        "Cache-Control": "private, no-store",
        "Content-Disposition": (
            f"{rendered.disposition.value}; "
            f"filename*=UTF-8''{encoded_filename}"
        ),
        "X-Content-Type-Options": "nosniff",
        "X-QwenPaw-Artifact-Renderer": rendered.renderer_id,
        "X-QwenPaw-Registry-Generation": str(
            rendered_artifact.registry_generation,
        ),
        "X-QwenPaw-Source-Hash": rendered.source_content_hash,
    }
    if rendered.disposition is ArtifactRenderDisposition.INLINE:
        headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return Response(
        content=rendered.content,
        media_type=rendered.media_type,
        headers=headers,
    )
