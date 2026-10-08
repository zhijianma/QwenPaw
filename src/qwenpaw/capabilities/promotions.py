# -*- coding: utf-8 -*-
"""Durable Lite journal for capability promotion WAL phases."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

from ..kernel import (
    CapabilityCheckOutcome,
    CapabilityEvaluationDecision,
    CapabilityPromotionCandidate,
    CapabilityPromotionCheck,
    CapabilityPromotionEvaluation,
    CapabilityPromotionEvent,
    CapabilityReleaseTag,
)
from ..utils.io_utils import (
    get_path_lock,
    read_json_async,
    run_sync_io,
    write_json_atomic_async,
)


class CapabilityPromotionConflictError(RuntimeError):
    """Raised when one operation phase has different durable evidence."""


class ContractCapabilityPromotionGate:
    """Lite gate admitting candidates that passed Registry staging."""

    async def evaluate(
        self,
        candidate: CapabilityPromotionCandidate,
        release: CapabilityReleaseTag,
    ) -> CapabilityPromotionEvaluation:
        """Record the shared schema, implementation, and health gate."""
        del release
        return CapabilityPromotionEvaluation(
            candidate_id=candidate.candidate_id,
            candidate_hash=candidate.candidate_hash,
            evaluator_id="qwenpaw.contract-gate",
            decision=CapabilityEvaluationDecision.ALLOW,
            checks=(
                CapabilityPromotionCheck(
                    check_id="contract.activation",
                    outcome=CapabilityCheckOutcome.PASSED,
                ),
            ),
        )


def rejected_contract_evaluation(
    candidate: CapabilityPromotionCandidate,
    *,
    check_id: str,
) -> CapabilityPromotionEvaluation:
    """Create content-safe denial evidence for a staging failure."""
    return CapabilityPromotionEvaluation(
        candidate_id=candidate.candidate_id,
        candidate_hash=candidate.candidate_hash,
        evaluator_id="qwenpaw.contract-gate",
        decision=CapabilityEvaluationDecision.DENY,
        checks=(
            CapabilityPromotionCheck(
                check_id=check_id,
                outcome=CapabilityCheckOutcome.FAILED,
            ),
        ),
    )


class FilesystemCapabilityPromotionJournal:
    """Owner-only append-once Lite promotion journal."""

    def __init__(self, state_dir: Path) -> None:
        self._root = (
            Path(state_dir) / "lite" / "capability-promotions"
        )

    def _path(self, event: CapabilityPromotionEvent) -> Path:
        return (
            self._root
            / str(event.operation_id)
            / f"{event.phase.value}.json"
        )

    async def append(self, event: CapabilityPromotionEvent) -> None:
        """Persist one immutable operation phase exactly once."""
        path = self._path(event)
        async with get_path_lock(path):
            try:
                payload = await read_json_async(path)
            except FileNotFoundError:
                payload = None
            if payload is not None:
                existing = CapabilityPromotionEvent.model_validate(payload)
                if existing != event:
                    raise CapabilityPromotionConflictError(
                        "promotion phase already has different evidence",
                    )
                return
            await write_json_atomic_async(
                path,
                event.model_dump(mode="json"),
                sort_keys=True,
            )

    def _list_sync(
        self,
        provider_id: str | None,
        limit: int,
    ) -> list[CapabilityPromotionEvent]:
        events = [
            CapabilityPromotionEvent.model_validate(
                json.loads(path.read_text(encoding="utf-8")),
            )
            for path in self._root.glob("*/*.json")
        ]
        if provider_id is not None:
            events = [
                event
                for event in events
                if event.candidate.provider_id == provider_id
            ]
        events.sort(key=lambda item: item.occurred_at, reverse=True)
        return events[:limit]

    async def list_events(
        self,
        *,
        provider_id: str | None = None,
        limit: int = 100,
    ) -> Sequence[CapabilityPromotionEvent]:
        """Return newest valid phases from durable evidence."""
        if provider_id is not None and not provider_id.strip():
            raise ValueError("provider_id cannot be empty")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        return await run_sync_io(
            self._list_sync,
            provider_id,
            limit,
        )


__all__ = [
    "CapabilityPromotionConflictError",
    "ContractCapabilityPromotionGate",
    "FilesystemCapabilityPromotionJournal",
    "rejected_contract_evaluation",
]
