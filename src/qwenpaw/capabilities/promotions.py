# -*- coding: utf-8 -*-
"""Durable Lite journal for capability promotion WAL phases."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from collections.abc import Mapping, Sequence
from uuid import UUID

from ..kernel import (
    ArtifactRef,
    ArtifactRenderDisposition,
    ArtifactRenderRequest,
    ArtifactRenderResult,
    CapabilityCheckOutcome,
    CapabilityEvaluationDecision,
    CapabilityPromotionAssessment,
    CapabilityPromotionCandidate,
    CapabilityPromotionCheck,
    CapabilityPromotionEvidence,
    CapabilityPromotionEvidenceBundle,
    CapabilityPromotionEvaluation,
    CapabilityPromotionEvent,
    CapabilityReleaseTag,
)
from ..kernel.ports import ArtifactRenderer
from ..kernel.slots import slot_contract
from ..utils.io_utils import (
    get_path_lock,
    read_json_async,
    run_sync_io,
    write_json_atomic_async,
)


class CapabilityPromotionConflictError(RuntimeError):
    """Raised when one operation phase has different durable evidence."""


class CapabilityPromotionEvidenceConflictError(RuntimeError):
    """Raised when one bundle identity has different evidence."""


def build_capability_promotion_assessment(
    candidate: CapabilityPromotionCandidate,
    release: CapabilityReleaseTag | None,
    *,
    evaluator_id: str,
    decision: CapabilityEvaluationDecision,
    checks: tuple[tuple[str, CapabilityCheckOutcome], ...],
    additional_evidence: tuple[CapabilityPromotionEvidence, ...] = (),
) -> CapabilityPromotionAssessment:
    capability_ids = release.capability_ids if release is not None else ()
    contract_evidence = tuple(
        CapabilityPromotionEvidence.create(
            candidate=candidate,
            check_id=check_id,
            producer_id=evaluator_id,
            outcome=outcome,
            capability_ids=capability_ids,
        )
        for check_id, outcome in checks
    )
    evidence = (*contract_evidence, *additional_evidence)
    bundle = CapabilityPromotionEvidenceBundle.create(
        candidate=candidate,
        evaluator_id=evaluator_id,
        evidence=evidence,
    )
    evaluation = CapabilityPromotionEvaluation(
        candidate_id=candidate.candidate_id,
        candidate_hash=candidate.candidate_hash,
        evaluator_id=evaluator_id,
        evidence_bundle_id=bundle.bundle_id,
        decision=decision,
        checks=tuple(
            CapabilityPromotionCheck(
                check_id=item.check_id,
                outcome=item.outcome,
                evidence_ids=(item.evidence_id,),
            )
            for item in evidence
        ),
    )
    return CapabilityPromotionAssessment(
        evaluation=evaluation,
        evidence_bundle=bundle,
    )


class ContractCapabilityPromotionGate:
    """Lite gate admitting candidates that passed Registry staging."""

    async def evaluate(
        self,
        candidate: CapabilityPromotionCandidate,
        release: CapabilityReleaseTag,
        scenario_evidence: Sequence[CapabilityPromotionEvidence] = (),
    ) -> CapabilityPromotionAssessment:
        """Record the shared schema, implementation, and health gate."""
        decision = (
            CapabilityEvaluationDecision.DENY
            if any(
                item.outcome is CapabilityCheckOutcome.FAILED
                for item in scenario_evidence
            )
            else CapabilityEvaluationDecision.ALLOW
        )
        return build_capability_promotion_assessment(
            candidate,
            release,
            evaluator_id="qwenpaw.contract-gate",
            decision=decision,
            checks=(
                ("contract.schema", CapabilityCheckOutcome.PASSED),
                ("contract.implementation", CapabilityCheckOutcome.PASSED),
                ("contract.health", CapabilityCheckOutcome.PASSED),
            ),
            additional_evidence=tuple(scenario_evidence),
        )


def rejected_contract_assessment(
    candidate: CapabilityPromotionCandidate,
    *,
    check_id: str,
    additional_evidence: tuple[CapabilityPromotionEvidence, ...] = (),
) -> CapabilityPromotionAssessment:
    """Create content-safe denial evidence for a staging failure."""
    return build_capability_promotion_assessment(
        candidate,
        None,
        evaluator_id="qwenpaw.contract-gate",
        decision=CapabilityEvaluationDecision.DENY,
        checks=((check_id, CapabilityCheckOutcome.FAILED),),
        additional_evidence=additional_evidence,
    )


def _bundle_payload(
    bundle: CapabilityPromotionEvidenceBundle,
) -> dict:
    payload = bundle.model_dump(mode="json")
    payload.pop("created_at", None)
    for evidence in payload["evidence"]:
        evidence.pop("created_at", None)
    return payload


class InMemoryCapabilityPromotionEvidenceStore:
    """Process-local evidence store for isolated registries and tests."""

    def __init__(self) -> None:
        self._bundles: dict[UUID, CapabilityPromotionEvidenceBundle] = {}
        self._lock = asyncio.Lock()

    async def append(
        self,
        bundle: CapabilityPromotionEvidenceBundle,
    ) -> None:
        """Append one immutable bundle idempotently."""
        async with self._lock:
            existing = self._bundles.get(bundle.bundle_id)
            if existing is not None and _bundle_payload(
                existing,
            ) != _bundle_payload(bundle):
                raise CapabilityPromotionEvidenceConflictError(
                    "promotion bundle already has different evidence",
                )
            self._bundles[bundle.bundle_id] = bundle

    async def get(
        self,
        bundle_id: UUID,
    ) -> CapabilityPromotionEvidenceBundle | None:
        """Return one bundle by identity."""
        async with self._lock:
            return self._bundles.get(bundle_id)

    async def list_for_candidate(
        self,
        candidate_id: UUID,
        *,
        limit: int = 100,
    ) -> Sequence[CapabilityPromotionEvidenceBundle]:
        """Return newest bundles for one candidate."""
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        async with self._lock:
            bundles = [
                item
                for item in self._bundles.values()
                if item.candidate_id == candidate_id
            ]
        bundles.sort(key=lambda item: item.created_at, reverse=True)
        return tuple(bundles[:limit])


class LiteCapabilityPromotionScenarioRunner:
    """Run bounded, side-effect-free host scenarios for supported Slots."""

    _MAX_OUTPUT_BYTES = 64 * 1024
    _TIMEOUT_SECONDS = 5.0
    _SAFE_INLINE_MEDIA_TYPES = frozenset(
        {"application/json", "text/markdown", "text/plain"},
    )

    @staticmethod
    def _fixtures() -> tuple[ArtifactRenderRequest, ...]:
        content = b"# QwenPaw promotion scenario\n"
        digest = hashlib.sha256(content).hexdigest()
        artifact = ArtifactRef(
            kind="task.summary",
            uri="promotion://artifact-renderer/fixture",
            media_type="text/markdown",
            content_hash=f"sha256:{digest}",
            size_bytes=len(content),
        )
        return tuple(
            ArtifactRenderRequest(
                artifact=artifact,
                content=content,
                disposition=disposition,
                filename="promotion-scenario.md",
                max_output_bytes=(
                    LiteCapabilityPromotionScenarioRunner._MAX_OUTPUT_BYTES
                ),
            )
            for disposition in (
                ArtifactRenderDisposition.INLINE,
                ArtifactRenderDisposition.ATTACHMENT,
            )
        )

    @classmethod
    def _validate_renderer_result(
        cls,
        renderer: ArtifactRenderer,
        request: ArtifactRenderRequest,
        result: ArtifactRenderResult,
    ) -> None:
        if result.renderer_id != renderer.renderer_id:
            raise ValueError("renderer result identity mismatch")
        if result.source_content_hash != request.artifact.content_hash:
            raise ValueError("renderer source digest mismatch")
        if result.disposition is not request.disposition:
            raise ValueError("renderer changed disposition")
        if result.filename != request.filename:
            raise ValueError("renderer changed filename")
        if len(result.content) > request.max_output_bytes:
            raise ValueError("renderer output exceeds scenario budget")
        if (
            request.disposition is ArtifactRenderDisposition.INLINE
            and result.media_type not in cls._SAFE_INLINE_MEDIA_TYPES
        ):
            raise ValueError("renderer returned unsafe inline media type")
        if request.disposition is ArtifactRenderDisposition.ATTACHMENT and (
            result.content != request.content
            or result.media_type != request.artifact.media_type
        ):
            raise ValueError("renderer changed attachment bytes or type")

    async def _run_renderer(
        self,
        implementation: object,
    ) -> CapabilityCheckOutcome:
        if not isinstance(implementation, ArtifactRenderer):
            return CapabilityCheckOutcome.FAILED
        supported = False
        try:
            for request in self._fixtures():
                if not implementation.supports(
                    request.artifact,
                    request.disposition,
                ):
                    continue
                supported = True
                result = await asyncio.wait_for(
                    implementation.render(request),
                    timeout=self._TIMEOUT_SECONDS,
                )
                if not isinstance(result, ArtifactRenderResult):
                    return CapabilityCheckOutcome.FAILED
                self._validate_renderer_result(
                    implementation,
                    request,
                    result,
                )
        except Exception:  # pylint: disable=broad-except
            return CapabilityCheckOutcome.FAILED
        return (
            CapabilityCheckOutcome.PASSED
            if supported
            else CapabilityCheckOutcome.NOT_APPLICABLE
        )

    async def run(
        self,
        candidate: CapabilityPromotionCandidate,
        release: CapabilityReleaseTag,
        implementations: Mapping[str, object],
    ) -> Sequence[CapabilityPromotionEvidence]:
        """Return content-safe evidence for every supported Slot scenario."""
        evidence = []
        for item in release.releases:
            contract = slot_contract(item.slot)
            if "artifact-renderer.roundtrip" not in (
                contract.promotion_scenarios
            ):
                continue
            outcome = await self._run_renderer(
                implementations.get(item.capability_id),
            )
            evidence.append(
                CapabilityPromotionEvidence.create(
                    candidate=candidate,
                    check_id=(
                        "scenario.artifact-renderer.roundtrip."
                        f"{item.capability_id}"
                    ),
                    producer_id="qwenpaw.lite-scenario-runner",
                    outcome=outcome,
                    capability_ids=(item.capability_id,),
                ),
            )
        return tuple(evidence)


class FilesystemCapabilityPromotionEvidenceStore:
    """Owner-only append-once Lite promotion evidence store."""

    def __init__(self, state_dir: Path) -> None:
        self._root = (
            Path(state_dir) / "lite" / "capability-promotion-evidence"
        )

    def _path(self, bundle_id: UUID) -> Path:
        return self._root / f"{bundle_id}.json"

    async def append(
        self,
        bundle: CapabilityPromotionEvidenceBundle,
    ) -> None:
        """Persist one immutable bundle exactly once."""
        path = self._path(bundle.bundle_id)
        async with get_path_lock(path):
            try:
                payload = await read_json_async(path)
            except FileNotFoundError:
                payload = None
            if payload is not None:
                existing = CapabilityPromotionEvidenceBundle.model_validate(
                    payload,
                )
                if _bundle_payload(existing) != _bundle_payload(bundle):
                    raise CapabilityPromotionEvidenceConflictError(
                        "promotion bundle already has different evidence",
                    )
                return
            await write_json_atomic_async(
                path,
                bundle.model_dump(mode="json"),
                sort_keys=True,
            )

    async def get(
        self,
        bundle_id: UUID,
    ) -> CapabilityPromotionEvidenceBundle | None:
        """Return one bundle by identity."""
        try:
            payload = await read_json_async(self._path(bundle_id))
        except FileNotFoundError:
            return None
        return CapabilityPromotionEvidenceBundle.model_validate(payload)

    def _list_sync(
        self,
        candidate_id: UUID,
        limit: int,
    ) -> list[CapabilityPromotionEvidenceBundle]:
        bundles = [
            CapabilityPromotionEvidenceBundle.model_validate(
                json.loads(path.read_text(encoding="utf-8")),
            )
            for path in self._root.glob("*.json")
        ]
        bundles = [
            item for item in bundles if item.candidate_id == candidate_id
        ]
        bundles.sort(key=lambda item: item.created_at, reverse=True)
        return bundles[:limit]

    async def list_for_candidate(
        self,
        candidate_id: UUID,
        *,
        limit: int = 100,
    ) -> Sequence[CapabilityPromotionEvidenceBundle]:
        """Return newest bundles for one candidate."""
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        return await run_sync_io(self._list_sync, candidate_id, limit)


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
    "CapabilityPromotionEvidenceConflictError",
    "ContractCapabilityPromotionGate",
    "FilesystemCapabilityPromotionEvidenceStore",
    "FilesystemCapabilityPromotionJournal",
    "InMemoryCapabilityPromotionEvidenceStore",
    "LiteCapabilityPromotionScenarioRunner",
    "build_capability_promotion_assessment",
    "rejected_contract_assessment",
]
