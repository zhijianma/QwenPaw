# -*- coding: utf-8 -*-
"""Unified, privacy-safe action evidence for the Lite runtime."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence
from uuid import UUID, uuid5

from agentscope.message import ToolResultState
from agentscope.tool import ToolResponse

from ..kernel import (
    ACTION_RETRY_DECISION_METADATA_KEY,
    ACTION_RETRY_HINT_METADATA_KEY,
    ActionAdmission,
    ActionAdmissionEvidence,
    ActionAdmissionMode,
    ActionApprovalLink,
    ActionExecutionContext,
    ActionIdempotencyMode,
    ActionKind,
    ActionRecord,
    ActionRequest,
    ActionResult,
    ActionRetryDecision,
    ActionRetryDisposition,
    ActionRetryContinuationStore,
    ActionRetryInputStore,
    ActionRetryPolicy,
    ActionRetryReason,
    ActionStore,
    ActionStatus,
    ApprovalSource,
    ArtifactRef,
    COMMITTED_ACTION_ITEM_METADATA_KEY,
    CommittedActionItem,
    EvidenceRef,
    EnvironmentRef,
    DriverToolDefinition,
    InvocationScope,
    ModelStepReconciliation,
    ModelStepReconciliationReason,
    RiskLevel,
    SideEffectStatus,
    ToolEffect,
    ToolSelection,
)
from ..kernel.models import utc_now
from ..tasks.redaction import redact_payload
from ..tool_calls._context import ToolCallContext
from ..utils.io_utils import (
    get_path_lock,
    read_json_async,
    run_sync_io,
    write_json_atomic_async,
)
from .tool_artifacts import (
    TOOL_ARTIFACT_ERRORS_KEY,
    TOOL_ARTIFACT_LINKS_KEY,
)

logger = logging.getLogger(__name__)

ACTION_REQUEST_CONTEXT_KEY = "qwenpaw_action_request"
ACTION_REQUEST_STATE_KEY = "qwenpaw_action_request_state"
ACTION_RESULT_CONTEXT_KEY = "qwenpaw_action_id"
_BULK_ARGUMENT_NAMES = frozenset(
    {
        "audio",
        "body",
        "bytes",
        "code",
        "command",
        "content",
        "data",
        "document",
        "image",
        "payload",
        "prompt",
        "script",
        "text",
    },
)


class ActionConflictError(RuntimeError):
    """Raised when one action identity has conflicting durable evidence."""


class ActionRequestPersistenceError(RuntimeError):
    """Raised before execution when action intent cannot be stored."""


class ActionResultPersistenceError(RuntimeError):
    """Raised after execution when its terminal evidence cannot be stored."""


def _action_admission(
    *,
    kind: ActionKind,
    capability_id: str,
    policy_decision: str,
) -> ActionAdmission:
    """Describe the real admission boundary for one executor family."""
    decision = policy_decision or "unknown"
    if decision == "retry_policy":
        return ActionAdmission(
            mode=ActionAdmissionMode.HOST_PRE_EXECUTION,
            authority="qwenpaw.system.action-retry",
            decision=decision,
            evidence=ActionAdmissionEvidence.ACTION_INTENT,
        )
    if kind is ActionKind.DRIVER:
        return ActionAdmission(
            mode=ActionAdmissionMode.EXECUTOR_DELEGATED,
            authority=capability_id,
            decision=decision,
            evidence=ActionAdmissionEvidence.ACTION_INTENT,
        )
    if kind is ActionKind.HARNESS_REMOTE:
        return ActionAdmission(
            mode=ActionAdmissionMode.PROVIDER_OBSERVED,
            authority=capability_id,
            decision=decision,
            evidence=ActionAdmissionEvidence.PROVIDER_EVENT,
        )
    if decision in {"unobserved", "unknown"}:
        return ActionAdmission(
            mode=ActionAdmissionMode.LEGACY_UNOBSERVED,
            authority="qwenpaw.legacy.governance",
            decision=decision,
            evidence=ActionAdmissionEvidence.LEGACY,
        )
    return ActionAdmission(
        mode=ActionAdmissionMode.HOST_PRE_EXECUTION,
        authority="qwenpaw.system.governance",
        decision=decision,
        evidence=ActionAdmissionEvidence.POLICY_AUDIT,
    )


class ActionRetryNotReadyError(RuntimeError):
    """Raised when an admitted retry has not reached its durable deadline."""

    def __init__(self, retry_at: datetime) -> None:
        super().__init__(
            f"action retry is not ready before {retry_at.isoformat()}",
        )
        self.retry_at = retry_at


def current_action_execution() -> ActionExecutionContext | None:
    """Return the active, content-safe executor identity if available."""
    from ..tool_calls._ctxvars import get_call_context

    context = get_call_context()
    if context is None:
        return None
    request = context.extra.get(ACTION_REQUEST_CONTEXT_KEY)
    if not isinstance(request, ActionRequest):
        return None
    return ActionExecutionContext(
        action_id=request.action_id,
        invocation_id=request.invocation_id,
        correlation_id=request.correlation_id,
        agent_id=request.agent_id,
        conversation_id=request.conversation_id,
        capability_id=request.capability_id,
        idempotency_mode=request.idempotency_mode,
        idempotency_key=request.idempotency_key,
        executor_item_id=request.executor_item_id or context.tool_call_id,
        retry_root_action_id=request.retry_root_action_id,
        retry_of_action_id=request.retry_of_action_id,
        attempt=request.attempt,
    )


def assess_model_step_reconciliation(
    records: Sequence[ActionRecord],
    invocation_id: UUID,
) -> ModelStepReconciliation | None:
    """Classify why Actions prevent automatic model-step continuation."""
    owned = [
        record
        for record in records
        if record.request.invocation_id == invocation_id
    ]
    if not owned:
        return None

    pending_result_count = sum(
        record.result is None for record in owned
    )
    terminal_results = [
        record.result
        for record in owned
        if record.result is not None
    ]
    uncertain_side_effect_count = sum(
        result.status in {ActionStatus.PARTIAL, ActionStatus.UNKNOWN}
        or result.side_effect_status is SideEffectStatus.UNCERTAIN
        for result in terminal_results
    )
    if uncertain_side_effect_count:
        reason = ModelStepReconciliationReason.UNCERTAIN_SIDE_EFFECT
    elif pending_result_count:
        reason = ModelStepReconciliationReason.PENDING_ACTION_RESULT
    else:
        reason = ModelStepReconciliationReason.DURABLE_CONTEXT_REQUIRED
    return ModelStepReconciliation(
        reason=reason,
        action_count=len(owned),
        pending_result_count=pending_result_count,
        uncertain_side_effect_count=uncertain_side_effect_count,
        terminal_result_count=len(terminal_results),
    )


def model_step_action_evidence_digest(
    records: Sequence[ActionRecord],
    invocation_id: UUID,
) -> str | None:
    """Hash content-safe terminal Action evidence for one Invocation."""
    assessment = assess_model_step_reconciliation(records, invocation_id)
    if (
        assessment is None
        or assessment.reason
        is not ModelStepReconciliationReason.DURABLE_CONTEXT_REQUIRED
    ):
        return None
    return model_step_reconciliation_evidence_digest(
        records,
        invocation_id,
    )


def model_step_reconciliation_evidence_digest(
    records: Sequence[ActionRecord],
    invocation_id: UUID,
) -> str | None:
    """Hash the exact terminal Action set awaiting reconciliation."""
    assessment = assess_model_step_reconciliation(records, invocation_id)
    if assessment is None or assessment.pending_result_count:
        return None
    evidence: list[dict[str, Any]] = []
    for record in records:
        if record.request.invocation_id != invocation_id:
            continue
        result = record.result
        if result is None:
            return None
        evidence.append(
            {
                "action_id": str(record.request.action_id),
                "observation_digest": result.observation_digest,
                "side_effect_status": (
                    result.side_effect_status.value
                    if result.side_effect_status is not None
                    else None
                ),
                "status": result.status.value,
            },
        )
    evidence.sort(key=lambda item: item["action_id"])
    return _sha256(evidence)


def model_step_action_call_ids(
    records: Sequence[ActionRecord],
    invocation_id: UUID,
) -> tuple[str, ...] | None:
    """Return executor call IDs only for terminal, certain Actions."""
    if model_step_action_evidence_digest(records, invocation_id) is None:
        return None
    call_ids = []
    for record in records:
        request = record.request
        if request.invocation_id != invocation_id:
            continue
        call_id = _executor_item_id(request, invocation_id)
        if not call_id:
            return None
        call_ids.append(call_id)
    if len(call_ids) != len(set(call_ids)):
        return None
    return tuple(sorted(call_ids))


def model_step_action_context_bindings(
    records: Sequence[ActionRecord],
    invocation_id: UUID,
) -> tuple[tuple[str, str], ...] | None:
    """Bind executor call IDs to the exact durable Action identities."""
    call_ids = model_step_action_call_ids(records, invocation_id)
    if call_ids is None:
        return None
    bindings = []
    for record in records:
        request = record.request
        if request.invocation_id != invocation_id:
            continue
        call_id = _executor_item_id(request, invocation_id)
        if call_id is None:
            return None
        bindings.append((call_id, str(request.action_id)))
    bindings.sort()
    return tuple(bindings)


def model_step_committed_action_items(
    records: Sequence[ActionRecord],
    invocation_id: UUID,
) -> tuple[CommittedActionItem, ...] | None:
    """Return exact terminal Action bindings for model-visible context."""
    if model_step_action_evidence_digest(records, invocation_id) is None:
        return None
    items: list[CommittedActionItem] = []
    for record in records:
        request = record.request
        result = record.result
        if request.invocation_id != invocation_id:
            continue
        if result is None:
            return None
        executor_item_id = _executor_item_id(request, invocation_id)
        if not executor_item_id:
            return None
        items.append(
            CommittedActionItem(
                action_id=request.action_id,
                invocation_id=request.invocation_id,
                conversation_id=request.conversation_id,
                executor_item_id=executor_item_id,
                observation_digest=result.observation_digest,
            ),
        )
    if len({item.executor_item_id for item in items}) != len(items):
        return None
    return tuple(sorted(items, key=lambda item: item.executor_item_id))


def _json_value(value: Any) -> Any:
    """Convert tool input to a stable JSON-compatible in-memory value."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return str(value)


def _executor_item_id(
    request: ActionRequest,
    invocation_id: UUID,
) -> str | None:
    """Read explicit item identity with legacy key parsing fallback."""
    if request.executor_item_id is not None:
        return request.executor_item_id
    prefix = f"{request.kind.value}:{invocation_id}:"
    if not request.idempotency_key.startswith(prefix):
        return None
    return request.idempotency_key[len(prefix) :] or None


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


def _sha256(value: Any) -> str:
    digest = hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _minimize_arguments(value: Any, *, key: str = "") -> Any:
    """Remove bulk user content while preserving safe routing parameters."""
    normalized_key = key.strip().lower().replace("-", "_")
    if normalized_key in _BULK_ARGUMENT_NAMES:
        return "[CONTENT OMITTED]"
    if isinstance(value, dict):
        return {
            nested_key: _minimize_arguments(item, key=nested_key)
            for nested_key, item in value.items()
        }
    if isinstance(value, list):
        return [_minimize_arguments(item, key=key) for item in value]
    return value


def public_action_record(record: ActionRecord) -> ActionRecord:
    """Return a read-safe projection, including records from older builds."""
    safe_arguments = _minimize_arguments(
        redact_payload(record.request.redacted_arguments),
    )
    request = record.request.model_copy(
        update={
            "arguments": {},
            "redacted_arguments": safe_arguments,
            "arguments_hash": _sha256(safe_arguments),
        },
    )
    return record.model_copy(update={"request": request})


def _risk_for_effect(effect: ToolEffect) -> RiskLevel:
    if effect is ToolEffect.NONE:
        return RiskLevel.LOW
    if effect is ToolEffect.LOCAL_WRITE:
        return RiskLevel.MEDIUM
    return RiskLevel.HIGH


def _side_effect_status(
    effect: ToolEffect,
    status: ActionStatus,
) -> SideEffectStatus | None:
    if effect is ToolEffect.NONE:
        return None
    if status in {ActionStatus.SUCCEEDED, ActionStatus.PARTIAL}:
        return SideEffectStatus.SUCCEEDED
    if status is ActionStatus.DENIED:
        return SideEffectStatus.FAILED
    return SideEffectStatus.UNCERTAIN


def _action_retry_decision(
    request: ActionRequest,
    *,
    status: ActionStatus,
    side_effect_status: SideEffectStatus | None,
    provider_retryable: bool,
) -> ActionRetryDecision:
    """Apply the host retry policy without replaying an Action."""
    policy = request.retry_policy
    if status is not ActionStatus.FAILED:
        return ActionRetryDecision(
            disposition=ActionRetryDisposition.NOT_APPLICABLE,
            reason=ActionRetryReason.STATUS_NOT_FAILED,
            provider_retryable=provider_retryable,
            max_attempts=policy.max_attempts,
        )
    executor_enforced = (
        request.idempotency_mode
        is ActionIdempotencyMode.EXECUTOR_ENFORCED
    )
    if (
        side_effect_status is SideEffectStatus.UNCERTAIN
        and provider_retryable
        and executor_enforced
    ):
        return _admit_action_retry(
            request,
            ActionRetryReason.EXECUTOR_IDEMPOTENT_FAILURE,
        )
    if side_effect_status is SideEffectStatus.UNCERTAIN:
        return ActionRetryDecision(
            disposition=ActionRetryDisposition.RECONCILE_REQUIRED,
            reason=ActionRetryReason.SIDE_EFFECT_UNCERTAIN,
            provider_retryable=provider_retryable,
            max_attempts=policy.max_attempts,
        )
    if not provider_retryable:
        return ActionRetryDecision(
            disposition=ActionRetryDisposition.FORBIDDEN,
            reason=ActionRetryReason.PROVIDER_NOT_RETRYABLE,
            max_attempts=policy.max_attempts,
        )
    if (
        request.effect is not ToolEffect.NONE
        and not executor_enforced
    ):
        return ActionRetryDecision(
            disposition=ActionRetryDisposition.FORBIDDEN,
            reason=ActionRetryReason.EFFECTFUL_RETRY_UNSUPPORTED,
            provider_retryable=True,
            max_attempts=policy.max_attempts,
        )
    return _admit_action_retry(
        request,
        ActionRetryReason.TRANSIENT_FAILURE,
    )


def _admit_action_retry(
    request: ActionRequest,
    reason: ActionRetryReason,
) -> ActionRetryDecision:
    """Apply the immutable Action attempt budget and backoff schedule."""
    policy = request.retry_policy
    if request.attempt >= policy.max_attempts:
        return ActionRetryDecision(
            disposition=ActionRetryDisposition.FORBIDDEN,
            reason=ActionRetryReason.ATTEMPT_BUDGET_EXHAUSTED,
            provider_retryable=True,
            max_attempts=policy.max_attempts,
        )
    next_attempt = request.attempt + 1
    return ActionRetryDecision(
        disposition=ActionRetryDisposition.RETRY_FROM_NEW_ACTION,
        reason=reason,
        provider_retryable=True,
        max_attempts=policy.max_attempts,
        next_attempt=next_attempt,
        retry_after_seconds=policy.delay_before_attempt(next_attempt),
    )


class FilesystemActionStore:
    """Owner-only Lite store for immutable action requests and results."""

    def __init__(self, workspace_dir: Path) -> None:
        self._root = Path(workspace_dir) / ".qwenpaw" / "lite" / "actions"

    @staticmethod
    def _conversation_key(conversation_id: str) -> str:
        return hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()

    def _action_dir(
        self,
        *,
        conversation_id: str | None,
        invocation_id: UUID,
        action_id: UUID,
    ) -> Path:
        owner = conversation_id or f"invocation:{invocation_id}"
        return (
            self._root
            / self._conversation_key(owner)
            / str(invocation_id)
            / str(action_id)
        )

    @staticmethod
    async def _append_immutable(
        path: Path,
        model: ActionApprovalLink | ActionRequest | ActionResult,
        *,
        timestamp_field: str,
    ) -> None:
        async with get_path_lock(path):
            try:
                existing_payload = await read_json_async(path)
            except FileNotFoundError:
                existing_payload = None
            model_type = type(model)
            if existing_payload is not None:
                existing = model_type.model_validate(existing_payload)
                if existing.model_dump(
                    mode="json",
                    exclude={timestamp_field},
                ) != model.model_dump(
                    mode="json",
                    exclude={timestamp_field},
                ):
                    raise ActionConflictError(
                        f"{path.name} already has different evidence",
                    )
                return
            await write_json_atomic_async(
                path,
                model.model_dump(mode="json"),
                sort_keys=True,
            )

    async def begin(self, request: ActionRequest) -> None:
        """Persist action intent before execution."""
        directory = self._action_dir(
            conversation_id=request.conversation_id,
            invocation_id=request.invocation_id,
            action_id=request.action_id,
        )
        await self._append_immutable(
            directory / "request.json",
            request,
            timestamp_field="requested_at",
        )

    async def complete(self, result: ActionResult) -> None:
        """Persist a terminal result only for an existing request."""
        directory = self._action_dir(
            conversation_id=result.conversation_id,
            invocation_id=result.invocation_id,
            action_id=result.action_id,
        )
        request_path = directory / "request.json"
        try:
            request = ActionRequest.model_validate(
                await read_json_async(request_path),
            )
        except FileNotFoundError as exc:
            raise ActionConflictError(
                "action result has no durable request",
            ) from exc
        approval_links = await run_sync_io(
            self._load_approval_links,
            directory,
        )
        ActionRecord(
            request=request,
            approval_links=approval_links,
            result=result,
        )
        await self._append_immutable(
            directory / "result.json",
            result,
            timestamp_field="completed_at",
        )

    async def link_approval(self, link: ActionApprovalLink) -> None:
        """Persist one approval relation under its owning action."""
        directory = self._action_dir(
            conversation_id=link.conversation_id,
            invocation_id=link.invocation_id,
            action_id=link.action_id,
        )
        request_path = directory / "request.json"
        try:
            request = ActionRequest.model_validate(
                await read_json_async(request_path),
            )
        except FileNotFoundError as exc:
            raise ActionConflictError(
                "action approval link has no durable request",
            ) from exc
        ActionRecord(request=request, approval_links=(link,))
        await self._append_immutable(
            directory / "approval-links" / f"{link.approval_id}.json",
            link,
            timestamp_field="linked_at",
        )

    async def get(
        self,
        action_id: UUID,
        *,
        invocation_id: UUID,
        conversation_id: str | None,
    ) -> ActionRecord | None:
        """Read one exact Action record for recovery admission."""
        directory = self._action_dir(
            conversation_id=conversation_id,
            invocation_id=invocation_id,
            action_id=action_id,
        )
        try:
            request = ActionRequest.model_validate(
                await read_json_async(directory / "request.json"),
            )
        except FileNotFoundError:
            return None
        if (
            request.action_id != action_id
            or request.invocation_id != invocation_id
            or request.conversation_id != conversation_id
        ):
            raise ActionConflictError(
                "Action record path does not match its identity",
            )
        try:
            result = ActionResult.model_validate(
                await read_json_async(directory / "result.json"),
            )
        except FileNotFoundError:
            result = None
        return ActionRecord(
            request=request,
            approval_links=await run_sync_io(
                self._load_approval_links,
                directory,
            ),
            result=result,
        )

    @staticmethod
    def _load_approval_links(
        directory: Path,
    ) -> tuple[ActionApprovalLink, ...]:
        links = [
            ActionApprovalLink.model_validate_json(
                path.read_text(encoding="utf-8"),
            )
            for path in sorted((directory / "approval-links").glob("*.json"))
        ]
        return tuple(links)

    def _list_sync(
        self,
        conversation_id: str,
        limit: int | None,
    ) -> list[ActionRecord]:
        root = self._root / self._conversation_key(conversation_id)
        records: list[ActionRecord] = []
        for request_path in root.glob("*/*/request.json"):
            request = ActionRequest.model_validate_json(
                request_path.read_text(encoding="utf-8"),
            )
            result_path = request_path.with_name("result.json")
            approval_links = self._load_approval_links(request_path.parent)
            result = (
                ActionResult.model_validate_json(
                    result_path.read_text(encoding="utf-8"),
                )
                if result_path.exists()
                else None
            )
            records.append(
                ActionRecord(
                    request=request,
                    approval_links=approval_links,
                    result=result,
                ),
            )
        records.sort(
            key=lambda item: item.request.requested_at,
            reverse=True,
        )
        return records if limit is None else records[:limit]

    async def list_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 100,
    ) -> Sequence[ActionRecord]:
        """Return newest action records for one ChatSpec identity."""
        if not conversation_id.strip():
            raise ValueError("conversation_id cannot be empty")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        return await run_sync_io(self._list_sync, conversation_id, limit)

    async def scan_for_conversation(
        self,
        conversation_id: str,
    ) -> Sequence[ActionRecord]:
        """Scan all Lite records for a derived-index rebuild."""
        if not conversation_id.strip():
            raise ValueError("conversation_id cannot be empty")
        return await run_sync_io(self._list_sync, conversation_id, None)


class RuntimeActionRecorder:
    """Bridge runtime executors to one privacy-safe Action Plane."""

    def __init__(
        self,
        scope: InvocationScope,
        store: ActionStore,
        *,
        tool_owners: dict[str, str] | None = None,
        retry_policy: ActionRetryPolicy | None = None,
        retry_input_store: ActionRetryInputStore | None = None,
        retry_continuation_store: (
            ActionRetryContinuationStore | None
        ) = None,
        retry_of: ActionRecord | None = None,
        tool_selection: ToolSelection | None = None,
        provider_execution_digests: dict[str, str] | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._scope = scope
        self._store = store
        self._tool_owners = dict(tool_owners or {})
        self._retry_policy = retry_policy or ActionRetryPolicy()
        self._retry_input_store = retry_input_store
        self._retry_continuation_store = retry_continuation_store
        self._retry_of = retry_of
        self._tool_selection = tool_selection
        self._provider_execution_digests = dict(
            provider_execution_digests or {},
        )
        self._clock = clock

    def bind_tool_owners(self, tool_owners: dict[str, str] | None) -> None:
        """Bind the final provider catalog before any tool can execute."""
        self._tool_owners = dict(tool_owners or {})

    def bind_tool_selection(self, selection: ToolSelection) -> None:
        """Bind exact provider filters before any tool can execute."""
        self._tool_selection = selection

    def bind_provider_execution_digests(
        self,
        digests: dict[str, str] | None,
    ) -> None:
        """Bind content-safe provider configuration identities."""
        self._provider_execution_digests = dict(digests or {})

    @staticmethod
    def active_request(
        context: ToolCallContext,
    ) -> ActionRequest | None:
        """Return the Action already bound to one supervised call."""
        request = context.extra.get(ACTION_REQUEST_CONTEXT_KEY)
        return request if isinstance(request, ActionRequest) else None

    async def committed_record(
        self,
        context: ToolCallContext,
    ) -> ActionRecord | None:
        """Return exact terminal evidence for one supervised call."""
        request = self.active_request(context)
        if request is None:
            return None
        record = await self._store.get(
            request.action_id,
            invocation_id=request.invocation_id,
            conversation_id=request.conversation_id,
        )
        if record is None or record.result is None:
            return None
        return record

    def _request(
        self,
        context: ToolCallContext,
        *,
        capability_id: str,
        kind: ActionKind,
        action_name: str,
        effect: ToolEffect,
        risk: RiskLevel,
        reversible: bool,
        policy_decision: str,
        approval_id: UUID | None,
        environment_ref: EnvironmentRef | None = None,
        idempotency_mode: ActionIdempotencyMode = (
            ActionIdempotencyMode.UNDECLARED
        ),
        retry_of: ActionRequest | None = None,
    ) -> ActionRequest:
        raw_input = context.extra.get("tool_input")
        arguments = _json_value(
            raw_input if isinstance(raw_input, dict) else {},
        )
        redacted_arguments = _minimize_arguments(
            redact_payload(arguments),
        )
        action_id = self.action_id(context, kind=kind)
        retry_root_action_id = None
        retry_of_action_id = None
        attempt = 1
        if retry_of is not None:
            retry_root_action_id = (
                retry_of.retry_root_action_id or retry_of.action_id
            )
            retry_of_action_id = retry_of.action_id
            attempt = retry_of.attempt + 1
        generated_idempotency_key = (
            f"{kind.value}:{self._scope.invocation_id}:"
            f"{context.tool_call_id}"
        )
        return ActionRequest(
            action_id=action_id,
            invocation_id=self._scope.invocation_id,
            correlation_id=(
                self._scope.correlation_id or self._scope.invocation_id
            ),
            agent_id=self._scope.agent_id,
            chat_id=self._scope.chat_id,
            registry_generation=self._scope.registry_generation,
            environment_ref=environment_ref
            or (
                EnvironmentRef(
                    resolution_id=(
                        self._scope.environment_resolution.resolution_id
                    ),
                    contract_id=(
                        self._scope.environment_resolution.contract_id
                    ),
                    contract_version=(
                        self._scope.environment_resolution.contract_version
                    ),
                    resolver_id=(
                        self._scope.environment_resolution.resolver_id
                    ),
                )
                if self._scope.environment_resolution is not None
                else None
            ),
            capability_id=capability_id,
            tool_selection=(
                retry_of.tool_selection
                if retry_of is not None
                else self._tool_selection
            ),
            provider_execution_digest=(
                retry_of.provider_execution_digest
                if retry_of is not None
                else self._provider_execution_digests.get(capability_id)
            ),
            kind=kind,
            action_name=action_name,
            arguments=arguments,
            redacted_arguments=redacted_arguments,
            arguments_hash=_sha256(redacted_arguments),
            effect=effect,
            risk=risk,
            reversible=reversible,
            idempotency_mode=idempotency_mode,
            retry_policy=(
                retry_of.retry_policy
                if retry_of is not None
                else self._retry_policy
            ),
            idempotency_key=(
                retry_of.idempotency_key
                if retry_of is not None
                and idempotency_mode
                is ActionIdempotencyMode.EXECUTOR_ENFORCED
                else generated_idempotency_key
            ),
            executor_item_id=context.tool_call_id,
            retry_root_action_id=retry_root_action_id,
            retry_of_action_id=retry_of_action_id,
            attempt=attempt,
            approval_id=approval_id,
            admission=_action_admission(
                kind=kind,
                capability_id=capability_id,
                policy_decision=policy_decision,
            ),
            policy_decision=policy_decision or "unknown",
            requested_at=self._clock(),
        )

    def action_id(
        self,
        context: ToolCallContext,
        *,
        kind: ActionKind,
    ) -> UUID:
        """Return the deterministic identity used before request storage."""
        return uuid5(
            self._scope.invocation_id,
            f"{kind.value}:{context.tool_call_id}",
        )

    async def begin(
        self,
        context: ToolCallContext,
        *,
        effect: ToolEffect,
        policy_decision: str,
        approval_id: UUID | None = None,
        environment_ref: EnvironmentRef | None = None,
        kind: ActionKind | None = None,
        idempotency_mode: ActionIdempotencyMode = (
            ActionIdempotencyMode.UNDECLARED
        ),
    ) -> ActionRequest:
        """Persist and bind a request before the executor is called."""
        existing = context.extra.get(ACTION_REQUEST_CONTEXT_KEY)
        if isinstance(existing, ActionRequest):
            return existing
        if self._retry_of is not None:
            return await self.begin_retry(context, self._retry_of)
        request = self._request(
            context,
            capability_id=self._tool_owners.get(
                context.tool_name,
                "qwenpaw.system.workspace-tools",
            ),
            kind=(
                kind
                or (
                    ActionKind.SHELL
                    if effect is ToolEffect.PROCESS
                    else ActionKind.TOOL
                )
            ),
            action_name=context.tool_name,
            effect=effect,
            risk=_risk_for_effect(effect),
            reversible=effect is ToolEffect.NONE,
            policy_decision=policy_decision,
            approval_id=approval_id,
            environment_ref=environment_ref,
            idempotency_mode=idempotency_mode,
        )
        try:
            await self._store.begin(request)
            context.extra[ACTION_REQUEST_CONTEXT_KEY] = request
            context.governance_metadata[ACTION_REQUEST_STATE_KEY] = "recorded"
            context.governance_metadata["action_id"] = str(request.action_id)
            if approval_id is not None:
                await self.link_approval(
                    context,
                    approval_id,
                    ApprovalSource.TOOL,
                )
        except Exception as exc:
            context.governance_metadata[ACTION_REQUEST_STATE_KEY] = "failed"
            raise ActionRequestPersistenceError(
                "tool was not executed because action intent could not be "
                "recorded",
            ) from exc
        return request

    async def begin_driver(
        self,
        context: ToolCallContext,
        definition: DriverToolDefinition,
    ) -> ActionRequest:
        """Persist a Driver-owned request before invoking its capability."""
        return await self.begin_driver_capability(
            context,
            provider_id=definition.provider_id,
            capability_id=definition.capability_id,
            effect=definition.effect,
            risk=definition.risk,
            reversible=definition.reversible,
            idempotency_mode=definition.idempotency_mode,
        )

    async def begin_driver_capability(
        self,
        context: ToolCallContext,
        *,
        provider_id: str,
        capability_id: str,
        effect: ToolEffect = ToolEffect.EXTERNAL_WRITE,
        risk: RiskLevel = RiskLevel.HIGH,
        reversible: bool = False,
        idempotency_mode: ActionIdempotencyMode = (
            ActionIdempotencyMode.UNDECLARED
        ),
    ) -> ActionRequest:
        """Persist a Driver capability across provider and legacy paths."""
        existing = context.extra.get(ACTION_REQUEST_CONTEXT_KEY)
        if isinstance(existing, ActionRequest):
            return existing
        request = self._request(
            context,
            capability_id=provider_id,
            kind=ActionKind.DRIVER,
            action_name=capability_id,
            effect=effect,
            risk=risk,
            reversible=reversible,
            policy_decision="driver_policy",
            approval_id=None,
            idempotency_mode=idempotency_mode,
        )
        try:
            await self._store.begin(request)
        except Exception as exc:
            context.governance_metadata[ACTION_REQUEST_STATE_KEY] = "failed"
            raise ActionRequestPersistenceError(
                "driver was not executed because action intent could not be "
                "recorded",
            ) from exc
        context.extra[ACTION_REQUEST_CONTEXT_KEY] = request
        context.governance_metadata[ACTION_REQUEST_STATE_KEY] = "recorded"
        context.governance_metadata["action_id"] = str(request.action_id)
        return request

    def _validate_retry_executor(
        self,
        context: ToolCallContext,
        previous_request: ActionRequest,
    ) -> None:
        if context.tool_name != previous_request.action_name:
            raise ActionConflictError("retry action tool name mismatch")
        if (
            previous_request.tool_selection is not None
            and previous_request.tool_selection != self._tool_selection
        ):
            raise ActionConflictError("retry action tool selection mismatch")
        previous_digest = previous_request.provider_execution_digest
        if (
            previous_digest is not None
            and self._provider_execution_digests.get(
                previous_request.capability_id,
            )
            != previous_digest
        ):
            raise ActionConflictError(
                "retry action provider configuration mismatch",
            )

    async def begin_retry(
        self,
        context: ToolCallContext,
        previous: ActionRecord,
    ) -> ActionRequest:
        """Persist a new Action attempt after exact Host admission."""
        previous_result = previous.result
        previous_request = previous.request
        self._validate_retry_executor(context, previous_request)
        retry_decision = (
            previous_result.retry_decision
            if previous_result is not None
            else None
        )
        if (
            previous_result is None
            or retry_decision is None
            or retry_decision.disposition
            is not ActionRetryDisposition.RETRY_FROM_NEW_ACTION
        ):
            raise ActionConflictError(
                "previous action is not admitted for retry",
            )
        next_attempt = previous_request.attempt + 1
        if (
            retry_decision.next_attempt is not None
            and retry_decision.next_attempt != next_attempt
        ):
            raise ActionConflictError("retry action attempt mismatch")
        if next_attempt > retry_decision.max_attempts:
            raise ActionConflictError("retry action budget exhausted")
        retry_after_seconds = retry_decision.retry_after_seconds or 0
        retry_at = previous_result.completed_at + timedelta(
            seconds=retry_after_seconds,
        )
        if self._clock() < retry_at:
            raise ActionRetryNotReadyError(retry_at)
        if previous_request.chat_id != self._scope.chat_id:
            raise ActionConflictError("retry action conversation mismatch")
        expected_correlation = (
            self._scope.correlation_id or self._scope.invocation_id
        )
        if previous_request.correlation_id != expected_correlation:
            raise ActionConflictError("retry action correlation mismatch")
        if (
            previous_request.registry_generation
            != self._scope.registry_generation
        ):
            raise ActionConflictError("retry action generation mismatch")
        previous_environment = previous_request.environment_ref
        current_resolution = self._scope.environment_resolution
        if (previous_environment is None) != (current_resolution is None):
            raise ActionConflictError("retry action environment mismatch")
        if (
            previous_environment is not None
            and current_resolution is not None
        ):
            previous_contract = (
                previous_environment.contract_id,
                previous_environment.contract_version,
                previous_environment.resolver_id,
            )
            current_contract = (
                current_resolution.contract_id,
                current_resolution.contract_version,
                current_resolution.resolver_id,
            )
            if previous_contract != current_contract:
                raise ActionConflictError(
                    "retry action environment contract changed",
                )
        request = self._request(
            context,
            capability_id=previous_request.capability_id,
            kind=previous_request.kind,
            action_name=previous_request.action_name,
            effect=previous_request.effect,
            risk=previous_request.risk,
            reversible=previous_request.reversible,
            policy_decision="retry_policy",
            approval_id=None,
            environment_ref=None,
            idempotency_mode=previous_request.idempotency_mode,
            retry_of=previous_request,
        )
        if request.arguments_hash != previous_request.arguments_hash:
            raise ActionConflictError("retry action arguments changed")
        try:
            await self._store.begin(request)
        except Exception as exc:
            context.governance_metadata[ACTION_REQUEST_STATE_KEY] = "failed"
            raise ActionRequestPersistenceError(
                "retry was not executed because its Action intent could "
                "not be recorded",
            ) from exc
        context.extra[ACTION_REQUEST_CONTEXT_KEY] = request
        context.governance_metadata[ACTION_REQUEST_STATE_KEY] = "recorded"
        context.governance_metadata["action_id"] = str(request.action_id)
        return request

    async def begin_harness_remote(
        self,
        context: ToolCallContext,
        *,
        capability_id: str,
        effect: ToolEffect,
        risk: RiskLevel,
        reversible: bool,
        policy_decision: str,
        environment_ref: EnvironmentRef,
    ) -> ActionRequest:
        """Persist a provider-reported Harness action at its first boundary."""
        existing = context.extra.get(ACTION_REQUEST_CONTEXT_KEY)
        if isinstance(existing, ActionRequest):
            return existing
        request = self._request(
            context,
            capability_id=capability_id,
            kind=ActionKind.HARNESS_REMOTE,
            action_name=context.tool_name,
            effect=effect,
            risk=risk,
            reversible=reversible,
            policy_decision=policy_decision,
            approval_id=None,
            environment_ref=environment_ref,
        )
        try:
            await self._store.begin(request)
        except Exception as exc:
            context.governance_metadata[ACTION_REQUEST_STATE_KEY] = "failed"
            raise ActionRequestPersistenceError(
                "Harness action was not accepted because its intent could "
                "not be recorded",
            ) from exc
        context.extra[ACTION_REQUEST_CONTEXT_KEY] = request
        context.governance_metadata[ACTION_REQUEST_STATE_KEY] = "recorded"
        context.governance_metadata["action_id"] = str(request.action_id)
        return request

    async def link_approval(
        self,
        context: ToolCallContext,
        approval_id: UUID,
        source: ApprovalSource,
    ) -> ActionApprovalLink:
        """Attach a dynamically discovered approval to the active action."""
        request = context.extra.get(ACTION_REQUEST_CONTEXT_KEY)
        if not isinstance(request, ActionRequest):
            raise ActionConflictError("approval has no active action request")
        link = ActionApprovalLink(
            action_id=request.action_id,
            invocation_id=request.invocation_id,
            conversation_id=request.conversation_id,
            approval_id=approval_id,
            source=source,
        )
        await self._store.link_approval(link)
        raw_ids = context.governance_metadata.setdefault("approval_ids", [])
        if str(approval_id) not in raw_ids:
            raw_ids.append(str(approval_id))
        return link

    async def _ensure_request(
        self,
        context: ToolCallContext,
        response: ToolResponse,
    ) -> ActionRequest:
        existing = context.extra.get(ACTION_REQUEST_CONTEXT_KEY)
        if isinstance(existing, ActionRequest):
            return existing
        from ..governance.tool_registry import DEFAULT_REGISTRY

        effect = ToolEffect(DEFAULT_REGISTRY.get_effect(context.tool_name))
        policy_decision = (
            "deny"
            if response.state is ToolResultState.DENIED
            else "unobserved"
        )
        return await self.begin(
            context,
            effect=effect,
            policy_decision=policy_decision,
        )

    @staticmethod
    def _status(response: ToolResponse) -> ActionStatus:
        if response.state is ToolResultState.SUCCESS:
            if (response.metadata or {}).get(TOOL_ARTIFACT_ERRORS_KEY):
                return ActionStatus.PARTIAL
            return ActionStatus.SUCCEEDED
        if response.state is ToolResultState.DENIED:
            return ActionStatus.DENIED
        if response.state is ToolResultState.INTERRUPTED:
            return ActionStatus.CANCELLED
        return ActionStatus.FAILED

    @staticmethod
    def _artifact_refs(
        response: ToolResponse,
    ) -> tuple[tuple[ArtifactRef, ...], tuple[EvidenceRef, ...]]:
        artifacts: list[ArtifactRef] = []
        evidence: list[EvidenceRef] = []
        links = (response.metadata or {}).get(TOOL_ARTIFACT_LINKS_KEY, [])
        if not isinstance(links, list):
            return (), ()
        for link in links:
            if not isinstance(link, dict):
                continue
            try:
                artifacts.append(
                    ArtifactRef.model_validate(link["artifact_ref"]),
                )
                evidence.append(
                    EvidenceRef.model_validate(link["evidence_ref"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
        return tuple(artifacts), tuple(evidence)

    async def _bind_retry_input(
        self,
        request: ActionRequest,
        decision: ActionRetryDecision,
    ) -> ActionRetryDecision:
        """Publish a retry decision only after private input is durable."""
        if (
            decision.disposition
            is not ActionRetryDisposition.RETRY_FROM_NEW_ACTION
            or self._retry_input_store is None
        ):
            return decision
        try:
            checkpoint = await self._retry_input_store.save(
                request,
                decision,
            )
        except Exception:
            logger.exception(
                "Action retry input could not be persisted for %s",
                request.action_id,
            )
            return ActionRetryDecision(
                disposition=ActionRetryDisposition.FORBIDDEN,
                reason=ActionRetryReason.RETRY_INPUT_UNAVAILABLE,
                provider_retryable=decision.provider_retryable,
                max_attempts=decision.max_attempts,
            )
        return decision.model_copy(
            update={"input_checkpoint_id": checkpoint.checkpoint_id},
        )

    async def _publish_retry_continuation(
        self,
        result: ActionResult,
    ) -> None:
        """Publish retry work only after immutable result persistence."""
        decision = result.retry_decision
        checkpoint_id = (
            decision.input_checkpoint_id
            if decision is not None
            else None
        )
        if (
            checkpoint_id is None
            or self._retry_input_store is None
            or self._retry_continuation_store is None
        ):
            return
        try:
            checkpoint, _ = await self._retry_input_store.load(
                checkpoint_id,
            )
            await self._retry_continuation_store.defer(
                checkpoint,
                result,
            )
        except Exception:
            logger.exception(
                "Action retry continuation could not be published for %s",
                result.action_id,
            )

    async def complete(
        self,
        response: ToolResponse,
        context: ToolCallContext,
    ) -> ToolResponse:
        """Commit terminal evidence after artifact publication."""
        if (
            context.governance_metadata.get(ACTION_REQUEST_STATE_KEY)
            == "failed"
        ):
            return response
        request = await self._ensure_request(context, response)
        response.metadata[ACTION_RESULT_CONTEXT_KEY] = str(
            request.action_id,
        )
        status = self._status(response)
        artifacts, evidence = self._artifact_refs(response)
        side_effect_status = _side_effect_status(request.effect, status)
        retry_decision = _action_retry_decision(
            request,
            status=status,
            side_effect_status=side_effect_status,
            provider_retryable=(
                (response.metadata or {}).get(
                    ACTION_RETRY_HINT_METADATA_KEY,
                )
                is True
            ),
        )
        retry_decision = await self._bind_retry_input(
            request,
            retry_decision,
        )
        response.metadata[ACTION_RETRY_DECISION_METADATA_KEY] = (
            retry_decision.model_dump(mode="json")
        )
        safe_observation = {
            "state": response.state.value,
            "retry_decision": retry_decision.model_dump(mode="json"),
            "artifact_refs": [
                {
                    "artifact_id": str(artifact.artifact_id),
                    "content_hash": artifact.content_hash,
                }
                for artifact in artifacts
            ],
            "evidence_ids": [str(item.evidence_id) for item in evidence],
            "artifact_errors": [
                str(item.get("code") or "unknown")
                for item in (
                    (response.metadata or {}).get(
                        TOOL_ARTIFACT_ERRORS_KEY,
                        [],
                    )
                )
                if isinstance(item, dict)
            ],
        }
        result = ActionResult(
            action_id=request.action_id,
            invocation_id=request.invocation_id,
            conversation_id=request.conversation_id,
            status=status,
            observation=response,
            observation_digest=_sha256(safe_observation),
            artifact_refs=artifacts,
            evidence_refs=evidence,
            approval_ids=tuple(
                UUID(value)
                for value in context.governance_metadata.get(
                    "approval_ids",
                    [],
                )
            ),
            error_code=(
                ""
                if status is ActionStatus.SUCCEEDED
                else (
                    "artifact_capture_partial"
                    if status is ActionStatus.PARTIAL
                    else response.state.value
                )
            ),
            retryable=(
                retry_decision.disposition
                is ActionRetryDisposition.RETRY_FROM_NEW_ACTION
            ),
            retry_decision=retry_decision,
            side_effect_status=side_effect_status,
            completed_at=self._clock(),
        )
        committed_item = CommittedActionItem(
            action_id=request.action_id,
            invocation_id=request.invocation_id,
            conversation_id=request.conversation_id,
            executor_item_id=context.tool_call_id,
            observation_digest=result.observation_digest,
        )
        response.metadata[COMMITTED_ACTION_ITEM_METADATA_KEY] = (
            committed_item.model_dump(mode="json")
        )
        prepare_background = context.extra.get(
            "_prepare_background_action_context",
        )
        if callable(prepare_background):
            await prepare_background(
                response,
                context,
                committed_item,
            )
        try:
            await self._store.complete(result)
        except Exception as exc:
            raise ActionResultPersistenceError(
                "tool executed but its result could not be durably verified",
            ) from exc
        await self._publish_retry_continuation(result)
        return response

    async def complete_harness_remote(
        self,
        context: ToolCallContext,
        *,
        status: ActionStatus,
        error_code: str = "",
        retryable: bool = False,
    ) -> CommittedActionItem:
        """Commit content-free terminal evidence for a Harness action."""
        request = context.extra.get(ACTION_REQUEST_CONTEXT_KEY)
        if not isinstance(request, ActionRequest):
            raise ActionConflictError(
                "Harness result has no active action request",
            )
        approval_ids = tuple(
            UUID(value)
            for value in context.governance_metadata.get(
                "approval_ids",
                [],
            )
        )
        side_effect_status = _side_effect_status(request.effect, status)
        retry_decision = _action_retry_decision(
            request,
            status=status,
            side_effect_status=side_effect_status,
            provider_retryable=retryable,
        )
        retry_decision = await self._bind_retry_input(
            request,
            retry_decision,
        )
        result = ActionResult(
            action_id=request.action_id,
            invocation_id=request.invocation_id,
            conversation_id=request.conversation_id,
            status=status,
            observation_digest=_sha256(
                {
                    "status": status.value,
                    "error_code": error_code,
                    "approval_ids": [str(value) for value in approval_ids],
                    "retry_decision": retry_decision.model_dump(
                        mode="json",
                    ),
                },
            ),
            approval_ids=approval_ids,
            error_code=error_code,
            retryable=(
                retry_decision.disposition
                is ActionRetryDisposition.RETRY_FROM_NEW_ACTION
            ),
            retry_decision=retry_decision,
            side_effect_status=side_effect_status,
            completed_at=self._clock(),
        )
        try:
            await self._store.complete(result)
        except Exception as exc:
            raise ActionResultPersistenceError(
                "Harness action completed but its result could not be "
                "durably verified",
            ) from exc
        await self._publish_retry_continuation(result)
        return CommittedActionItem(
            action_id=request.action_id,
            invocation_id=request.invocation_id,
            conversation_id=request.conversation_id,
            executor_item_id=context.tool_call_id,
            observation_digest=result.observation_digest,
        )


def lite_action_store(workspace_dir: Path) -> FilesystemActionStore:
    """Return the Lite action evidence store for one agent workspace."""
    return FilesystemActionStore(workspace_dir)


async def link_active_action_approval(
    request_context: dict[str, Any],
    approval_id: UUID,
    source: ApprovalSource,
) -> None:
    """Link approval when a runtime action is active; no-op for legacy use."""
    recorder = request_context.get("_action_recorder")
    if not isinstance(recorder, RuntimeActionRecorder):
        return
    from ..tool_calls._ctxvars import get_call_context

    context = get_call_context()
    if context is None:
        return
    await recorder.link_approval(context, approval_id, source)


__all__ = [
    "ACTION_REQUEST_CONTEXT_KEY",
    "ACTION_REQUEST_STATE_KEY",
    "ACTION_RETRY_DECISION_METADATA_KEY",
    "ACTION_RETRY_HINT_METADATA_KEY",
    "ACTION_RESULT_CONTEXT_KEY",
    "COMMITTED_ACTION_ITEM_METADATA_KEY",
    "ActionConflictError",
    "ActionRetryNotReadyError",
    "ActionRequestPersistenceError",
    "ActionResultPersistenceError",
    "FilesystemActionStore",
    "RuntimeActionRecorder",
    "current_action_execution",
    "assess_model_step_reconciliation",
    "link_active_action_approval",
    "lite_action_store",
    "model_step_action_call_ids",
    "model_step_action_context_bindings",
    "model_step_committed_action_items",
    "model_step_action_evidence_digest",
    "model_step_reconciliation_evidence_digest",
    "public_action_record",
]
