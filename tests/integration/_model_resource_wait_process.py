# -*- coding: utf-8 -*-
"""Process helper for durable model-resource wait recovery tests."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from qwenpaw.kernel import (
    ModelCallAttempt,
    ModelCallResult,
    ModelCallStatus,
    ModelFailureClass,
    ModelRecoveryDisposition,
    ResourceWaitStatus,
)
from qwenpaw.recovery import ModelResourceWaitService

_AGENT_ID = "agent-resource-recovery"
_CHAT_ID = "chat-resource-recovery"
_ATTEMPT_ID = UUID("10000000-0000-4000-8000-000000000001")
_INVOCATION_ID = UUID("10000000-0000-4000-8000-000000000002")
_CORRELATION_ID = UUID("10000000-0000-4000-8000-000000000003")
_ROUTE_ID = UUID("10000000-0000-4000-8000-000000000004")
_MANIFEST_ID = UUID("10000000-0000-4000-8000-000000000005")
_SUBMISSION_ID = UUID("10000000-0000-4000-8000-000000000006")
_RETRY_AFTER_SECONDS = 10.0


def _attempt() -> ModelCallAttempt:
    return ModelCallAttempt(
        attempt_id=_ATTEMPT_ID,
        route_decision_id=_ROUTE_ID,
        invocation_id=_INVOCATION_ID,
        correlation_id=_CORRELATION_ID,
        agent_id=_AGENT_ID,
        conversation_id=_CHAT_ID,
        registry_generation=1,
        context_manifest_id=_MANIFEST_ID,
        model_call_index=1,
        attempt_index=1,
        provider_id="provider-process-recovery",
        model_id="model-process-recovery",
    )


def _service(database_path: Path) -> ModelResourceWaitService:
    return ModelResourceWaitService(
        database_path,
        agent_id=_AGENT_ID,
    )


async def _defer(database_path: Path, ready_path: Path) -> None:
    attempt = _attempt()
    result = ModelCallResult(
        attempt_id=attempt.attempt_id,
        invocation_id=attempt.invocation_id,
        conversation_id=attempt.conversation_id,
        status=ModelCallStatus.FAILED,
        error_kind="rate_limited",
        failure_class=ModelFailureClass.RATE_LIMITED,
        recovery_disposition=ModelRecoveryDisposition.WAIT_RESOURCE,
        retry_after_seconds=_RETRY_AFTER_SECONDS,
        completed_at=datetime.now(timezone.utc),
    )
    wait = await _service(database_path).defer(attempt, result)
    if wait is None or wait.status is not ResourceWaitStatus.WAITING:
        raise RuntimeError("rate-limit wait was not durably deferred")
    ready_path.write_text(
        json.dumps(
            {
                "correlation_id": str(wait.correlation_id),
                "conversation_id": wait.conversation_id,
                "not_before": wait.not_before.isoformat(),
                "wait_id": str(wait.wait_id),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    await asyncio.Event().wait()


async def _inspect(database_path: Path, wait_id: UUID) -> None:
    service = _service(database_path)
    wait = await service.get(wait_id)
    remaining = await service.seconds_until_next_timer()
    if wait is None:
        raise RuntimeError("resource wait disappeared after process restart")
    print(
        json.dumps(
            {
                "remaining_seconds": remaining,
                "status": wait.status.value,
                "submission_id": wait.submission_id,
            },
            default=str,
            sort_keys=True,
        ),
        flush=True,
    )


async def _recover(database_path: Path, wait_id: UUID) -> None:
    service = _service(database_path)
    ready = await service.list_ready()
    selected = next((item for item in ready if item.wait_id == wait_id), None)
    if selected is None:
        raise RuntimeError("mature resource wait was not dispatchable")

    async def enqueue(current) -> UUID:
        if current.conversation_id != _CHAT_ID:
            raise RuntimeError("resource wait ChatSpec identity drifted")
        if current.correlation_id != _CORRELATION_ID:
            raise RuntimeError("resource wait correlation identity drifted")
        return _SUBMISSION_ID

    dispatched = await service.dispatch_ready(wait_id, enqueue)
    print(
        json.dumps(
            {
                "correlation_id": str(dispatched.correlation_id),
                "conversation_id": dispatched.conversation_id,
                "status": dispatched.status.value,
                "submission_id": str(dispatched.submission_id),
                "wait_id": str(dispatched.wait_id),
            },
            sort_keys=True,
        ),
        flush=True,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("defer", "inspect", "recover"))
    parser.add_argument("database_path", type=Path)
    parser.add_argument("--ready-path", type=Path)
    parser.add_argument("--wait-id", type=UUID)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.mode == "defer":
        if args.ready_path is None:
            raise ValueError("defer mode requires --ready-path")
        asyncio.run(_defer(args.database_path, args.ready_path))
        return
    if args.wait_id is None:
        raise ValueError(f"{args.mode} mode requires --wait-id")
    if args.mode == "inspect":
        asyncio.run(_inspect(args.database_path, args.wait_id))
        return
    asyncio.run(_recover(args.database_path, args.wait_id))


if __name__ == "__main__":
    main()
