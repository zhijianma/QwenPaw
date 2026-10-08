# -*- coding: utf-8 -*-
"""Durable Lite journal for capability promotion WAL phases."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import UUID

from jsonschema.exceptions import SchemaError, best_match
from jsonschema.validators import validator_for
from pydantic import JsonValue

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
    CapabilityProviderKind,
    CapabilityReleaseTag,
    CapabilitySelection,
    CapabilityDescriptor,
    DriverApprovalRejectedError,
    DriverApprovalRequest,
    InvocationScope,
    MemoryStateConflictError,
    MemoryStateScope,
    MemoryStateSnapshot,
    ToolDefinition,
    ToolSelection,
    validate_driver_session,
)
from ..kernel.ports import (
    ArtifactRenderer,
    DriverProvider,
    DriverSession,
    MemoryProvider,
    MemorySession,
    ToolProvider,
)
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
    _MAX_MEMORY_PROMPT_BYTES = 32 * 1024
    _MAX_TOOL_CATALOG_BYTES = 64 * 1024
    _MAX_TOOLS = 128
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

    @staticmethod
    def _validate_tool_parameter(
        definition: ToolDefinition,
        parameter_name: str,
    ) -> None:
        if not parameter_name:
            return
        try:
            parameters = inspect.signature(
                definition.function,
            ).parameters
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "tool governance parameter cannot be inspected",
            ) from exc
        if parameter_name in parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            return
        raise ValueError(
            "tool governance parameter is absent from its callable",
        )

    @classmethod
    def _validate_tool_catalog(
        cls,
        tools: object,
    ) -> None:
        if not isinstance(tools, Sequence) or isinstance(
            tools,
            (str, bytes, bytearray),
        ):
            raise ValueError("tool provider returned a non-sequence catalog")
        if len(tools) > cls._MAX_TOOLS:
            raise ValueError("tool provider catalog exceeds count budget")
        names: set[str] = set()
        serializable = []
        for tool in tools:
            if isinstance(tool, ToolDefinition):
                name = tool.name
                cls._validate_tool_parameter(tool, tool.target_param)
                cls._validate_tool_parameter(tool, tool.pattern_param)
                serializable.append(tool.model_dump(mode="json"))
            elif callable(tool):
                name = str(getattr(tool, "__name__", ""))
                serializable.append({"legacy_callable": name})
            else:
                raise ValueError("tool provider returned an invalid tool")
            if not name or len(name.encode("utf-8")) > 128:
                raise ValueError("tool name is empty or exceeds budget")
            if name in names:
                raise ValueError("tool provider returned duplicate names")
            names.add(name)
        encoded = json.dumps(
            serializable,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > cls._MAX_TOOL_CATALOG_BYTES:
            raise ValueError("tool provider catalog exceeds byte budget")

    @staticmethod
    def _empty_config_outcome(
        descriptor: CapabilityDescriptor | None,
    ) -> CapabilityCheckOutcome | None:
        if descriptor is None:
            return CapabilityCheckOutcome.FAILED
        schema = descriptor.config_schema
        if schema is None:
            return None
        try:
            validator_class = validator_for(schema)
            validator_class.check_schema(schema)
            validation_error = best_match(
                validator_class(schema).iter_errors({}),
            )
        except SchemaError:
            return CapabilityCheckOutcome.FAILED
        if validation_error is not None:
            return CapabilityCheckOutcome.NOT_APPLICABLE
        return None

    async def _run_tool_provider(
        self,
        implementation: object,
        capability_id: str,
        generation: int,
        descriptor: CapabilityDescriptor | None,
    ) -> CapabilityCheckOutcome:
        if not isinstance(implementation, ToolProvider):
            return CapabilityCheckOutcome.FAILED
        config_outcome = self._empty_config_outcome(descriptor)
        if config_outcome is not None:
            return config_outcome
        scope = InvocationScope(
            agent_id="promotion-scenario",
            conversation_id="promotion-scenario",
            session_id="promotion-scenario",
            root_agent_id="promotion-scenario",
            root_session_id="promotion-scenario",
            workspace_dir=".",
            registry_generation=generation,
            selection=CapabilitySelection(
                tool_provider_ids=(capability_id,),
            ),
        )
        try:
            tools = await asyncio.wait_for(
                implementation.list_tools(
                    scope,
                    ToolSelection(),
                    _PromotionScenarioToolHost(),
                ),
                timeout=self._TIMEOUT_SECONDS,
            )
            self._validate_tool_catalog(tools)
        except Exception:  # pylint: disable=broad-except
            return CapabilityCheckOutcome.FAILED
        return CapabilityCheckOutcome.PASSED

    async def _run_memory_provider(
        self,
        implementation: object,
        capability_id: str,
        generation: int,
        descriptor: CapabilityDescriptor | None,
    ) -> CapabilityCheckOutcome:
        if not isinstance(implementation, MemoryProvider):
            return CapabilityCheckOutcome.FAILED
        config_outcome = self._empty_config_outcome(descriptor)
        if config_outcome is not None:
            return config_outcome
        session: MemorySession | None = None
        failed = False
        host_type = (
            _PromotionScenarioWorkspaceMemoryHost
            if descriptor.provider_kind is CapabilityProviderKind.SYSTEM
            else _PromotionScenarioMemoryHost
        )
        try:
            session = await asyncio.wait_for(
                implementation.open(
                    _promotion_invocation_scope(
                        capability_id,
                        generation,
                    ),
                    host_type(capability_id),
                ),
                timeout=self._TIMEOUT_SECONDS,
            )
            if not isinstance(session, MemorySession):
                raise ValueError("memory provider returned an invalid session")
            prompt = session.get_prompt()
            if not isinstance(prompt, str):
                raise ValueError("memory session returned a non-string prompt")
            if len(prompt.encode("utf-8")) > self._MAX_MEMORY_PROMPT_BYTES:
                raise ValueError("memory prompt exceeds scenario budget")
            self._validate_tool_catalog(session.list_tools())
        except Exception:  # pylint: disable=broad-except
            failed = True
        finally:
            if session is not None:
                try:
                    await asyncio.wait_for(
                        session.close(),
                        timeout=self._TIMEOUT_SECONDS,
                    )
                except Exception:  # pylint: disable=broad-except
                    failed = True
        return (
            CapabilityCheckOutcome.FAILED
            if failed
            else CapabilityCheckOutcome.PASSED
        )

    async def _run_driver_provider(
        self,
        implementation: object,
        capability_id: str,
        generation: int,
        descriptor: CapabilityDescriptor | None,
    ) -> CapabilityCheckOutcome:
        if not isinstance(implementation, DriverProvider):
            return CapabilityCheckOutcome.FAILED
        config_outcome = self._empty_config_outcome(descriptor)
        if config_outcome is not None:
            return config_outcome
        session: DriverSession | None = None
        failed = False
        host_type = (
            _PromotionScenarioWorkspaceDriverHost
            if descriptor.provider_kind is CapabilityProviderKind.SYSTEM
            else _PromotionScenarioDriverHost
        )
        try:
            session = await asyncio.wait_for(
                implementation.open(
                    _promotion_driver_invocation_scope(
                        capability_id,
                        generation,
                    ),
                    host_type(),
                ),
                timeout=self._TIMEOUT_SECONDS,
            )
            if not isinstance(session, DriverSession):
                raise ValueError("driver provider returned an invalid session")
            validate_driver_session(session, capability_id)
        except Exception:  # pylint: disable=broad-except
            failed = True
        finally:
            if session is not None:
                try:
                    await asyncio.wait_for(
                        session.close(),
                        timeout=self._TIMEOUT_SECONDS,
                    )
                except Exception:  # pylint: disable=broad-except
                    failed = True
        return (
            CapabilityCheckOutcome.FAILED
            if failed
            else CapabilityCheckOutcome.PASSED
        )

    async def run(
        self,
        candidate: CapabilityPromotionCandidate,
        release: CapabilityReleaseTag,
        implementations: Mapping[str, object],
        descriptors: Mapping[str, CapabilityDescriptor],
    ) -> Sequence[CapabilityPromotionEvidence]:
        """Return content-safe evidence for every supported Slot scenario."""
        evidence = []
        for item in release.releases:
            contract = slot_contract(item.slot)
            descriptor = descriptors.get(item.capability_id)
            for scenario_id in contract.promotion_scenarios:
                if descriptor is None or descriptor.slot != item.slot:
                    outcome = CapabilityCheckOutcome.FAILED
                elif scenario_id == "artifact-renderer.roundtrip":
                    outcome = await self._run_renderer(
                        implementations.get(item.capability_id),
                    )
                elif scenario_id == "tool-provider.catalog":
                    outcome = await self._run_tool_provider(
                        implementations.get(item.capability_id),
                        item.capability_id,
                        release.promoted_generation,
                        descriptor,
                    )
                elif scenario_id == "memory-provider.session":
                    outcome = await self._run_memory_provider(
                        implementations.get(item.capability_id),
                        item.capability_id,
                        release.promoted_generation,
                        descriptor,
                    )
                elif scenario_id == "driver-provider.catalog":
                    outcome = await self._run_driver_provider(
                        implementations.get(item.capability_id),
                        item.capability_id,
                        release.promoted_generation,
                        descriptor,
                    )
                else:
                    outcome = CapabilityCheckOutcome.FAILED
                evidence.append(
                    CapabilityPromotionEvidence.create(
                        candidate=candidate,
                        check_id=(
                            f"scenario.{scenario_id}."
                            f"{item.capability_id}"
                        ),
                        producer_id="qwenpaw.lite-scenario-runner",
                        outcome=outcome,
                        capability_ids=(item.capability_id,),
                    ),
                )
        return tuple(evidence)


async def _promotion_scenario_tool() -> None:
    """Represent one inert host tool during catalog discovery."""


def _promotion_invocation_scope(
    capability_id: str,
    generation: int,
) -> InvocationScope:
    """Create one deterministic, non-production invocation fixture."""
    return InvocationScope(
        agent_id="promotion-scenario",
        conversation_id="promotion-scenario",
        session_id="promotion-scenario",
        root_agent_id="promotion-scenario",
        root_session_id="promotion-scenario",
        workspace_dir=".",
        registry_generation=generation,
        selection=CapabilitySelection(
            memory_provider_id=capability_id,
        ),
    )


def _promotion_driver_invocation_scope(
    capability_id: str,
    generation: int,
) -> InvocationScope:
    """Create an isolated Driver catalog-discovery fixture."""
    return InvocationScope(
        agent_id="promotion-scenario",
        conversation_id="promotion-scenario",
        session_id="promotion-scenario",
        root_agent_id="promotion-scenario",
        root_session_id="promotion-scenario",
        workspace_dir=".",
        registry_generation=generation,
        selection=CapabilitySelection(
            driver_provider_id=capability_id,
        ),
    )


class _PromotionScenarioToolHost:
    """Expose inert invocation services to staged Tool Providers."""

    def config_snapshot(self) -> dict:
        """Return an empty detached configuration snapshot."""
        return {}

    def credential(self, alias: str) -> None:
        """Deny credential access during promotion scenarios."""
        del alias

    def interaction_broker(self) -> None:
        """Disable user interaction during promotion scenarios."""
        return None

    async def list_workspace_tools(
        self,
        selection: ToolSelection,
    ) -> tuple[ToolDefinition, ...]:
        """Support the system compatibility Provider without real I/O."""
        del selection
        return (
            ToolDefinition(
                function=_promotion_scenario_tool,
                name="_promotion_scenario_tool",
                tool_type="internal",
            ),
        )


class _PromotionScenarioMemoryStateStore:
    """Keep promotion-only Memory state detached from filesystem state."""

    def __init__(
        self,
        provider_id: str,
        scope: MemoryStateScope,
    ) -> None:
        self._provider_id = provider_id
        self._scope = scope
        self._values: dict[str, MemoryStateSnapshot] = {}

    async def read(self, key: str) -> MemoryStateSnapshot | None:
        """Read one promotion-only value."""
        return self._values.get(key)

    async def write(
        self,
        key: str,
        value: JsonValue,
        *,
        expected_revision: int,
    ) -> MemoryStateSnapshot:
        """Apply optimistic revision checks without persistent I/O."""
        current = self._values.get(key)
        current_revision = current.revision if current is not None else 0
        if expected_revision != current_revision:
            raise MemoryStateConflictError("memory revision mismatch")
        snapshot = MemoryStateSnapshot(
            provider_id=self._provider_id,
            scope=self._scope,
            owner_id="promotion-scenario",
            key=key,
            value=value,
            revision=current_revision + 1,
        )
        self._values[key] = snapshot
        return snapshot

    async def delete(
        self,
        key: str,
        *,
        expected_revision: int,
    ) -> None:
        """Delete only the observed promotion-only revision."""
        current = self._values.get(key)
        if current is None or current.revision != expected_revision:
            raise MemoryStateConflictError("memory revision mismatch")
        self._values.pop(key)


class _PromotionScenarioMemoryHost:
    """Expose only detached config and in-memory state to a Provider."""

    def __init__(self, provider_id: str) -> None:
        self._stores = {
            scope: _PromotionScenarioMemoryStateStore(provider_id, scope)
            for scope in MemoryStateScope
        }

    def config_snapshot(self) -> dict:
        """Return empty non-secret config for the scenario fixture."""
        return {}

    def state(
        self,
        scope: MemoryStateScope,
    ) -> _PromotionScenarioMemoryStateStore:
        """Return a provider-owned process-local state namespace."""
        return self._stores[scope]


class _PromotionScenarioWorkspaceMemoryHost(
    _PromotionScenarioMemoryHost,
):
    """Add only the built-in provider's compatibility surface."""

    def compatibility_backend(self) -> None:
        """Let the built-in adapter bind an inert empty backend."""
        return None


class _PromotionScenarioDriverHost:
    """Expose no credentials or approval side effects during promotion."""

    async def require_approval(
        self,
        request: DriverApprovalRequest,
    ) -> None:
        """Reject approval attempts outside a real Invocation."""
        raise DriverApprovalRejectedError(
            request.capability_id,
            "promotion scenarios cannot request approval",
        )

    def config_snapshot(self) -> dict:
        """Return empty non-secret config for catalog discovery."""
        return {}

    def credential(self, alias: str) -> None:
        """Deny credential access during promotion scenarios."""
        del alias


class _PromotionScenarioWorkspaceDriverHost(
    _PromotionScenarioDriverHost,
):
    """Add only the built-in Provider's inert compatibility loader."""

    async def load(self) -> tuple[tuple[()], tuple[()]]:
        """Return no real Workspace Driver tools or prompt fragments."""
        return (), ()


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
