# -*- coding: utf-8 -*-
"""Behavioral promotion scenarios over staged capability implementations."""

from __future__ import annotations

import asyncio

import pytest

from qwenpaw.capabilities import ActivationError, GenerationRegistry
from qwenpaw.capabilities.system_tools import (
    SYSTEM_TOOL_CAPABILITY_BUNDLE,
    system_tool_contribution_factory,
)
from qwenpaw.capabilities.promotions import (
    LiteCapabilityPromotionScenarioRunner,
)
from qwenpaw.kernel import (
    ArtifactRenderDisposition,
    ArtifactRenderResult,
    CapabilityBundle,
    CapabilityCheckOutcome,
    CapabilityContribution,
    CapabilityDescriptor,
    CapabilityPromotionCandidate,
    CapabilityProviderKind,
    CapabilityPromotionAssessment,
    ToolDefinition,
)


class _OmittingGate:
    async def evaluate(
        self,
        candidate,
        release,
        scenario_evidence=(),
    ):
        del release, scenario_evidence
        return CapabilityPromotionAssessment.create(
            candidate=candidate,
            evaluator_id="example.omitting-gate",
            decision="allow",
            checks=(("contract.custom", "passed"),),
        )


class _Renderer:
    priority = 100

    def __init__(
        self,
        renderer_id: str,
        *,
        supported: bool = True,
        mismatched_identity: bool = False,
    ) -> None:
        self.renderer_id = renderer_id
        self._supported = supported
        self._mismatched_identity = mismatched_identity

    async def health_check(self) -> bool:
        return True

    def supports(self, artifact, disposition) -> bool:
        del artifact
        return (
            self._supported
            and disposition is ArtifactRenderDisposition.INLINE
        )

    async def render(self, request):
        return ArtifactRenderResult(
            renderer_id=(
                "example.renderers.wrong"
                if self._mismatched_identity
                else self.renderer_id
            ),
            content=request.content,
            media_type=request.artifact.media_type,
            filename=request.filename,
            disposition=request.disposition,
            source_content_hash=request.artifact.content_hash,
        )


async def _catalog_tool(target: str = "") -> str:
    return target


class _ToolProvider:
    provider_id = "example.tools.provider"

    def __init__(
        self,
        *,
        duplicate: bool = False,
        invalid_target: bool = False,
    ) -> None:
        self._duplicate = duplicate
        self._invalid_target = invalid_target

    async def health_check(self) -> bool:
        return True

    async def list_tools(self, scope, selection, host):
        del scope, selection
        assert host.config_snapshot() == {}
        assert host.credential("service") is None
        assert host.interaction_broker() is None
        definition = ToolDefinition(
            function=_catalog_tool,
            name="_catalog_tool",
            tool_type="internal",
            target_param=(
                "missing_target" if self._invalid_target else "target"
            ),
        )
        return (definition, definition) if self._duplicate else (definition,)


class _SlowToolProvider(_ToolProvider):
    async def list_tools(self, scope, selection, host):
        await asyncio.sleep(0.05)
        return await super().list_tools(scope, selection, host)


def _bundle(
    provider_kind: CapabilityProviderKind,
) -> CapabilityBundle:
    return CapabilityBundle(
        provider_id="example.renderers",
        provider_kind=provider_kind,
        version="1.0.0",
        contributions=(
            CapabilityContribution(
                contribution_id="preview",
                slot="artifact.renderer",
                entrypoint="example:renderer",
            ),
        ),
    )


def _candidate(bundle: CapabilityBundle) -> CapabilityPromotionCandidate:
    return CapabilityPromotionCandidate.create(
        provider_id=bundle.provider_id,
        provider_kind=bundle.provider_kind,
        version=bundle.version,
        bundle_payload=bundle.model_dump(mode="json"),
    )


def _tool_bundle(
    config_schema: dict | None = None,
) -> CapabilityBundle:
    return CapabilityBundle(
        provider_id="example.tools",
        provider_kind=CapabilityProviderKind.PLUGIN,
        version="1.0.0",
        contributions=(
            CapabilityContribution(
                contribution_id="provider",
                slot="tool.provider",
                entrypoint="example:tools",
                config_schema=config_schema,
            ),
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_kind",
    [CapabilityProviderKind.SYSTEM, CapabilityProviderKind.PLUGIN],
)
async def test_system_and_plugin_renderers_share_behavioral_gate(
    provider_kind: CapabilityProviderKind,
) -> None:
    bundle = _bundle(provider_kind)
    registry = GenerationRegistry(
        promotion_scenario_runner=(
            LiteCapabilityPromotionScenarioRunner()
        ),
    )

    snapshot = await registry.activate_bundle(
        bundle,
        lambda _declaration: _Renderer("example.renderers.preview"),
    )
    [evidence_bundle] = await registry.promotion_evidence(
        _candidate(bundle).candidate_id,
    )
    [scenario] = [
        item
        for item in evidence_bundle.evidence
        if item.check_id.startswith("scenario.")
    ]

    assert snapshot.generation == 2
    assert scenario.outcome is CapabilityCheckOutcome.PASSED
    assert scenario.capability_ids == ("example.renderers.preview",)


@pytest.mark.asyncio
async def test_failed_renderer_scenario_prevents_publication() -> None:
    bundle = _bundle(CapabilityProviderKind.PLUGIN)
    registry = GenerationRegistry(
        promotion_scenario_runner=(
            LiteCapabilityPromotionScenarioRunner()
        ),
    )

    with pytest.raises(ActivationError, match="did not allow"):
        await registry.activate_bundle(
            bundle,
            lambda _declaration: _Renderer(
                "example.renderers.preview",
                mismatched_identity=True,
            ),
        )

    assert registry.generation == 1
    assert registry.stable_release("example.renderers") is None
    [evidence_bundle] = await registry.promotion_evidence(
        _candidate(bundle).candidate_id,
    )
    assert any(
        item.outcome is CapabilityCheckOutcome.FAILED
        for item in evidence_bundle.evidence
        if item.check_id.startswith("scenario.")
    )


@pytest.mark.asyncio
async def test_unsupported_fixture_is_not_reported_as_scenario_pass() -> None:
    bundle = _bundle(CapabilityProviderKind.PLUGIN)
    candidate = _candidate(bundle)
    release_registry = GenerationRegistry()
    snapshot = await release_registry.activate_bundle(
        bundle,
        lambda _declaration: _Renderer(
            "example.renderers.preview",
            supported=False,
        ),
    )
    release = release_registry.stable_release("example.renderers")
    assert snapshot.generation == 2
    assert release is not None
    [evidence] = await LiteCapabilityPromotionScenarioRunner().run(
        candidate,
        release,
        {
            "example.renderers.preview": _Renderer(
                "example.renderers.preview",
                supported=False,
            ),
        },
        {
            "example.renderers.preview": CapabilityDescriptor(
                capability_id="example.renderers.preview",
                slot="artifact.renderer",
                provider_id="example.renderers",
                provider_kind=CapabilityProviderKind.PLUGIN,
                version="1.0.0",
            ),
        },
    )

    assert evidence.outcome is CapabilityCheckOutcome.NOT_APPLICABLE


@pytest.mark.asyncio
async def test_missing_scenario_descriptor_is_failed() -> None:
    bundle = _bundle(CapabilityProviderKind.PLUGIN)
    candidate = _candidate(bundle)
    release_registry = GenerationRegistry()
    await release_registry.activate_bundle(
        bundle,
        lambda _declaration: _Renderer("example.renderers.preview"),
    )
    release = release_registry.stable_release("example.renderers")
    assert release is not None

    [evidence] = await LiteCapabilityPromotionScenarioRunner().run(
        candidate,
        release,
        {
            "example.renderers.preview": _Renderer(
                "example.renderers.preview",
            ),
        },
        {},
    )

    assert evidence.outcome is CapabilityCheckOutcome.FAILED


@pytest.mark.asyncio
async def test_gate_error_bundle_retains_completed_scenario_evidence() -> None:
    bundle = _bundle(CapabilityProviderKind.PLUGIN)
    registry = GenerationRegistry(
        promotion_gate=_OmittingGate(),
        promotion_scenario_runner=LiteCapabilityPromotionScenarioRunner(),
    )

    with pytest.raises(ActivationError, match="evaluation failed"):
        await registry.activate_bundle(
            bundle,
            lambda _declaration: _Renderer("example.renderers.preview"),
        )

    [evidence_bundle] = await registry.promotion_evidence(
        _candidate(bundle).candidate_id,
    )
    outcomes = {
        item.check_id: item.outcome
        for item in evidence_bundle.evidence
    }
    assert outcomes["evaluation.error"] is CapabilityCheckOutcome.FAILED
    assert outcomes[
        "scenario.artifact-renderer.roundtrip.example.renderers.preview"
    ] is CapabilityCheckOutcome.PASSED


@pytest.mark.asyncio
async def test_plugin_tool_provider_catalog_scenario_passes() -> None:
    bundle = _tool_bundle()
    registry = GenerationRegistry(
        promotion_scenario_runner=LiteCapabilityPromotionScenarioRunner(),
    )

    await registry.activate_bundle(
        bundle,
        lambda _declaration: _ToolProvider(),
    )
    [evidence_bundle] = await registry.promotion_evidence(
        _candidate(bundle).candidate_id,
    )
    scenario = next(
        item
        for item in evidence_bundle.evidence
        if item.check_id.startswith("scenario.tool-provider.catalog")
    )

    assert scenario.outcome is CapabilityCheckOutcome.PASSED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider",
    [
        _ToolProvider(duplicate=True),
        _ToolProvider(invalid_target=True),
    ],
)
async def test_invalid_tool_catalog_prevents_publication(
    provider: _ToolProvider,
) -> None:
    bundle = _tool_bundle()
    registry = GenerationRegistry(
        promotion_scenario_runner=LiteCapabilityPromotionScenarioRunner(),
    )

    with pytest.raises(ActivationError, match="did not allow"):
        await registry.activate_bundle(
            bundle,
            lambda _declaration: provider,
        )

    assert registry.generation == 1


@pytest.mark.asyncio
async def test_system_tool_provider_uses_same_catalog_scenario() -> None:
    registry = GenerationRegistry(
        promotion_scenario_runner=LiteCapabilityPromotionScenarioRunner(),
    )

    await registry.activate_bundle(
        SYSTEM_TOOL_CAPABILITY_BUNDLE,
        system_tool_contribution_factory,
    )
    release = registry.stable_release("qwenpaw.system")
    assert release is not None

    lease = await registry.pin()
    assert lease.resolve("qwenpaw.system.workspace-tools") is not None
    await lease.close()


@pytest.mark.asyncio
async def test_required_runtime_config_is_not_reported_as_scenario_pass(
) -> None:
    bundle = _tool_bundle(
        {
            "type": "object",
            "required": ["endpoint"],
            "properties": {"endpoint": {"type": "string"}},
        },
    )
    registry = GenerationRegistry(
        promotion_scenario_runner=LiteCapabilityPromotionScenarioRunner(),
    )

    await registry.activate_bundle(
        bundle,
        lambda _declaration: _ToolProvider(),
    )
    [evidence_bundle] = await registry.promotion_evidence(
        _candidate(bundle).candidate_id,
    )
    scenario = next(
        item
        for item in evidence_bundle.evidence
        if item.check_id.startswith("scenario.tool-provider.catalog")
    )

    assert scenario.outcome is CapabilityCheckOutcome.NOT_APPLICABLE


@pytest.mark.asyncio
async def test_invalid_tool_provider_schema_blocks_publication() -> None:
    bundle = _tool_bundle({"type": "not-a-json-schema-type"})
    registry = GenerationRegistry(
        promotion_scenario_runner=LiteCapabilityPromotionScenarioRunner(),
    )

    with pytest.raises(ActivationError, match="did not allow"):
        await registry.activate_bundle(
            bundle,
            lambda _declaration: _ToolProvider(),
        )

    assert registry.generation == 1


@pytest.mark.asyncio
async def test_tool_catalog_timeout_blocks_publication() -> None:
    bundle = _tool_bundle()
    runner = LiteCapabilityPromotionScenarioRunner()
    runner._TIMEOUT_SECONDS = 0.001  # pylint: disable=protected-access
    registry = GenerationRegistry(promotion_scenario_runner=runner)

    with pytest.raises(ActivationError, match="did not allow"):
        await registry.activate_bundle(
            bundle,
            lambda _declaration: _SlowToolProvider(),
        )

    assert registry.generation == 1
