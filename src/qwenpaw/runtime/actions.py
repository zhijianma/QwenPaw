# -*- coding: utf-8 -*-
"""Unified, privacy-safe action evidence for the Lite runtime."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Sequence
from uuid import UUID, uuid5

from agentscope.message import ToolResultState
from agentscope.tool import ToolResponse

from ..kernel import (
    ActionApprovalLink,
    ActionKind,
    ActionRecord,
    ActionRequest,
    ActionResult,
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
)
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
        prefix = f"{request.kind.value}:{invocation_id}:"
        if not request.idempotency_key.startswith(prefix):
            return None
        call_id = request.idempotency_key[len(prefix) :]
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
        prefix = f"{request.kind.value}:{invocation_id}:"
        call_id = request.idempotency_key[len(prefix) :]
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
        prefix = f"{request.kind.value}:{invocation_id}:"
        if not request.idempotency_key.startswith(prefix):
            return None
        executor_item_id = request.idempotency_key[len(prefix) :]
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
    if status in {ActionStatus.FAILED, ActionStatus.DENIED}:
        return SideEffectStatus.FAILED
    return SideEffectStatus.UNCERTAIN


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
    ) -> None:
        self._scope = scope
        self._store = store
        self._tool_owners = dict(tool_owners or {})

    def bind_tool_owners(self, tool_owners: dict[str, str] | None) -> None:
        """Bind the final provider catalog before any tool can execute."""
        self._tool_owners = dict(tool_owners or {})

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
    ) -> ActionRequest:
        raw_input = context.extra.get("tool_input")
        arguments = _json_value(
            raw_input if isinstance(raw_input, dict) else {},
        )
        redacted_arguments = _minimize_arguments(
            redact_payload(arguments),
        )
        action_id = self.action_id(context, kind=kind)
        return ActionRequest(
            action_id=action_id,
            invocation_id=self._scope.invocation_id,
            correlation_id=(
                self._scope.correlation_id or self._scope.invocation_id
            ),
            conversation_id=self._scope.conversation_id,
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
            kind=kind,
            action_name=action_name,
            arguments=arguments,
            redacted_arguments=redacted_arguments,
            arguments_hash=_sha256(redacted_arguments),
            effect=effect,
            risk=risk,
            reversible=reversible,
            idempotency_key=(
                f"{kind.value}:{self._scope.invocation_id}:"
                f"{context.tool_call_id}"
            ),
            approval_id=approval_id,
            policy_decision=policy_decision or "unknown",
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
    ) -> ActionRequest:
        """Persist and bind a request before the executor is called."""
        existing = context.extra.get(ACTION_REQUEST_CONTEXT_KEY)
        if isinstance(existing, ActionRequest):
            return existing
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
        safe_observation = {
            "state": response.state.value,
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
            retryable=False,
            side_effect_status=_side_effect_status(request.effect, status),
        )
        try:
            await self._store.complete(result)
        except Exception as exc:
            raise ActionResultPersistenceError(
                "tool executed but its result could not be durably verified",
            ) from exc
        response.metadata[COMMITTED_ACTION_ITEM_METADATA_KEY] = (
            CommittedActionItem(
                action_id=request.action_id,
                invocation_id=request.invocation_id,
                conversation_id=request.conversation_id,
                executor_item_id=context.tool_call_id,
                observation_digest=result.observation_digest,
            ).model_dump(mode="json")
        )
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
                },
            ),
            approval_ids=approval_ids,
            error_code=error_code,
            retryable=retryable,
            side_effect_status=_side_effect_status(request.effect, status),
        )
        try:
            await self._store.complete(result)
        except Exception as exc:
            raise ActionResultPersistenceError(
                "Harness action completed but its result could not be "
                "durably verified",
            ) from exc
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
    "ACTION_RESULT_CONTEXT_KEY",
    "COMMITTED_ACTION_ITEM_METADATA_KEY",
    "ActionConflictError",
    "ActionRequestPersistenceError",
    "ActionResultPersistenceError",
    "FilesystemActionStore",
    "RuntimeActionRecorder",
    "assess_model_step_reconciliation",
    "link_active_action_approval",
    "lite_action_store",
    "model_step_action_call_ids",
    "model_step_action_context_bindings",
    "model_step_committed_action_items",
    "model_step_action_evidence_digest",
    "public_action_record",
]
