# -*- coding: utf-8 -*-
"""Durable route and attempt evidence for concrete provider model calls."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Literal, TypeVar
from uuid import UUID, uuid5

from ..kernel import (
    ContextManifest,
    InvocationScope,
    ModelCallAttempt,
    ModelCallRecord,
    ModelCallResult,
    ModelCallStatus,
    ModelFailureClass,
    ModelOutputBoundary,
    ModelRecoveryDisposition,
    ModelResourceRecoveryPort,
    ModelResourceWait,
    ModelCallStore,
    ModelRouteReason,
    ModelStepContinuation,
    ModelStepContinuationStatus,
    RouteDecision,
)
from ..utils.io_utils import (
    get_path_lock,
    read_json_async,
    run_sync_io,
    write_json_atomic_async,
)

_Result = TypeVar("_Result")
logger = logging.getLogger(__name__)
_CURRENT_MODEL_CALL: ContextVar["ModelCallSession | None"] = ContextVar(
    "qwenpaw_model_call_session",
    default=None,
)


class ModelCallConflictError(RuntimeError):
    """Raised when durable model-call evidence conflicts."""


class ModelCallPersistenceError(RuntimeError):
    """Raised when model-call evidence cannot be durably recorded."""


class ModelStepRecoveryError(RuntimeError):
    """Signal that a partial model step reached a durable boundary."""

    def __init__(
        self,
        continuation_id: UUID,
        status: ModelStepContinuationStatus,
    ) -> None:
        self.continuation_id = continuation_id
        self.status = status
        super().__init__(
            f"model step recovery persisted as {status.value}",
        )


class FilesystemModelCallStore:
    """Owner-only Lite store for immutable model routing evidence."""

    def __init__(self, workspace_dir: Path) -> None:
        self._root = Path(workspace_dir) / ".qwenpaw" / "lite" / "model-calls"

    @staticmethod
    def _owner_key(conversation_id: str | None, invocation_id: object) -> str:
        owner = conversation_id or f"invocation:{invocation_id}"
        return hashlib.sha256(str(owner).encode("utf-8")).hexdigest()

    def _attempt_dir(
        self,
        *,
        conversation_id: str | None,
        invocation_id: object,
        attempt_id: object,
    ) -> Path:
        return (
            self._root
            / self._owner_key(conversation_id, invocation_id)
            / str(invocation_id)
            / str(attempt_id)
        )

    async def begin(
        self,
        route: RouteDecision,
        attempt: ModelCallAttempt,
    ) -> None:
        """Persist route and request atomically before provider dispatch."""
        record = ModelCallRecord(route=route, attempt=attempt)
        path = (
            self._attempt_dir(
                conversation_id=attempt.conversation_id,
                invocation_id=attempt.invocation_id,
                attempt_id=attempt.attempt_id,
            )
            / "attempt.json"
        )
        async with get_path_lock(path):
            try:
                existing = ModelCallRecord.model_validate(
                    await read_json_async(path),
                )
            except FileNotFoundError:
                existing = None
            if existing is not None:
                if existing.model_dump(
                    mode="json",
                    exclude={
                        "route": {"decided_at"},
                        "attempt": {"started_at"},
                    },
                ) != record.model_dump(
                    mode="json",
                    exclude={
                        "route": {"decided_at"},
                        "attempt": {"started_at"},
                    },
                ):
                    raise ModelCallConflictError(
                        "model attempt already has different evidence",
                    )
                return
            await write_json_atomic_async(
                path,
                record.model_dump(mode="json"),
                sort_keys=True,
            )

    async def complete(self, result: ModelCallResult) -> None:
        """Persist terminal evidence only after an attempt exists."""
        directory = self._attempt_dir(
            conversation_id=result.conversation_id,
            invocation_id=result.invocation_id,
            attempt_id=result.attempt_id,
        )
        attempt_path = directory / "attempt.json"
        result_path = directory / "result.json"
        async with get_path_lock(result_path):
            try:
                record = ModelCallRecord.model_validate(
                    await read_json_async(attempt_path),
                )
            except FileNotFoundError as exc:
                raise ModelCallConflictError(
                    "model result has no durable attempt",
                ) from exc
            ModelCallRecord(
                route=record.route,
                attempt=record.attempt,
                result=result,
            )
            try:
                existing = ModelCallResult.model_validate(
                    await read_json_async(result_path),
                )
            except FileNotFoundError:
                existing = None
            if existing is not None:
                if existing.model_dump(
                    mode="json",
                    exclude={"completed_at"},
                ) != result.model_dump(
                    mode="json",
                    exclude={"completed_at"},
                ):
                    raise ModelCallConflictError(
                        "model attempt already has a different result",
                    )
                return
            await write_json_atomic_async(
                result_path,
                result.model_dump(mode="json"),
                sort_keys=True,
            )

    def _list_sync(
        self,
        conversation_id: str,
        limit: int | None,
    ) -> list[ModelCallRecord]:
        root = self._root / self._owner_key(conversation_id, "")
        records: list[ModelCallRecord] = []
        for attempt_path in root.glob("*/*/attempt.json"):
            record = ModelCallRecord.model_validate_json(
                attempt_path.read_text(encoding="utf-8"),
            )
            result_path = attempt_path.with_name("result.json")
            result = (
                ModelCallResult.model_validate_json(
                    result_path.read_text(encoding="utf-8"),
                )
                if result_path.exists()
                else None
            )
            records.append(record.model_copy(update={"result": result}))
        records.sort(
            key=lambda item: (
                item.attempt.started_at,
                item.attempt.attempt_index,
            ),
            reverse=True,
        )
        return records if limit is None else records[:limit]

    async def list_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 100,
    ) -> Sequence[ModelCallRecord]:
        """Return newest model attempts for one ChatSpec identity."""
        if not conversation_id.strip():
            raise ValueError("conversation_id cannot be empty")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        return await run_sync_io(self._list_sync, conversation_id, limit)

    async def scan_for_conversation(
        self,
        conversation_id: str,
    ) -> Sequence[ModelCallRecord]:
        """Scan all Lite records for a derived-index rebuild."""
        if not conversation_id.strip():
            raise ValueError("conversation_id cannot be empty")
        return await run_sync_io(self._list_sync, conversation_id, None)

    def _scan_all_sync(self) -> list[ModelCallRecord]:
        records: list[ModelCallRecord] = []
        for attempt_path in self._root.glob("*/*/*/attempt.json"):
            record = ModelCallRecord.model_validate_json(
                attempt_path.read_text(encoding="utf-8"),
            )
            result_path = attempt_path.with_name("result.json")
            result = (
                ModelCallResult.model_validate_json(
                    result_path.read_text(encoding="utf-8"),
                )
                if result_path.exists()
                else None
            )
            records.append(record.model_copy(update={"result": result}))
        records.sort(
            key=lambda item: (
                item.attempt.started_at,
                item.attempt.attempt_index,
            ),
        )
        return records

    async def scan_all(self) -> Sequence[ModelCallRecord]:
        """Scan every workspace-owned fact for projection rebuilding."""
        return await run_sync_io(self._scan_all_sync)


class ModelCallSession:
    """Classify actual provider attempts within one logical model call."""

    def __init__(
        self,
        scope: InvocationScope,
        manifest: ContextManifest,
        store: ModelCallStore,
        *,
        requested_provider_id: str | None,
        requested_model_id: str | None,
        resource_waits: ModelResourceRecoveryPort | None = None,
    ) -> None:
        if manifest.invocation_id != scope.invocation_id:
            raise ValueError("model-call manifest invocation mismatch")
        if manifest.registry_epoch_id != scope.registry_epoch_id:
            raise ValueError("model-call manifest registry epoch mismatch")
        if manifest.registry_generation != scope.registry_generation:
            raise ValueError("model-call manifest generation mismatch")
        self._scope = scope
        self._manifest = manifest
        self._store = store
        self._requested_provider_id = requested_provider_id
        self._requested_model_id = requested_model_id
        self._resource_waits = resource_waits
        self._attempt_index = 0
        self._previous_attempt: ModelCallAttempt | None = None
        self._last_result: ModelCallResult | None = None

    async def begin(
        self,
        *,
        provider_id: str,
        model_id: str,
        adapter_id: str,
        adapter_version: str | None,
        formatter_id: str | None,
        formatter_version: str | None,
    ) -> ModelCallAttempt:
        """Persist one actual provider attempt before network dispatch."""
        self._attempt_index += 1
        previous = self._previous_attempt
        if previous is None:
            reason = (
                ModelRouteReason.OVERFLOW_RETRY
                if self._manifest.attempt_kind == "overflow_retry"
                else ModelRouteReason.PRIMARY
            )
        elif (
            previous.provider_id == provider_id
            and previous.model_id == model_id
        ):
            reason = ModelRouteReason.SAME_MODEL_RETRY
        else:
            reason = ModelRouteReason.FALLBACK
        attempt_id = uuid5(
            self._manifest.manifest_id,
            f"attempt:{self._attempt_index}:{provider_id}:{model_id}",
        )
        route_id = uuid5(attempt_id, "route")
        route = RouteDecision(
            route_decision_id=route_id,
            attempt_id=attempt_id,
            invocation_id=self._scope.invocation_id,
            correlation_id=(
                self._scope.correlation_id or self._scope.invocation_id
            ),
            conversation_id=self._scope.conversation_id,
            registry_epoch_id=self._scope.registry_epoch_id,
            registry_generation=self._scope.registry_generation,
            context_manifest_id=self._manifest.manifest_id,
            model_call_index=self._manifest.model_call_index,
            attempt_index=self._attempt_index,
            provider_id=provider_id,
            model_id=model_id,
            requested_provider_id=self._requested_provider_id,
            requested_model_id=self._requested_model_id,
            reason=reason,
            previous_attempt_id=(
                previous.attempt_id if previous is not None else None
            ),
        )
        attempt = ModelCallAttempt(
            attempt_id=attempt_id,
            route_decision_id=route_id,
            invocation_id=route.invocation_id,
            correlation_id=route.correlation_id,
            agent_id=self._scope.agent_id,
            conversation_id=route.conversation_id,
            registry_epoch_id=route.registry_epoch_id,
            registry_generation=route.registry_generation,
            context_manifest_id=route.context_manifest_id,
            model_call_index=route.model_call_index,
            attempt_index=route.attempt_index,
            provider_id=provider_id,
            model_id=model_id,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            formatter_id=formatter_id,
            formatter_version=formatter_version,
        )
        try:
            await self._store.begin(route, attempt)
        except Exception as exc:
            self._attempt_index -= 1
            raise ModelCallPersistenceError(
                "model request was not sent because its route could not be "
                "recorded",
            ) from exc
        self._previous_attempt = attempt
        return attempt

    async def complete(
        self,
        attempt: ModelCallAttempt,
        *,
        status: ModelCallStatus,
        error_kind: str = "",
        retryable: bool = False,
        emitted_content: bool = False,
        output_boundary: ModelOutputBoundary | None = None,
        failure_class: ModelFailureClass | None = None,
        recovery_disposition: ModelRecoveryDisposition | None = None,
        retry_after_seconds: float | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        usage_measurement: Literal["provider_reported"] | None = None,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        cache_eligible_input_tokens: int = 0,
        cache_observed: bool = False,
        cost_micros: int | None = None,
    ) -> ModelCallResult:
        """Persist content-free terminal evidence for one attempt."""
        result = ModelCallResult(
            attempt_id=attempt.attempt_id,
            invocation_id=attempt.invocation_id,
            conversation_id=attempt.conversation_id,
            status=status,
            error_kind=error_kind,
            retryable=retryable,
            emitted_content=emitted_content,
            output_boundary=output_boundary,
            failure_class=failure_class,
            recovery_disposition=recovery_disposition,
            retry_after_seconds=retry_after_seconds,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            usage_measurement=usage_measurement,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            cache_eligible_input_tokens=cache_eligible_input_tokens,
            cache_observed=cache_observed,
            cost_micros=cost_micros,
            cost_unknown=cost_micros is None,
        )
        try:
            await self._store.complete(result)
        except Exception as exc:
            raise ModelCallPersistenceError(
                "model attempt finished but its result could not be "
                "durably recorded",
            ) from exc
        self._last_result = result
        if (
            status is ModelCallStatus.SUCCEEDED
            and self._resource_waits is not None
        ):
            try:
                await self._resource_waits.release_provider_resource(
                    provider_id=attempt.provider_id,
                    model_id=attempt.model_id,
                )
            except Exception:  # pylint: disable=broad-except
                logger.exception(
                    "Failed to apply provider availability signal "
                    "provider=%s model=%s",
                    attempt.provider_id,
                    attempt.model_id,
                )
        return result

    async def defer_terminal_resource_wait(
        self,
    ) -> ModelResourceWait | ModelStepContinuation | None:
        """Persist recovery only after the logical model call has failed."""
        result = self._last_result
        attempt = self._previous_attempt
        if self._resource_waits is None or result is None or attempt is None:
            return None
        try:
            if result.recovery_disposition in {
                ModelRecoveryDisposition.RETRY_TRANSPORT,
                ModelRecoveryDisposition.WAIT_RESOURCE,
            }:
                return await self._resource_waits.defer(attempt, result)
            elif (
                result.recovery_disposition
                is ModelRecoveryDisposition.CONTINUE_MODEL_STEP
            ):
                return await self._resource_waits.defer_model_step(
                    attempt,
                    result,
                )
        except Exception as exc:
            raise ModelCallPersistenceError(
                "model result was recorded but its recovery could not "
                "be persisted",
            ) from exc
        return None


async def _defer_recovery_or_raise(
    session: ModelCallSession,
    error: BaseException,
) -> None:
    """Replace a partial-stream failure after its boundary is durable."""
    recovery = await session.defer_terminal_resource_wait()
    if isinstance(recovery, ModelStepContinuation):
        raise ModelStepRecoveryError(
            recovery.continuation_id,
            recovery.status,
        ) from error


async def begin_current_model_attempt(
    *,
    provider_id: str,
    model_id: str,
    adapter_id: str,
    adapter_version: str | None,
    formatter_id: str | None,
    formatter_version: str | None,
) -> ModelCallAttempt | None:
    """Begin an attempt when a logical call session is active."""
    session = _CURRENT_MODEL_CALL.get()
    if session is None:
        return None
    return await session.begin(
        provider_id=provider_id,
        model_id=model_id,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        formatter_id=formatter_id,
        formatter_version=formatter_version,
    )


async def complete_current_model_attempt(
    attempt: ModelCallAttempt | None,
    *,
    status: ModelCallStatus,
    error_kind: str = "",
    retryable: bool = False,
    emitted_content: bool = False,
    output_boundary: ModelOutputBoundary | None = None,
    failure_class: ModelFailureClass | None = None,
    recovery_disposition: ModelRecoveryDisposition | None = None,
    retry_after_seconds: float | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    usage_measurement: Literal["provider_reported"] | None = None,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_eligible_input_tokens: int = 0,
    cache_observed: bool = False,
    cost_micros: int | None = None,
) -> ModelCallResult | None:
    """Complete an attempt when it belongs to the active logical call."""
    if attempt is None:
        return None
    session = _CURRENT_MODEL_CALL.get()
    if session is None:
        raise ModelCallPersistenceError("model attempt lost its call session")
    return await session.complete(
        attempt,
        status=status,
        error_kind=error_kind,
        retryable=retryable,
        emitted_content=emitted_content,
        output_boundary=output_boundary,
        failure_class=failure_class,
        recovery_disposition=recovery_disposition,
        retry_after_seconds=retry_after_seconds,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        usage_measurement=usage_measurement,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        cache_eligible_input_tokens=cache_eligible_input_tokens,
        cache_observed=cache_observed,
        cost_micros=cost_micros,
    )


async def call_with_model_session(
    session: ModelCallSession,
    invoke: Callable[[], Awaitable[_Result]],
) -> _Result:
    """Keep attempt recording active for call creation and stream use."""
    token = _CURRENT_MODEL_CALL.set(session)
    try:
        try:
            result = await invoke()
        except BaseException as error:
            await _defer_recovery_or_raise(session, error)
            raise
    finally:
        _CURRENT_MODEL_CALL.reset(token)
    if not isinstance(result, AsyncGenerator):
        return result

    async def consume() -> AsyncGenerator[Any, None]:
        iterator = result.__aiter__()
        try:
            while True:
                stream_token = _CURRENT_MODEL_CALL.set(session)
                try:
                    item = await anext(iterator)
                except StopAsyncIteration:
                    return
                except BaseException as error:
                    await _defer_recovery_or_raise(session, error)
                    raise
                finally:
                    _CURRENT_MODEL_CALL.reset(stream_token)
                yield item
        finally:
            close_token = _CURRENT_MODEL_CALL.set(session)
            try:
                await result.aclose()
            finally:
                _CURRENT_MODEL_CALL.reset(close_token)

    return consume()  # type: ignore[return-value]


def lite_model_call_store(workspace_dir: Path) -> FilesystemModelCallStore:
    """Return the Lite model-call evidence store for one workspace."""
    return FilesystemModelCallStore(workspace_dir)


__all__ = [
    "FilesystemModelCallStore",
    "ModelCallConflictError",
    "ModelCallPersistenceError",
    "ModelCallSession",
    "ModelStepRecoveryError",
    "begin_current_model_attempt",
    "call_with_model_session",
    "complete_current_model_attempt",
    "lite_model_call_store",
]
