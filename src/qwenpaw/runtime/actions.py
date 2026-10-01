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
    ActionKind,
    ActionRecord,
    ActionRequest,
    ActionResult,
    ActionStore,
    ActionStatus,
    ArtifactRef,
    EvidenceRef,
    InvocationScope,
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
_BULK_ARGUMENT_NAMES = frozenset(
    {
        "audio",
        "body",
        "bytes",
        "content",
        "data",
        "document",
        "image",
        "payload",
        "prompt",
        "text",
    },
)


class ActionConflictError(RuntimeError):
    """Raised when one action identity has conflicting durable evidence."""


class ActionRequestPersistenceError(RuntimeError):
    """Raised before execution when action intent cannot be stored."""


class ActionResultPersistenceError(RuntimeError):
    """Raised after execution when its terminal evidence cannot be stored."""


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
        model: ActionRequest | ActionResult,
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
        ActionRecord(request=request, result=result)
        await self._append_immutable(
            directory / "result.json",
            result,
            timestamp_field="completed_at",
        )

    def _list_sync(
        self,
        conversation_id: str,
        limit: int,
    ) -> list[ActionRecord]:
        root = self._root / self._conversation_key(conversation_id)
        records: list[ActionRecord] = []
        for request_path in root.glob("*/*/request.json"):
            request = ActionRequest.model_validate_json(
                request_path.read_text(encoding="utf-8"),
            )
            result_path = request_path.with_name("result.json")
            result = (
                ActionResult.model_validate_json(
                    result_path.read_text(encoding="utf-8"),
                )
                if result_path.exists()
                else None
            )
            records.append(ActionRecord(request=request, result=result))
        records.sort(
            key=lambda item: item.request.requested_at,
            reverse=True,
        )
        return records[:limit]

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


class ToolActionRecorder:
    """Bridge governed built-in and plugin tools to the Action Plane."""

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

    def _request(
        self,
        context: ToolCallContext,
        *,
        effect: ToolEffect,
        policy_decision: str,
        approval_id: UUID | None,
    ) -> ActionRequest:
        raw_input = context.extra.get("tool_input")
        arguments = _json_value(
            raw_input if isinstance(raw_input, dict) else {},
        )
        redacted_arguments = _minimize_arguments(
            redact_payload(arguments),
        )
        provider_id = self._tool_owners.get(
            context.tool_name,
            "qwenpaw.system.workspace-tools",
        )
        action_id = uuid5(
            self._scope.invocation_id,
            f"tool:{context.tool_call_id}",
        )
        return ActionRequest(
            action_id=action_id,
            invocation_id=self._scope.invocation_id,
            correlation_id=(
                self._scope.correlation_id or self._scope.invocation_id
            ),
            conversation_id=self._scope.conversation_id,
            registry_generation=self._scope.registry_generation,
            capability_id=provider_id,
            kind=(
                ActionKind.SHELL
                if effect is ToolEffect.PROCESS
                else ActionKind.TOOL
            ),
            action_name=context.tool_name,
            arguments=arguments,
            redacted_arguments=redacted_arguments,
            arguments_hash=_sha256(redacted_arguments),
            effect=effect,
            risk=_risk_for_effect(effect),
            reversible=effect is ToolEffect.NONE,
            idempotency_key=(
                f"tool:{self._scope.invocation_id}:{context.tool_call_id}"
            ),
            approval_id=approval_id,
            policy_decision=policy_decision or "unknown",
        )

    async def begin(
        self,
        context: ToolCallContext,
        *,
        effect: ToolEffect,
        policy_decision: str,
        approval_id: UUID | None = None,
    ) -> ActionRequest:
        """Persist and bind a request before the executor is called."""
        existing = context.extra.get(ACTION_REQUEST_CONTEXT_KEY)
        if isinstance(existing, ActionRequest):
            return existing
        request = self._request(
            context,
            effect=effect,
            policy_decision=policy_decision,
            approval_id=approval_id,
        )
        try:
            await self._store.begin(request)
        except Exception as exc:
            context.governance_metadata[ACTION_REQUEST_STATE_KEY] = "failed"
            raise ActionRequestPersistenceError(
                "tool was not executed because action intent could not be "
                "recorded",
            ) from exc
        context.extra[ACTION_REQUEST_CONTEXT_KEY] = request
        context.governance_metadata[ACTION_REQUEST_STATE_KEY] = "recorded"
        context.governance_metadata["action_id"] = str(request.action_id)
        return request

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
        return response


def lite_action_store(workspace_dir: Path) -> FilesystemActionStore:
    """Return the Lite action evidence store for one agent workspace."""
    return FilesystemActionStore(workspace_dir)


__all__ = [
    "ACTION_REQUEST_CONTEXT_KEY",
    "ACTION_REQUEST_STATE_KEY",
    "ActionConflictError",
    "ActionRequestPersistenceError",
    "ActionResultPersistenceError",
    "FilesystemActionStore",
    "ToolActionRecorder",
    "lite_action_store",
]
