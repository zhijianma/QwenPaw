# -*- coding: utf-8 -*-
"""Tests for atomic registry generations and hot activation."""

from collections.abc import AsyncIterator

import pytest

from qwenpaw.kernel.models import (
    CapabilityBundle,
    CapabilityContribution,
    CapabilityProviderKind,
    Run,
    RunnerSignal,
    TaskOrder,
)
from qwenpaw.plugins.architecture import PluginManifest
from qwenpaw.plugins.generations import ActivationError, GenerationRegistry


class _Runner:
    def __init__(self, runner_id: str = "task-insights.runner") -> None:
        self.runner_id = runner_id

    async def health_check(self) -> bool:
        return True

    async def execute(
        self,
        order: TaskOrder,
        run: Run,
    ) -> AsyncIterator[RunnerSignal]:
        del order, run
        yield RunnerSignal(event_type="runner.ready")


class _UnhealthyRunner(_Runner):
    async def health_check(self) -> bool:
        return False


def _factory(declaration):
    if declaration.slot == "runner":
        return _Runner()
    return {"entrypoint": declaration.entrypoint}


def _manifest(version: str = "1.0.0") -> PluginManifest:
    return PluginManifest.from_dict(
        {
            "schema_version": "qwenpaw.plugin.v2",
            "id": "task-insights",
            "version": version,
            "contributions": [
                {
                    "id": "runner",
                    "slot": "runner",
                    "entrypoint": "task_insights.runner:create",
                },
                {
                    "id": "inspector",
                    "slot": "ui.task.inspector",
                    "entrypoint": "frontend/index.js",
                },
            ],
        },
    )


@pytest.mark.asyncio
async def test_one_plugin_activates_two_slots_in_one_generation() -> None:
    registry = GenerationRegistry()

    snapshot = await registry.activate(_manifest(), _factory)
    lease = await registry.pin()

    assert snapshot.generation == 2
    assert set(snapshot.capabilities) == {
        "task-insights.runner",
        "task-insights.inspector",
    }
    assert lease.generation == 2
    assert lease.registry_epoch_id == registry.registry_epoch_id
    assert lease.resolve("task-insights.runner").slot == "runner"
    release = registry.stable_release("task-insights")
    assert release is not None
    assert release.provider_kind is CapabilityProviderKind.PLUGIN
    assert release.version == "1.0.0"
    await lease.close()


@pytest.mark.asyncio
async def test_failed_health_check_keeps_current_generation() -> None:
    registry = GenerationRegistry()
    await registry.activate(_manifest(), _factory)
    original_release = registry.stable_release("task-insights")

    with pytest.raises(ActivationError, match="health check failed"):
        await registry.activate(
            _manifest("2.0.0"),
            lambda declaration: (
                _UnhealthyRunner()
                if declaration.slot == "runner"
                else {"entrypoint": declaration.entrypoint}
            ),
        )

    assert registry.generation == 2
    assert registry.stable_release("task-insights") == original_release


@pytest.mark.asyncio
async def test_plugin_release_rolls_back_without_drifting_new_lease() -> None:
    registry = GenerationRegistry()
    await registry.activate(_manifest(), _factory)
    await registry.activate(_manifest("2.0.0"), _factory)
    promoted = registry.stable_release("task-insights")
    assert promoted is not None
    promoted_lease = await registry.pin()

    await registry.rollback_provider(
        "task-insights",
        expected_release_hash=promoted.release_hash,
    )
    current = await registry.pin()

    assert current.resolve("task-insights.runner").version == "1.0.0"
    assert promoted_lease.resolve("task-insights.runner").version == "2.0.0"
    restored = registry.stable_release("task-insights")
    assert restored is not None
    assert restored.provider_kind is CapabilityProviderKind.PLUGIN
    assert restored.version == "1.0.0"
    await current.close()
    await promoted_lease.close()


@pytest.mark.asyncio
async def test_old_generation_is_retained_until_lease_closes() -> None:
    registry = GenerationRegistry()
    await registry.activate(_manifest(), _factory)
    old_lease = await registry.pin()

    await registry.activate(_manifest("2.0.0"), _factory)

    assert registry.retained_generations() == (2, 3)
    assert old_lease.resolve("task-insights.runner").version == "1.0.0"
    await old_lease.close()
    assert registry.retained_generations() == (3,)


@pytest.mark.asyncio
async def test_system_and_plugin_bundles_share_one_generation() -> None:
    registry = GenerationRegistry()
    system = CapabilityBundle(
        provider_id="qwenpaw.system",
        provider_kind=CapabilityProviderKind.SYSTEM,
        version="1.0.0",
        contributions=(
            CapabilityContribution(
                contribution_id="console-agent",
                slot="runner",
                entrypoint="qwenpaw.tasks.system:ConsoleAgentRunner",
            ),
        ),
    )

    await registry.activate_bundle(
        system,
        lambda _: _Runner("qwenpaw.system.console-agent"),
    )
    await registry.activate(_manifest(), _factory)
    lease = await registry.pin()

    builtin = lease.resolve("qwenpaw.system.console-agent")
    plugin = lease.resolve("task-insights.runner")
    assert builtin is not None
    assert plugin is not None
    assert builtin.provider_kind is CapabilityProviderKind.SYSTEM
    assert plugin.provider_kind is CapabilityProviderKind.PLUGIN
    assert builtin.slot == plugin.slot == "runner"
    await lease.close()


@pytest.mark.asyncio
async def test_replacing_system_bundle_keeps_plugin_capabilities() -> None:
    registry = GenerationRegistry()
    system = CapabilityBundle(
        provider_id="qwenpaw.system",
        provider_kind=CapabilityProviderKind.SYSTEM,
        version="1.0.0",
        contributions=(
            CapabilityContribution(
                contribution_id="console-agent",
                slot="runner",
                entrypoint="qwenpaw.tasks.system:ConsoleAgentRunner",
            ),
        ),
    )
    await registry.activate(_manifest(), _factory)
    await registry.activate_bundle(
        system,
        lambda _: _Runner("qwenpaw.system.console-agent"),
    )

    replaced = CapabilityBundle.model_validate(
        {
            **system.model_dump(),
            "version": "2.0.0",
        },
    )
    await registry.activate_bundle(
        replaced,
        lambda _: _Runner("qwenpaw.system.console-agent"),
    )
    lease = await registry.pin()

    assert lease.resolve("qwenpaw.system.console-agent").version == "2.0.0"
    assert lease.resolve("task-insights.runner") is not None
    await lease.close()


@pytest.mark.asyncio
async def test_invalid_runner_keeps_existing_generation_available() -> None:
    registry = GenerationRegistry()
    await registry.activate(_manifest(), _factory)
    old_lease = await registry.pin()
    old_runner = old_lease.implementation("task-insights.runner")

    with pytest.raises(
        ActivationError,
        match="does not implement the 'runner' contract",
    ):
        await registry.activate(
            _manifest("2.0.0"),
            lambda declaration: (
                object()
                if declaration.slot == "runner"
                else {"entrypoint": declaration.entrypoint}
            ),
        )

    current_lease = await registry.pin()
    assert registry.generation == 2
    assert current_lease.implementation("task-insights.runner") is old_runner
    await current_lease.close()
    await old_lease.close()


@pytest.mark.asyncio
async def test_mismatched_runner_identity_rejects_activation() -> None:
    class WrongRunner(_Runner):
        def __init__(self) -> None:
            super().__init__("another-plugin.runner")

    registry = GenerationRegistry()

    with pytest.raises(ActivationError, match="declares runner_id"):
        await registry.activate(
            _manifest(),
            lambda declaration: (
                WrongRunner()
                if declaration.slot == "runner"
                else {"entrypoint": declaration.entrypoint}
            ),
        )

    assert registry.generation == 1
