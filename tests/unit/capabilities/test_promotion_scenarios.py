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
from qwenpaw.capabilities.system_memory import (
    SYSTEM_MEMORY_CAPABILITY_BUNDLE,
    system_memory_contribution_factory,
)
from qwenpaw.capabilities.system_drivers import (
    SYSTEM_DRIVER_CAPABILITY_BUNDLE,
    system_driver_contribution_factory,
)
from qwenpaw.capabilities.promotions import (
    LiteCapabilityPromotionScenarioRunner,
)
from qwenpaw.scheduling import HostSchedulerProvider
from qwenpaw.tasks.runner import LocalAgentRunner
from qwenpaw.tasks.system_contributions import (
    SYSTEM_CAPABILITY_BUNDLE,
    system_contribution_factory,
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
    CostAccountingMode,
    DriverApprovalRequest,
    DriverToolDefinition,
    DeliveryMode,
    MemoryStateScope,
    PromptFragment,
    RunnerPreflightResult,
    RunnerSignal,
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


class _MemorySession:
    def __init__(self, prompt: str) -> None:
        self._prompt = prompt
        self.closed = False

    def get_prompt(self) -> str:
        return self._prompt

    def list_tools(self):
        return (
            ToolDefinition(
                function=_catalog_tool,
                name="_catalog_tool",
                tool_type="internal",
            ),
        )

    async def close(self) -> None:
        self.closed = True


class _MemoryProvider:
    provider_id = "example.memory.provider"

    def __init__(self, prompt: str = "Remember the stable fixture.") -> None:
        self._prompt = prompt
        self.session: _MemorySession | None = None

    async def health_check(self) -> bool:
        return True

    async def open(self, scope, host):
        assert scope.selection.memory_provider_id == self.provider_id
        assert host.config_snapshot() == {}
        assert not hasattr(host, "compatibility_backend")
        state = host.state(MemoryStateScope.AGENT)
        assert await state.read("probe") is None
        snapshot = await state.write(
            "probe",
            "isolated",
            expected_revision=0,
        )
        assert snapshot.revision == 1
        await state.delete("probe", expected_revision=1)
        self.session = _MemorySession(self._prompt)
        return self.session


async def _driver_invoke(payload):
    return payload


class _DriverSession:
    def __init__(
        self,
        provider_id: str,
        *,
        foreign_tool: bool = False,
    ) -> None:
        self.provider_id = provider_id
        self._foreign_tool = foreign_tool
        self.closed = False

    def list_tools(self):
        return (
            DriverToolDefinition(
                provider_id=(
                    "foreign.driver"
                    if self._foreign_tool
                    else self.provider_id
                ),
                capability_id="driver://promotion/tools/read#invoke",
                name="promotion_driver_read",
                invoke=_driver_invoke,
            ),
        )

    def prompt_fragments(self):
        return (
            PromptFragment(
                fragment_id=f"{self.provider_id}.policy",
                content="Promotion scenario policy.",
            ),
        )

    async def close(self) -> None:
        self.closed = True


class _DriverProvider:
    provider_id = "example.driver.provider"

    def __init__(
        self,
        *,
        foreign_tool: bool = False,
        approval_on_open: bool = False,
    ) -> None:
        self._foreign_tool = foreign_tool
        self._approval_on_open = approval_on_open
        self.session: _DriverSession | None = None

    async def health_check(self) -> bool:
        return True

    async def open(self, scope, host):
        assert scope.selection.driver_provider_id == self.provider_id
        assert host.config_snapshot() == {}
        assert host.credential("service") is None
        assert not hasattr(host, "load")
        if self._approval_on_open:
            await host.require_approval(
                DriverApprovalRequest(
                    provider_id=self.provider_id,
                    capability_id="driver://promotion/open#invoke",
                    tool_name="promotion_open",
                ),
            )
        self.session = _DriverSession(
            self.provider_id,
            foreign_tool=self._foreign_tool,
        )
        return self.session


class _DeliveryAdapter:
    adapter_id = "example.delivery.adapter"

    def __init__(self, *, accept_foreign: bool = False) -> None:
        self._accept_foreign = accept_foreign
        self.deliver_called = False

    async def health_check(self) -> bool:
        return True

    def supports(self, request) -> bool:
        if self._accept_foreign:
            return True
        return (
            request.destination.adapter_id == self.adapter_id
            and request.destination.address == "local"
            and request.mode is DeliveryMode.FINAL
        )

    async def deliver(self, request, *, attempt):
        del request, attempt
        self.deliver_called = True
        raise AssertionError("promotion scenario must not call deliver")


class _InvalidSchedulerProvider:
    provider_id = "example.scheduler.provider"

    async def health_check(self) -> bool:
        return True

    async def open(self, host):
        del host
        return object()


async def _runner_execution(order, run, context):
    del order, run, context
    yield RunnerSignal(event_type="example.runner.completed")


class _MismatchedPreflightRunner:
    runner_id = "example.runner.adapter"
    cost_accounting = CostAccountingMode.ZERO

    async def preflight(self, request):
        return RunnerPreflightResult(
            runner_id="example.runner.foreign",
            slot=request.slot,
            registry_generation=request.registry_generation,
            contextual=False,
            cost_accounting=self.cost_accounting,
        )

    async def execute(self, order, run):
        del order, run
        yield RunnerSignal(event_type="example.runner.unreachable")


class _LegacyRunner:
    runner_id = "example.runner.adapter"

    async def execute(self, order, run):
        del order, run
        yield RunnerSignal(event_type="example.runner.unreachable")


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


def _memory_bundle() -> CapabilityBundle:
    return CapabilityBundle(
        provider_id="example.memory",
        provider_kind=CapabilityProviderKind.PLUGIN,
        version="1.0.0",
        contributions=(
            CapabilityContribution(
                contribution_id="provider",
                slot="memory.provider",
                entrypoint="example:memory",
            ),
        ),
    )


def _driver_bundle() -> CapabilityBundle:
    return CapabilityBundle(
        provider_id="example.driver",
        provider_kind=CapabilityProviderKind.PLUGIN,
        version="1.0.0",
        contributions=(
            CapabilityContribution(
                contribution_id="provider",
                slot="driver.provider",
                entrypoint="example:driver",
            ),
        ),
    )


def _delivery_bundle(
    addresses: list[str] | None = None,
) -> CapabilityBundle:
    metadata = (
        {"delivery_addresses": addresses}
        if addresses is not None
        else {}
    )
    return CapabilityBundle(
        provider_id="example.delivery",
        provider_kind=CapabilityProviderKind.PLUGIN,
        version="1.0.0",
        contributions=(
            CapabilityContribution(
                contribution_id="adapter",
                slot="delivery.adapter",
                entrypoint="example:delivery",
                metadata=metadata,
            ),
        ),
    )


def _scheduler_bundle() -> CapabilityBundle:
    return CapabilityBundle(
        provider_id="example.scheduler",
        provider_kind=CapabilityProviderKind.PLUGIN,
        version="1.0.0",
        contributions=(
            CapabilityContribution(
                contribution_id="provider",
                slot="scheduler.provider",
                entrypoint="example:scheduler",
            ),
        ),
    )


def _runner_bundle(slot: str = "runner") -> CapabilityBundle:
    return CapabilityBundle(
        provider_id="example.runner",
        provider_kind=CapabilityProviderKind.PLUGIN,
        version="1.0.0",
        contributions=(
            CapabilityContribution(
                contribution_id="adapter",
                slot=slot,
                entrypoint="example:runner",
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


@pytest.mark.asyncio
async def test_plugin_memory_provider_session_scenario_passes() -> None:
    bundle = _memory_bundle()
    provider = _MemoryProvider()
    registry = GenerationRegistry(
        promotion_scenario_runner=LiteCapabilityPromotionScenarioRunner(),
    )

    await registry.activate_bundle(
        bundle,
        lambda _declaration: provider,
    )
    [evidence_bundle] = await registry.promotion_evidence(
        _candidate(bundle).candidate_id,
    )
    scenario = next(
        item
        for item in evidence_bundle.evidence
        if item.check_id.startswith("scenario.memory-provider.session")
    )

    assert scenario.outcome is CapabilityCheckOutcome.PASSED
    assert provider.session is not None
    assert provider.session.closed


@pytest.mark.asyncio
async def test_oversized_memory_prompt_blocks_publication_and_closes() -> None:
    bundle = _memory_bundle()
    provider = _MemoryProvider("x" * (32 * 1024 + 1))
    registry = GenerationRegistry(
        promotion_scenario_runner=LiteCapabilityPromotionScenarioRunner(),
    )

    with pytest.raises(ActivationError, match="did not allow"):
        await registry.activate_bundle(
            bundle,
            lambda _declaration: provider,
        )

    assert registry.generation == 1
    assert provider.session is not None
    assert provider.session.closed


@pytest.mark.asyncio
async def test_system_memory_provider_uses_same_session_scenario() -> None:
    registry = GenerationRegistry(
        promotion_scenario_runner=LiteCapabilityPromotionScenarioRunner(),
    )

    await registry.activate_bundle(
        SYSTEM_MEMORY_CAPABILITY_BUNDLE,
        system_memory_contribution_factory,
    )

    lease = await registry.pin()
    assert lease.resolve("qwenpaw.system.memory.workspace-memory") is not None
    await lease.close()


@pytest.mark.asyncio
async def test_plugin_driver_provider_catalog_scenario_passes() -> None:
    bundle = _driver_bundle()
    provider = _DriverProvider()
    registry = GenerationRegistry(
        promotion_scenario_runner=LiteCapabilityPromotionScenarioRunner(),
    )

    await registry.activate_bundle(
        bundle,
        lambda _declaration: provider,
    )
    [evidence_bundle] = await registry.promotion_evidence(
        _candidate(bundle).candidate_id,
    )
    scenario = next(
        item
        for item in evidence_bundle.evidence
        if item.check_id.startswith("scenario.driver-provider.catalog")
    )

    assert scenario.outcome is CapabilityCheckOutcome.PASSED
    assert provider.session is not None
    assert provider.session.closed


@pytest.mark.asyncio
async def test_foreign_driver_tool_blocks_publication_and_closes() -> None:
    bundle = _driver_bundle()
    provider = _DriverProvider(foreign_tool=True)
    registry = GenerationRegistry(
        promotion_scenario_runner=LiteCapabilityPromotionScenarioRunner(),
    )

    with pytest.raises(ActivationError, match="did not allow"):
        await registry.activate_bundle(
            bundle,
            lambda _declaration: provider,
        )

    assert registry.generation == 1
    assert provider.session is not None
    assert provider.session.closed


@pytest.mark.asyncio
async def test_driver_approval_during_open_blocks_publication() -> None:
    bundle = _driver_bundle()
    provider = _DriverProvider(approval_on_open=True)
    registry = GenerationRegistry(
        promotion_scenario_runner=LiteCapabilityPromotionScenarioRunner(),
    )

    with pytest.raises(ActivationError, match="did not allow"):
        await registry.activate_bundle(
            bundle,
            lambda _declaration: provider,
        )

    assert registry.generation == 1
    assert provider.session is None


@pytest.mark.asyncio
async def test_system_driver_provider_uses_same_catalog_scenario() -> None:
    registry = GenerationRegistry(
        promotion_scenario_runner=LiteCapabilityPromotionScenarioRunner(),
    )

    await registry.activate_bundle(
        SYSTEM_DRIVER_CAPABILITY_BUNDLE,
        system_driver_contribution_factory,
    )

    lease = await registry.pin()
    assert lease.resolve("qwenpaw.system.drivers.workspace-driver") is not None
    await lease.close()


@pytest.mark.asyncio
async def test_delivery_adapter_routing_scenario_passes_without_delivery(
) -> None:
    bundle = _delivery_bundle(["local"])
    adapter = _DeliveryAdapter()
    registry = GenerationRegistry(
        promotion_scenario_runner=LiteCapabilityPromotionScenarioRunner(),
    )

    await registry.activate_bundle(
        bundle,
        lambda _declaration: adapter,
    )
    [evidence_bundle] = await registry.promotion_evidence(
        _candidate(bundle).candidate_id,
    )
    scenario = next(
        item
        for item in evidence_bundle.evidence
        if item.check_id.startswith("scenario.delivery-adapter.routing")
    )

    assert scenario.outcome is CapabilityCheckOutcome.PASSED
    assert not adapter.deliver_called


@pytest.mark.asyncio
async def test_delivery_adapter_accepting_foreign_identity_is_rejected(
) -> None:
    bundle = _delivery_bundle(["local"])
    registry = GenerationRegistry(
        promotion_scenario_runner=LiteCapabilityPromotionScenarioRunner(),
    )

    with pytest.raises(ActivationError, match="did not allow"):
        await registry.activate_bundle(
            bundle,
            lambda _declaration: _DeliveryAdapter(
                accept_foreign=True,
            ),
        )

    assert registry.generation == 1


@pytest.mark.asyncio
async def test_delivery_adapter_without_route_hints_is_not_applicable(
) -> None:
    bundle = _delivery_bundle()
    registry = GenerationRegistry(
        promotion_scenario_runner=LiteCapabilityPromotionScenarioRunner(),
    )

    await registry.activate_bundle(
        bundle,
        lambda _declaration: _DeliveryAdapter(),
    )
    [evidence_bundle] = await registry.promotion_evidence(
        _candidate(bundle).candidate_id,
    )
    scenario = next(
        item
        for item in evidence_bundle.evidence
        if item.check_id.startswith("scenario.delivery-adapter.routing")
    )

    assert scenario.outcome is CapabilityCheckOutcome.NOT_APPLICABLE


@pytest.mark.asyncio
async def test_host_scheduler_provider_catalog_scenario_passes() -> None:
    bundle = _scheduler_bundle()
    registry = GenerationRegistry(
        promotion_scenario_runner=LiteCapabilityPromotionScenarioRunner(),
    )

    await registry.activate_bundle(
        bundle,
        lambda _declaration: HostSchedulerProvider(
            "example.scheduler.provider",
        ),
    )
    [evidence_bundle] = await registry.promotion_evidence(
        _candidate(bundle).candidate_id,
    )
    scenario = next(
        item
        for item in evidence_bundle.evidence
        if item.check_id.startswith("scenario.scheduler-provider.catalog")
    )

    assert scenario.outcome is CapabilityCheckOutcome.PASSED


@pytest.mark.asyncio
async def test_scheduler_provider_returning_foreign_store_is_rejected(
) -> None:
    bundle = _scheduler_bundle()
    registry = GenerationRegistry(
        promotion_scenario_runner=LiteCapabilityPromotionScenarioRunner(),
    )

    with pytest.raises(ActivationError, match="did not allow"):
        await registry.activate_bundle(
            bundle,
            lambda _declaration: _InvalidSchedulerProvider(),
        )

    assert registry.generation == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("slot", ["runner", "harness.runner"])
async def test_runner_preflight_scenario_passes(slot: str) -> None:
    bundle = _runner_bundle(slot)
    registry = GenerationRegistry(
        promotion_scenario_runner=LiteCapabilityPromotionScenarioRunner(),
    )

    await registry.activate_bundle(
        bundle,
        lambda _declaration: LocalAgentRunner(
            "example.runner.adapter",
            execute_context=_runner_execution,
            cost_accounting=CostAccountingMode.ZERO,
        ),
    )
    [evidence_bundle] = await registry.promotion_evidence(
        _candidate(bundle).candidate_id,
    )
    scenario = next(
        item
        for item in evidence_bundle.evidence
        if item.check_id.startswith("scenario.runner.preflight")
    )

    assert scenario.outcome is CapabilityCheckOutcome.PASSED


@pytest.mark.asyncio
async def test_runner_preflight_with_mismatched_identity_is_rejected() -> None:
    bundle = _runner_bundle()
    registry = GenerationRegistry(
        promotion_scenario_runner=LiteCapabilityPromotionScenarioRunner(),
    )

    with pytest.raises(ActivationError, match="did not allow"):
        await registry.activate_bundle(
            bundle,
            lambda _declaration: _MismatchedPreflightRunner(),
        )

    assert registry.generation == 1


@pytest.mark.asyncio
async def test_legacy_runner_preflight_is_not_applicable() -> None:
    bundle = _runner_bundle()
    registry = GenerationRegistry(
        promotion_scenario_runner=LiteCapabilityPromotionScenarioRunner(),
    )

    await registry.activate_bundle(
        bundle,
        lambda _declaration: _LegacyRunner(),
    )
    [evidence_bundle] = await registry.promotion_evidence(
        _candidate(bundle).candidate_id,
    )
    scenario = next(
        item
        for item in evidence_bundle.evidence
        if item.check_id.startswith("scenario.runner.preflight")
    )

    assert scenario.outcome is CapabilityCheckOutcome.NOT_APPLICABLE


@pytest.mark.asyncio
async def test_system_runners_pass_preflight_without_resolving_workspace(
) -> None:
    workspace_resolved = False

    async def resolve_workspace(_agent_id):
        nonlocal workspace_resolved
        workspace_resolved = True
        raise AssertionError("preflight must not resolve a Workspace")

    registry = GenerationRegistry(
        promotion_scenario_runner=LiteCapabilityPromotionScenarioRunner(),
    )

    await registry.activate_bundle(
        SYSTEM_CAPABILITY_BUNDLE,
        system_contribution_factory(resolve_workspace),
    )
    [evidence_bundle] = await registry.promotion_evidence(
        _candidate(SYSTEM_CAPABILITY_BUNDLE).candidate_id,
    )
    runner_scenarios = tuple(
        item
        for item in evidence_bundle.evidence
        if item.check_id.startswith("scenario.runner.preflight")
    )

    assert len(runner_scenarios) == 3
    assert all(
        item.outcome is CapabilityCheckOutcome.PASSED
        for item in runner_scenarios
    )
    assert workspace_resolved is False
