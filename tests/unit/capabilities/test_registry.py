# -*- coding: utf-8 -*-
"""Tests for the provider-neutral capability registry."""

import pytest

from qwenpaw.capabilities import ActivationError, GenerationRegistry
from qwenpaw.kernel.models import (
    CapabilityBundle,
    CapabilityContribution,
    CapabilityProviderKind,
)


class _HealthyFactory:
    def __init__(self, factory_id: str) -> None:
        self.factory_id = factory_id

    async def health_check(self) -> bool:
        return True

    async def build(self, context, app_services):
        return context, app_services


def _factory(declaration: CapabilityContribution) -> _HealthyFactory:
    return _HealthyFactory(
        f"qwenpaw.system.test.{declaration.contribution_id}",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_kind",
    [CapabilityProviderKind.SYSTEM, CapabilityProviderKind.PLUGIN],
)
async def test_all_provider_kinds_share_the_slot_contract_gate(
    provider_kind: CapabilityProviderKind,
) -> None:
    registry = GenerationRegistry()
    bundle = _bundle().model_copy(
        update={"provider_kind": provider_kind},
    )

    with pytest.raises(
        ActivationError,
        match="does not implement the 'agent.factory' contract",
    ):
        await registry.activate_bundle(bundle, lambda _: object())

    assert registry.generation == 1


def _bundle() -> CapabilityBundle:
    return CapabilityBundle(
        provider_id="qwenpaw.system.test",
        provider_kind=CapabilityProviderKind.SYSTEM,
        version="1.0.0",
        contributions=(
            CapabilityContribution(
                contribution_id="factory",
                slot="agent.factory",
                entrypoint="tests:factory",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_ensure_bundle_is_idempotent() -> None:
    registry = GenerationRegistry()

    first = await registry.ensure_bundle(_bundle(), _factory)
    second = await registry.ensure_bundle(_bundle(), _factory)

    assert first.generation == 2
    assert second.generation == 2
    assert registry.generation == 2


@pytest.mark.asyncio
async def test_ensure_bundle_removes_stale_provider_contributions() -> None:
    registry = GenerationRegistry()
    bundle = _bundle()
    expanded = bundle.model_copy(
        update={
            "contributions": bundle.contributions
            + (
                CapabilityContribution(
                    contribution_id="stale",
                    slot="agent.factory",
                    entrypoint="tests:stale",
                ),
            ),
        },
    )
    await registry.activate_bundle(expanded, _factory)

    snapshot = await registry.ensure_bundle(bundle, _factory)
    lease = await registry.pin()

    assert snapshot.generation == 3
    assert lease.resolve("qwenpaw.system.test.stale") is None
    await lease.close()


@pytest.mark.asyncio
async def test_old_invocation_keeps_its_generation_during_activation() -> None:
    registry = GenerationRegistry()
    await registry.ensure_bundle(_bundle(), _factory)
    old_lease = await registry.pin()

    replacement = _bundle().model_copy(update={"version": "2.0.0"})
    await registry.activate_bundle(replacement, _factory)

    assert old_lease.resolve("qwenpaw.system.test.factory").version == "1.0.0"
    assert (
        registry.current_descriptor(
            "qwenpaw.system.test.factory",
        ).version
        == "2.0.0"
    )
    await old_lease.close()
    assert registry.retained_generations() == (3,)


@pytest.mark.asyncio
async def test_provider_cleanup_waits_for_old_generation_lease() -> None:
    registry = GenerationRegistry()
    await registry.activate_bundle(_bundle(), _factory)
    old_lease = await registry.pin()
    cleaned = []

    await registry.deactivate_provider("qwenpaw.system.test")
    registry.defer_provider_cleanup(
        "qwenpaw.system.test",
        lambda: cleaned.append(True),
    )

    assert not cleaned
    await old_lease.close()
    assert cleaned == [True]


@pytest.mark.asyncio
async def test_provider_cleanup_runs_immediately_without_old_lease() -> None:
    registry = GenerationRegistry()
    await registry.activate_bundle(_bundle(), _factory)
    await registry.deactivate_provider("qwenpaw.system.test")
    cleaned = []

    registry.defer_provider_cleanup(
        "qwenpaw.system.test",
        lambda: cleaned.append(True),
    )

    assert cleaned == [True]
