# -*- coding: utf-8 -*-
"""Immutable capability generations with atomic provider activation."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import NoReturn
from uuid import UUID, uuid4

from ..kernel.models import (
    CapabilityBundle,
    CapabilityContribution,
    CapabilityDescriptor,
)
from ..kernel.ports import (
    CapabilityLease,
    CapabilityPromotionEvidenceStore,
    CapabilityPromotionGate,
    CapabilityPromotionJournal,
    CapabilityPromotionScenarioRunner,
)
from ..kernel.releases import (
    CapabilityCheckOutcome,
    CapabilityEvaluationDecision,
    CapabilityPromotionAction,
    CapabilityPromotionAssessment,
    CapabilityPromotionCandidate,
    CapabilityPromotionEvidence,
    CapabilityPromotionEvaluation,
    CapabilityPromotionEvent,
    CapabilityPromotionEvidenceBundle,
    CapabilityPromotionPhase,
    CapabilityReleaseTag,
)
from .contracts import (
    CapabilityImplementationError,
    validate_capability_implementation,
)
from .promotions import (
    ContractCapabilityPromotionGate,
    InMemoryCapabilityPromotionEvidenceStore,
    build_capability_promotion_assessment,
    rejected_contract_assessment,
)

ContributionFactory = Callable[
    [CapabilityContribution],
    object | Awaitable[object],
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActivatedContribution:
    """One validated descriptor paired with its implementation."""

    descriptor: CapabilityDescriptor
    implementation: object


@dataclass(frozen=True)
class RegistrySnapshot:
    """Immutable registry state pinned by an active invocation."""

    registry_epoch_id: UUID
    generation: int
    capabilities: Mapping[str, ActivatedContribution]


class ActivationError(RuntimeError):
    """Raised when staging or health checks reject an activation."""


class ReleaseRollbackError(RuntimeError):
    """Raised when a stable provider release cannot be rolled back."""


class ProviderDeactivationError(RuntimeError):
    """Raised when a provider cannot be transactionally deactivated."""


@dataclass(frozen=True)
class _ProviderRollbackPoint:
    """Last promoted provider state retained for a fenced rollback."""

    promoted_release_hash: str
    contributions: Mapping[str, ActivatedContribution]


@dataclass(frozen=True)
class _PromotionTransaction:
    """Prepared in-memory state for one provider promotion."""

    operation_id: UUID
    candidate: CapabilityPromotionCandidate
    evaluation: CapabilityPromotionEvaluation
    evidence_bundle: CapabilityPromotionEvidenceBundle
    previous_snapshot: RegistrySnapshot
    previous_release: CapabilityReleaseTag | None
    previous_rollback: _ProviderRollbackPoint | None
    previous_contributions: Mapping[str, ActivatedContribution]
    snapshot: RegistrySnapshot
    release: CapabilityReleaseTag


@dataclass(frozen=True)
class _RollbackTransaction:
    """Prepared in-memory state for one fenced provider rollback."""

    operation_id: UUID
    candidate: CapabilityPromotionCandidate
    evaluation: CapabilityPromotionEvaluation
    evidence_bundle: CapabilityPromotionEvidenceBundle
    previous_snapshot: RegistrySnapshot
    previous_release: CapabilityReleaseTag
    rollback_point: _ProviderRollbackPoint
    snapshot: RegistrySnapshot
    target_release: CapabilityReleaseTag | None


@dataclass(frozen=True)
class _DeactivationTransaction:
    """Prepared in-memory state for one provider deactivation."""

    operation_id: UUID
    candidate: CapabilityPromotionCandidate
    evaluation: CapabilityPromotionEvaluation
    evidence_bundle: CapabilityPromotionEvidenceBundle
    previous_snapshot: RegistrySnapshot
    previous_release: CapabilityReleaseTag | None
    previous_rollback: _ProviderRollbackPoint | None
    snapshot: RegistrySnapshot


class GenerationLease(CapabilityLease):
    """Reference-counted lease over one immutable snapshot."""

    def __init__(
        self,
        registry: "GenerationRegistry",
        snapshot: RegistrySnapshot,
    ) -> None:
        self._registry = registry
        self._snapshot = snapshot
        self._closed = False

    @property
    def generation(self) -> int:
        """Return the pinned generation number."""
        return self._snapshot.generation

    @property
    def registry_epoch_id(self) -> UUID:
        """Return the process epoch that scopes this generation."""
        return self._snapshot.registry_epoch_id

    def resolve(self, capability_id: str) -> CapabilityDescriptor | None:
        """Resolve a descriptor without exposing its implementation."""
        contribution = self._snapshot.capabilities.get(capability_id)
        return contribution.descriptor if contribution else None

    def implementation(self, capability_id: str) -> object | None:
        """Resolve the staged implementation for runtime dispatch."""
        contribution = self._snapshot.capabilities.get(capability_id)
        return contribution.implementation if contribution else None

    def descriptors(
        self,
        slot: str | None = None,
    ) -> tuple[CapabilityDescriptor, ...]:
        """List pinned descriptors in stable capability ID order."""
        descriptors = (
            contribution.descriptor
            for contribution in self._snapshot.capabilities.values()
        )
        return tuple(
            sorted(
                (
                    descriptor
                    for descriptor in descriptors
                    if slot is None or descriptor.slot == slot
                ),
                key=lambda descriptor: descriptor.capability_id,
            ),
        )

    async def close(self) -> None:
        """Release this lease exactly once."""
        if self._closed:
            return
        self._closed = True
        await self._registry.release(self.generation)


class GenerationRegistry:
    """Publish complete provider updates as one atomic generation."""

    def __init__(
        self,
        *,
        promotion_journal: CapabilityPromotionJournal | None = None,
        promotion_evidence_store: (
            CapabilityPromotionEvidenceStore | None
        ) = None,
        promotion_gate: CapabilityPromotionGate | None = None,
        promotion_scenario_runner: (
            CapabilityPromotionScenarioRunner | None
        ) = None,
        registry_epoch_id: UUID | None = None,
    ) -> None:
        self._registry_epoch_id = registry_epoch_id or uuid4()
        initial = RegistrySnapshot(
            registry_epoch_id=self._registry_epoch_id,
            generation=1,
            capabilities=MappingProxyType({}),
        )
        self._current = initial
        self._snapshots = {initial.generation: initial}
        self._leases = {initial.generation: 0}
        self._lock = asyncio.Lock()
        self._ensure_lock = asyncio.Lock()
        self._provider_cleanup: dict[str, list[Callable[[], None]]] = {}
        self._stable_releases: dict[str, CapabilityReleaseTag] = {}
        self._rollback_points: dict[str, _ProviderRollbackPoint] = {}
        self._promotion_journal = promotion_journal
        self._promotion_evidence_store = (
            promotion_evidence_store
            or InMemoryCapabilityPromotionEvidenceStore()
        )
        self._promotion_gate = (
            promotion_gate or ContractCapabilityPromotionGate()
        )
        self._promotion_scenario_runner = promotion_scenario_runner

    @property
    def generation(self) -> int:
        """Return the currently published generation."""
        return self._current.generation

    @property
    def registry_epoch_id(self) -> UUID:
        """Return the process epoch that scopes generation numbers."""
        return self._registry_epoch_id

    def current_descriptor(
        self,
        capability_id: str,
    ) -> CapabilityDescriptor | None:
        """Return one descriptor from the current immutable snapshot."""
        contribution = self._current.capabilities.get(capability_id)
        return contribution.descriptor if contribution else None

    def stable_release(
        self,
        provider_id: str,
    ) -> CapabilityReleaseTag | None:
        """Return the current content-addressed stable provider tag."""
        return self._stable_releases.get(provider_id)

    def stable_releases(self) -> tuple[CapabilityReleaseTag, ...]:
        """List stable provider tags in deterministic identity order."""
        return tuple(
            sorted(
                self._stable_releases.values(),
                key=lambda item: item.provider_id,
            ),
        )

    async def promotion_events(
        self,
        *,
        provider_id: str | None = None,
        limit: int = 100,
    ) -> tuple[CapabilityPromotionEvent, ...]:
        """Read durable promotion phases when a Journal is configured."""
        if self._promotion_journal is None:
            return ()
        events = await self._promotion_journal.list_events(
            provider_id=provider_id,
            limit=limit,
        )
        return tuple(events)

    async def promotion_evidence(
        self,
        candidate_id: UUID,
        *,
        limit: int = 100,
    ) -> tuple[CapabilityPromotionEvidenceBundle, ...]:
        """Read immutable evidence bundles for one candidate."""
        bundles = await self._promotion_evidence_store.list_for_candidate(
            candidate_id,
            limit=limit,
        )
        return tuple(bundles)

    async def _persist_promotion_evidence(
        self,
        bundle: CapabilityPromotionEvidenceBundle,
    ) -> None:
        await self._promotion_evidence_store.append(bundle)
        persisted = await self._promotion_evidence_store.get(
            bundle.bundle_id,
        )
        if persisted is None or (
            persisted.bundle_id != bundle.bundle_id
            or persisted.bundle_hash != bundle.bundle_hash
        ):
            raise RuntimeError("promotion evidence persistence mismatch")

    async def _append_promotion_event(
        self,
        event: CapabilityPromotionEvent,
    ) -> None:
        if self._promotion_journal is not None:
            await self._promotion_journal.append(event)

    async def _record_rejected_activation(
        self,
        *,
        operation_id: UUID,
        candidate: CapabilityPromotionCandidate,
        check_id: str,
    ) -> None:
        assessment = rejected_contract_assessment(
            candidate,
            check_id=check_id,
        )
        evaluation = assessment.evaluation
        event = CapabilityPromotionEvent.create(
            operation_id=operation_id,
            registry_epoch_id=self._registry_epoch_id,
            action=CapabilityPromotionAction.PROMOTE,
            phase=CapabilityPromotionPhase.REJECTED,
            candidate=candidate,
            evaluation=evaluation,
            from_generation=self.generation,
            target_generation=None,
            previous_release_hash=(
                release.release_hash
                if (
                    release := self.stable_release(candidate.provider_id)
                )
                is not None
                else None
            ),
            target_release=None,
            reason_code=check_id,
        )
        try:
            await self._persist_promotion_evidence(
                assessment.evidence_bundle,
            )
            await self._append_promotion_event(event)
        except Exception:  # pylint: disable=broad-except
            logger.exception(
                "failed to journal rejected capability promotion: %s",
                candidate.provider_id,
            )

    async def pin(
        self,
        generation: int | None = None,
    ) -> GenerationLease:
        """Pin the current or one retained immutable generation."""
        async with self._lock:
            if generation is None:
                snapshot = self._current
            else:
                snapshot = self._snapshots.get(generation)
                if snapshot is None:
                    raise LookupError(
                        f"registry generation {generation} is unavailable",
                    )
            self._leases[snapshot.generation] += 1
        return GenerationLease(self, snapshot)

    async def ensure_bundle(
        self,
        bundle: CapabilityBundle,
        factory: ContributionFactory,
    ) -> RegistrySnapshot:
        """Publish a bundle only when its exact descriptors are absent."""
        async with self._ensure_lock:
            expected = {
                f"{bundle.provider_id}.{item.contribution_id}": item
                for item in bundle.contributions
            }
            current_provider_ids = {
                capability_id
                for capability_id, contribution in (
                    self._current.capabilities.items()
                )
                if contribution.descriptor.provider_id == bundle.provider_id
            }
            current_matches = current_provider_ids == set(expected) and all(
                (descriptor := self.current_descriptor(capability_id))
                is not None
                and descriptor.version == bundle.version
                and descriptor.slot == declaration.slot
                for capability_id, declaration in expected.items()
            )
            if current_matches:
                return self._current
            return await self.activate_bundle(bundle, factory)

    async def activate_bundle(
        self,
        bundle: CapabilityBundle,
        factory: ContributionFactory,
    ) -> RegistrySnapshot:
        """Stage and atomically publish a system or plugin bundle."""
        operation_id = uuid4()
        candidate = CapabilityPromotionCandidate.create(
            provider_id=bundle.provider_id,
            provider_kind=bundle.provider_kind,
            version=bundle.version,
            bundle_payload=bundle.model_dump(mode="json"),
        )
        try:
            staged = await self._stage_bundle(bundle, factory)
        except Exception as exc:
            await self._handle_stage_failure(
                operation_id=operation_id,
                candidate=candidate,
                provider_id=bundle.provider_id,
                error=exc,
            )

        async with self._lock:
            transaction = await self._prepare_promotion(
                operation_id=operation_id,
                candidate=candidate,
                bundle=bundle,
                staged=staged,
            )
            return await self._commit_promotion(transaction)

    async def _stage_bundle(
        self,
        bundle: CapabilityBundle,
        factory: ContributionFactory,
    ) -> dict[str, ActivatedContribution]:
        staged: dict[str, ActivatedContribution] = {}
        for declaration in bundle.contributions:
            implementation = factory(declaration)
            if inspect.isawaitable(implementation):
                implementation = await implementation
            health_check = getattr(implementation, "health_check", None)
            if health_check is not None:
                healthy = health_check()
                if inspect.isawaitable(healthy):
                    healthy = await healthy
                if healthy is not True:
                    raise ActivationError(
                        f"health check failed for "
                        f"{declaration.contribution_id}",
                    )
            validate_capability_implementation(
                bundle.provider_id,
                declaration,
                implementation,
            )
            descriptor = CapabilityDescriptor(
                capability_id=(
                    f"{bundle.provider_id}.{declaration.contribution_id}"
                ),
                slot=declaration.slot,
                provider_id=bundle.provider_id,
                provider_kind=bundle.provider_kind,
                version=bundle.version,
                input_schema=declaration.input_schema,
                output_schema=declaration.output_schema,
                config_schema=declaration.config_schema,
                restart_policy=bundle.restart_policy,
                metadata=declaration.metadata,
            )
            staged[descriptor.capability_id] = ActivatedContribution(
                descriptor=descriptor,
                implementation=implementation,
            )
        return staged

    async def _handle_stage_failure(
        self,
        *,
        operation_id: UUID,
        candidate: CapabilityPromotionCandidate,
        provider_id: str,
        error: Exception,
    ) -> NoReturn:
        if isinstance(error, ActivationError) and (
            "health check failed" in str(error)
        ):
            check_id = "contract.health"
        elif isinstance(error, CapabilityImplementationError):
            check_id = "contract.implementation"
        else:
            check_id = "contract.staging"
        await self._record_rejected_activation(
            operation_id=operation_id,
            candidate=candidate,
            check_id=check_id,
        )
        if isinstance(error, ActivationError):
            raise error
        if isinstance(error, CapabilityImplementationError):
            raise ActivationError(str(error)) from error
        raise ActivationError(
            f"failed to stage provider '{provider_id}'",
        ) from error

    async def _prepare_promotion(
        self,
        *,
        operation_id: UUID,
        candidate: CapabilityPromotionCandidate,
        bundle: CapabilityBundle,
        staged: Mapping[str, ActivatedContribution],
    ) -> _PromotionTransaction:
        previous_snapshot = self._current
        previous_release = self._stable_releases.get(bundle.provider_id)
        previous_rollback = self._rollback_points.get(bundle.provider_id)
        previous = {
            capability_id: contribution
            for capability_id, contribution in (
                self._current.capabilities.items()
            )
            if contribution.descriptor.provider_id == bundle.provider_id
        }
        capabilities = {
            capability_id: contribution
            for capability_id, contribution in (
                self._current.capabilities.items()
            )
            if contribution.descriptor.provider_id != bundle.provider_id
        }
        capabilities.update(staged)
        generation = previous_snapshot.generation + 1
        snapshot = RegistrySnapshot(
            registry_epoch_id=self._registry_epoch_id,
            generation=generation,
            capabilities=MappingProxyType(capabilities),
        )
        release = CapabilityReleaseTag.create(
            provider_id=bundle.provider_id,
            provider_kind=bundle.provider_kind,
            version=bundle.version,
            promoted_generation=generation,
            descriptors=tuple(
                contribution.descriptor
                for contribution in staged.values()
            ),
        )
        scenario_evidence: Sequence[CapabilityPromotionEvidence] = ()
        if self._promotion_scenario_runner is not None:
            scenario_evidence = await self._promotion_scenario_runner.run(
                candidate,
                release,
                {
                    capability_id: contribution.implementation
                    for capability_id, contribution in staged.items()
                },
                {
                    capability_id: contribution.descriptor
                    for capability_id, contribution in staged.items()
                },
            )
        assessment = await self._evaluate_release(
            operation_id=operation_id,
            candidate=candidate,
            release=release,
            previous_snapshot=previous_snapshot,
            previous_release=previous_release,
            scenario_evidence=scenario_evidence,
        )
        return _PromotionTransaction(
            operation_id=operation_id,
            candidate=candidate,
            evaluation=assessment.evaluation,
            evidence_bundle=assessment.evidence_bundle,
            previous_snapshot=previous_snapshot,
            previous_release=previous_release,
            previous_rollback=previous_rollback,
            previous_contributions=MappingProxyType(previous),
            snapshot=snapshot,
            release=release,
        )

    async def _evaluate_release(
        self,
        *,
        operation_id: UUID,
        candidate: CapabilityPromotionCandidate,
        release: CapabilityReleaseTag,
        previous_snapshot: RegistrySnapshot,
        previous_release: CapabilityReleaseTag | None,
        scenario_evidence: Sequence[CapabilityPromotionEvidence],
    ) -> CapabilityPromotionAssessment:
        try:
            assessment = await self._promotion_gate.evaluate(
                candidate,
                release,
                scenario_evidence,
            )
            if not isinstance(
                assessment,
                CapabilityPromotionAssessment,
            ):
                raise TypeError(
                    "promotion gate must return an assessment",
                )
            included_evidence = {
                item.evidence_id
                for item in assessment.evidence_bundle.evidence
            }
            required_evidence = {
                item.evidence_id for item in scenario_evidence
            }
            if not required_evidence <= included_evidence:
                raise TypeError(
                    "promotion gate omitted scenario evidence",
                )
        except Exception as exc:
            assessment = rejected_contract_assessment(
                candidate,
                check_id="evaluation.error",
                additional_evidence=tuple(scenario_evidence),
            )
            try:
                await self._persist_promotion_evidence(
                    assessment.evidence_bundle,
                )
            except Exception as evidence_error:
                raise ActivationError(
                    f"promotion evidence failed for "
                    f"'{candidate.provider_id}'",
                ) from evidence_error
            await self._append_rejected_evaluation(
                operation_id=operation_id,
                candidate=candidate,
                evaluation=assessment.evaluation,
                previous_snapshot=previous_snapshot,
                previous_release=previous_release,
                reason_code="evaluation.error",
            )
            raise ActivationError(
                f"promotion evaluation failed for "
                f"'{candidate.provider_id}'",
            ) from exc
        evaluation = assessment.evaluation
        if evaluation.decision is not CapabilityEvaluationDecision.ALLOW:
            try:
                await self._persist_promotion_evidence(
                    assessment.evidence_bundle,
                )
            except Exception as exc:
                raise ActivationError(
                    f"promotion evidence failed for "
                    f"'{candidate.provider_id}'",
                ) from exc
            await self._append_rejected_evaluation(
                operation_id=operation_id,
                candidate=candidate,
                evaluation=evaluation,
                previous_snapshot=previous_snapshot,
                previous_release=previous_release,
                reason_code="evaluation.not_allowed",
            )
            raise ActivationError(
                f"promotion evaluation did not allow "
                f"'{candidate.provider_id}'",
            )
        return assessment

    async def _append_rejected_evaluation(
        self,
        *,
        operation_id: UUID,
        candidate: CapabilityPromotionCandidate,
        evaluation: CapabilityPromotionEvaluation,
        previous_snapshot: RegistrySnapshot,
        previous_release: CapabilityReleaseTag | None,
        reason_code: str,
    ) -> None:
        rejected = CapabilityPromotionEvent.create(
            operation_id=operation_id,
            registry_epoch_id=self._registry_epoch_id,
            action=CapabilityPromotionAction.PROMOTE,
            phase=CapabilityPromotionPhase.REJECTED,
            candidate=candidate,
            evaluation=evaluation,
            from_generation=previous_snapshot.generation,
            target_generation=None,
            previous_release_hash=(
                previous_release.release_hash
                if previous_release is not None
                else None
            ),
            target_release=None,
            reason_code=reason_code,
        )
        try:
            await self._append_promotion_event(rejected)
        except Exception as exc:
            raise ActivationError(
                f"promotion rejection journal failed for "
                f"'{candidate.provider_id}'",
            ) from exc

    def _transaction_event(
        self,
        transaction: _PromotionTransaction,
        phase: CapabilityPromotionPhase,
        *,
        reason_code: str | None = None,
    ) -> CapabilityPromotionEvent:
        previous_release = transaction.previous_release
        return CapabilityPromotionEvent.create(
            operation_id=transaction.operation_id,
            registry_epoch_id=self._registry_epoch_id,
            action=CapabilityPromotionAction.PROMOTE,
            phase=phase,
            candidate=transaction.candidate,
            evaluation=transaction.evaluation,
            from_generation=transaction.previous_snapshot.generation,
            target_generation=transaction.snapshot.generation,
            previous_release_hash=(
                previous_release.release_hash
                if previous_release is not None
                else None
            ),
            target_release=transaction.release,
            reason_code=reason_code,
        )

    async def _commit_promotion(
        self,
        transaction: _PromotionTransaction,
    ) -> RegistrySnapshot:
        provider_id = transaction.candidate.provider_id
        try:
            await self._persist_promotion_evidence(
                transaction.evidence_bundle,
            )
        except Exception as exc:
            raise ActivationError(
                f"promotion evidence failed for '{provider_id}'",
            ) from exc
        prepared = self._transaction_event(
            transaction,
            CapabilityPromotionPhase.PREPARED,
        )
        try:
            await self._append_promotion_event(prepared)
        except Exception as exc:
            raise ActivationError(
                f"promotion journal prepare failed for '{provider_id}'",
            ) from exc

        self._rollback_points[provider_id] = _ProviderRollbackPoint(
            promoted_release_hash=transaction.release.release_hash,
            contributions=transaction.previous_contributions,
        )
        self._stable_releases[provider_id] = transaction.release
        self._current = transaction.snapshot
        self._snapshots[transaction.snapshot.generation] = (
            transaction.snapshot
        )
        self._leases[transaction.snapshot.generation] = 0
        committed = self._transaction_event(
            transaction,
            CapabilityPromotionPhase.COMMITTED,
        )
        try:
            await self._append_promotion_event(committed)
        except Exception as exc:
            self._restore_promotion(transaction)
            await self._journal_aborted_promotion(transaction)
            raise ActivationError(
                f"promotion journal commit failed for '{provider_id}'",
            ) from exc
        self._reclaim_unleased()
        return transaction.snapshot

    def _restore_promotion(
        self,
        transaction: _PromotionTransaction,
    ) -> None:
        provider_id = transaction.candidate.provider_id
        self._current = transaction.previous_snapshot
        self._snapshots.pop(transaction.snapshot.generation, None)
        self._leases.pop(transaction.snapshot.generation, None)
        if transaction.previous_release is None:
            self._stable_releases.pop(provider_id, None)
        else:
            self._stable_releases[provider_id] = (
                transaction.previous_release
            )
        if transaction.previous_rollback is None:
            self._rollback_points.pop(provider_id, None)
        else:
            self._rollback_points[provider_id] = (
                transaction.previous_rollback
            )

    async def _journal_aborted_promotion(
        self,
        transaction: _PromotionTransaction,
    ) -> None:
        aborted = self._transaction_event(
            transaction,
            CapabilityPromotionPhase.ABORTED,
            reason_code="journal.commit_failed",
        )
        try:
            await self._append_promotion_event(aborted)
        except Exception:  # pylint: disable=broad-except
            logger.exception(
                "failed to journal aborted capability promotion: %s",
                transaction.candidate.provider_id,
            )

    async def rollback_provider(
        self,
        provider_id: str,
        *,
        expected_release_hash: str,
    ) -> RegistrySnapshot:
        """Rollback one provider without reverting unrelated promotions."""
        async with self._lock:
            transaction = self._prepare_rollback(
                provider_id,
                expected_release_hash=expected_release_hash,
            )
            return await self._commit_rollback(transaction)

    def _prepare_rollback(
        self,
        provider_id: str,
        *,
        expected_release_hash: str,
    ) -> _RollbackTransaction:
        current_release = self._stable_releases.get(provider_id)
        point = self._rollback_points.get(provider_id)
        if current_release is None or point is None:
            raise ReleaseRollbackError(
                f"provider '{provider_id}' has no rollback point",
            )
        if current_release.release_hash != expected_release_hash:
            raise ReleaseRollbackError(
                f"provider '{provider_id}' stable release changed",
            )
        if point.promoted_release_hash != expected_release_hash:
            raise ReleaseRollbackError(
                f"provider '{provider_id}' rollback point is stale",
            )
        capabilities = {
            capability_id: contribution
            for capability_id, contribution in (
                self._current.capabilities.items()
            )
            if contribution.descriptor.provider_id != provider_id
        }
        capabilities.update(point.contributions)
        generation = self._current.generation + 1
        snapshot = RegistrySnapshot(
            registry_epoch_id=self._registry_epoch_id,
            generation=generation,
            capabilities=MappingProxyType(capabilities),
        )
        descriptors = tuple(
            contribution.descriptor
            for contribution in point.contributions.values()
        )
        target_release = self._rollback_target_release(
            current_release,
            descriptors,
            generation,
        )
        candidate = CapabilityPromotionCandidate.create(
            provider_id=provider_id,
            provider_kind=current_release.provider_kind,
            version=(
                target_release.version
                if target_release is not None
                else current_release.version
            ),
            bundle_payload={
                "action": CapabilityPromotionAction.ROLLBACK.value,
                "provider_id": provider_id,
                "target_release": (
                    target_release.model_dump(mode="json")
                    if target_release is not None
                    else None
                ),
            },
        )
        assessment = build_capability_promotion_assessment(
            candidate,
            target_release,
            evaluator_id="qwenpaw.rollback-gate",
            decision=CapabilityEvaluationDecision.ALLOW,
            checks=(
                (
                    "rollback.release-fence",
                    CapabilityCheckOutcome.PASSED,
                ),
            ),
        )
        return _RollbackTransaction(
            operation_id=uuid4(),
            candidate=candidate,
            evaluation=assessment.evaluation,
            evidence_bundle=assessment.evidence_bundle,
            previous_snapshot=self._current,
            previous_release=current_release,
            rollback_point=point,
            snapshot=snapshot,
            target_release=target_release,
        )

    @staticmethod
    def _rollback_target_release(
        current_release: CapabilityReleaseTag,
        descriptors: tuple[CapabilityDescriptor, ...],
        generation: int,
    ) -> CapabilityReleaseTag | None:
        if not descriptors:
            return None
        descriptor = descriptors[0]
        return CapabilityReleaseTag.create(
            provider_id=current_release.provider_id,
            provider_kind=descriptor.provider_kind,
            version=descriptor.version,
            promoted_generation=generation,
            descriptors=descriptors,
        )

    def _rollback_event(
        self,
        transaction: _RollbackTransaction,
        phase: CapabilityPromotionPhase,
        *,
        reason_code: str | None = None,
    ) -> CapabilityPromotionEvent:
        return CapabilityPromotionEvent.create(
            operation_id=transaction.operation_id,
            registry_epoch_id=self._registry_epoch_id,
            action=CapabilityPromotionAction.ROLLBACK,
            phase=phase,
            candidate=transaction.candidate,
            evaluation=transaction.evaluation,
            from_generation=transaction.previous_snapshot.generation,
            target_generation=transaction.snapshot.generation,
            previous_release_hash=(
                transaction.previous_release.release_hash
            ),
            target_release=transaction.target_release,
            reason_code=reason_code,
        )

    async def _commit_rollback(
        self,
        transaction: _RollbackTransaction,
    ) -> RegistrySnapshot:
        provider_id = transaction.candidate.provider_id
        try:
            await self._persist_promotion_evidence(
                transaction.evidence_bundle,
            )
        except Exception as exc:
            raise ReleaseRollbackError(
                f"rollback evidence failed for '{provider_id}'",
            ) from exc
        try:
            await self._append_promotion_event(
                self._rollback_event(
                    transaction,
                    CapabilityPromotionPhase.PREPARED,
                ),
            )
        except Exception as exc:
            raise ReleaseRollbackError(
                f"rollback journal prepare failed for '{provider_id}'",
            ) from exc
        self._current = transaction.snapshot
        self._snapshots[transaction.snapshot.generation] = (
            transaction.snapshot
        )
        self._leases[transaction.snapshot.generation] = 0
        if transaction.target_release is None:
            self._stable_releases.pop(provider_id, None)
        else:
            self._stable_releases[provider_id] = (
                transaction.target_release
            )
        self._rollback_points.pop(provider_id, None)
        try:
            await self._append_promotion_event(
                self._rollback_event(
                    transaction,
                    CapabilityPromotionPhase.COMMITTED,
                ),
            )
        except Exception as exc:
            self._restore_rollback(transaction)
            await self._journal_aborted_rollback(transaction)
            raise ReleaseRollbackError(
                f"rollback journal commit failed for '{provider_id}'",
            ) from exc
        self._reclaim_unleased()
        return transaction.snapshot

    def _restore_rollback(
        self,
        transaction: _RollbackTransaction,
    ) -> None:
        provider_id = transaction.candidate.provider_id
        self._current = transaction.previous_snapshot
        self._snapshots.pop(transaction.snapshot.generation, None)
        self._leases.pop(transaction.snapshot.generation, None)
        self._stable_releases[provider_id] = transaction.previous_release
        self._rollback_points[provider_id] = transaction.rollback_point

    async def _journal_aborted_rollback(
        self,
        transaction: _RollbackTransaction,
    ) -> None:
        try:
            await self._append_promotion_event(
                self._rollback_event(
                    transaction,
                    CapabilityPromotionPhase.ABORTED,
                    reason_code="journal.commit_failed",
                ),
            )
        except Exception:  # pylint: disable=broad-except
            logger.exception(
                "failed to journal aborted capability rollback: %s",
                transaction.candidate.provider_id,
            )

    async def release(self, generation: int) -> None:
        """Release one lease and reclaim obsolete unreferenced snapshots."""
        async with self._lock:
            count = self._leases.get(generation)
            if count is None or count < 1:
                return
            self._leases[generation] = count - 1
            self._reclaim_unleased()

    async def deactivate_provider(
        self,
        provider_id: str,
    ) -> RegistrySnapshot:
        """Atomically remove one provider from all future leases."""
        async with self._lock:
            transaction = self._prepare_deactivation(provider_id)
            if transaction is None:
                return self._current
            return await self._commit_deactivation(transaction)

    def _prepare_deactivation(
        self,
        provider_id: str,
    ) -> _DeactivationTransaction | None:
        contributions = {
            capability_id: contribution
            for capability_id, contribution in (
                self._current.capabilities.items()
            )
            if contribution.descriptor.provider_id == provider_id
        }
        if not contributions:
            return None
        capabilities = {
            capability_id: contribution
            for capability_id, contribution in (
                self._current.capabilities.items()
            )
            if contribution.descriptor.provider_id != provider_id
        }
        previous_release = self._stable_releases.get(provider_id)
        descriptor = next(iter(contributions.values())).descriptor
        candidate = CapabilityPromotionCandidate.create(
            provider_id=provider_id,
            provider_kind=descriptor.provider_kind,
            version=(
                previous_release.version
                if previous_release is not None
                else descriptor.version
            ),
            bundle_payload={
                "action": CapabilityPromotionAction.DEACTIVATE.value,
                "provider_id": provider_id,
                "previous_release_hash": (
                    previous_release.release_hash
                    if previous_release is not None
                    else None
                ),
                "capability_ids": sorted(contributions),
            },
        )
        assessment = build_capability_promotion_assessment(
            candidate,
            previous_release,
            evaluator_id="qwenpaw.deactivation-contract",
            decision=CapabilityEvaluationDecision.ALLOW,
            checks=(
                (
                    "deactivate.provider-present",
                    CapabilityCheckOutcome.PASSED,
                ),
            ),
        )
        generation = self._current.generation + 1
        snapshot = RegistrySnapshot(
            registry_epoch_id=self._registry_epoch_id,
            generation=generation,
            capabilities=MappingProxyType(capabilities),
        )
        return _DeactivationTransaction(
            operation_id=uuid4(),
            candidate=candidate,
            evaluation=assessment.evaluation,
            evidence_bundle=assessment.evidence_bundle,
            previous_snapshot=self._current,
            previous_release=previous_release,
            previous_rollback=self._rollback_points.get(provider_id),
            snapshot=snapshot,
        )

    def _deactivation_event(
        self,
        transaction: _DeactivationTransaction,
        phase: CapabilityPromotionPhase,
        *,
        reason_code: str | None = None,
    ) -> CapabilityPromotionEvent:
        previous_release = transaction.previous_release
        return CapabilityPromotionEvent.create(
            operation_id=transaction.operation_id,
            registry_epoch_id=self._registry_epoch_id,
            action=CapabilityPromotionAction.DEACTIVATE,
            phase=phase,
            candidate=transaction.candidate,
            evaluation=transaction.evaluation,
            from_generation=transaction.previous_snapshot.generation,
            target_generation=transaction.snapshot.generation,
            previous_release_hash=(
                previous_release.release_hash
                if previous_release is not None
                else None
            ),
            target_release=None,
            reason_code=reason_code,
        )

    async def _commit_deactivation(
        self,
        transaction: _DeactivationTransaction,
    ) -> RegistrySnapshot:
        provider_id = transaction.candidate.provider_id
        try:
            await self._persist_promotion_evidence(
                transaction.evidence_bundle,
            )
        except Exception as exc:
            raise ProviderDeactivationError(
                f"deactivation evidence failed for '{provider_id}'",
            ) from exc
        try:
            await self._append_promotion_event(
                self._deactivation_event(
                    transaction,
                    CapabilityPromotionPhase.PREPARED,
                ),
            )
        except Exception as exc:
            raise ProviderDeactivationError(
                f"deactivation journal prepare failed for '{provider_id}'",
            ) from exc
        self._current = transaction.snapshot
        self._stable_releases.pop(provider_id, None)
        self._rollback_points.pop(provider_id, None)
        self._snapshots[transaction.snapshot.generation] = (
            transaction.snapshot
        )
        self._leases[transaction.snapshot.generation] = 0
        try:
            await self._append_promotion_event(
                self._deactivation_event(
                    transaction,
                    CapabilityPromotionPhase.COMMITTED,
                ),
            )
        except Exception as exc:
            self._restore_deactivation(transaction)
            await self._journal_aborted_deactivation(transaction)
            raise ProviderDeactivationError(
                f"deactivation journal commit failed for '{provider_id}'",
            ) from exc
        self._reclaim_unleased()
        return transaction.snapshot

    def _restore_deactivation(
        self,
        transaction: _DeactivationTransaction,
    ) -> None:
        provider_id = transaction.candidate.provider_id
        self._current = transaction.previous_snapshot
        self._snapshots.pop(transaction.snapshot.generation, None)
        self._leases.pop(transaction.snapshot.generation, None)
        if transaction.previous_release is not None:
            self._stable_releases[provider_id] = (
                transaction.previous_release
            )
        if transaction.previous_rollback is not None:
            self._rollback_points[provider_id] = (
                transaction.previous_rollback
            )

    async def _journal_aborted_deactivation(
        self,
        transaction: _DeactivationTransaction,
    ) -> None:
        try:
            await self._append_promotion_event(
                self._deactivation_event(
                    transaction,
                    CapabilityPromotionPhase.ABORTED,
                    reason_code="journal.commit_failed",
                ),
            )
        except Exception:  # pylint: disable=broad-except
            logger.exception(
                "failed to journal aborted provider deactivation: %s",
                transaction.candidate.provider_id,
            )

    def defer_provider_cleanup(
        self,
        provider_id: str,
        callback: Callable[[], None],
    ) -> None:
        """Run cleanup after no retained generation uses the provider."""
        if not self._provider_is_retained(provider_id):
            callback()
            return
        self._provider_cleanup.setdefault(provider_id, []).append(callback)

    def _reclaim_unleased(self) -> None:
        current = self._current.generation
        reclaimable = [
            generation
            for generation, count in self._leases.items()
            if generation != current and count == 0
        ]
        for generation in reclaimable:
            self._leases.pop(generation, None)
            self._snapshots.pop(generation, None)
        self._run_ready_provider_cleanup()

    def _provider_is_retained(self, provider_id: str) -> bool:
        return any(
            contribution.descriptor.provider_id == provider_id
            for snapshot in self._snapshots.values()
            for contribution in snapshot.capabilities.values()
        )

    def _run_ready_provider_cleanup(self) -> None:
        ready = [
            provider_id
            for provider_id in self._provider_cleanup
            if not self._provider_is_retained(provider_id)
        ]
        for provider_id in ready:
            callbacks = self._provider_cleanup.pop(provider_id, [])
            for callback in callbacks:
                try:
                    callback()
                except Exception:  # pylint: disable=broad-except
                    logger.exception(
                        "provider cleanup failed: %s",
                        provider_id,
                    )

    def retained_generations(self) -> tuple[int, ...]:
        """Return retained generations for structured diagnostics."""
        return tuple(sorted(self._snapshots))


__all__ = [
    "ActivatedContribution",
    "ActivationError",
    "ContributionFactory",
    "GenerationLease",
    "GenerationRegistry",
    "ProviderDeactivationError",
    "ReleaseRollbackError",
    "RegistrySnapshot",
]
