# -*- coding: utf-8 -*-
"""Tests for the provider-neutral capability registry."""

import pytest
from pydantic import ValidationError

from qwenpaw.capabilities import (
    ActivationError,
    GenerationRegistry,
    ReleaseRollbackError,
)
from qwenpaw.kernel import CapabilityReleaseTag
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


def _factory_for(provider_id: str):
    def factory(
        declaration: CapabilityContribution,
    ) -> _HealthyFactory:
        return _HealthyFactory(
            f"{provider_id}.{declaration.contribution_id}",
        )

    return factory


_factory = _factory_for("qwenpaw.system.test")


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


def _bundle(
    provider_id: str = "qwenpaw.system.test",
    version: str = "1.0.0",
) -> CapabilityBundle:
    return CapabilityBundle(
        provider_id=provider_id,
        provider_kind=CapabilityProviderKind.SYSTEM,
        version=version,
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
    original_epoch = registry.registry_epoch_id

    replacement = _bundle().model_copy(update={"version": "2.0.0"})
    await registry.activate_bundle(replacement, _factory)

    assert old_lease.resolve("qwenpaw.system.test.factory").version == "1.0.0"
    assert old_lease.registry_epoch_id == original_epoch
    assert registry.registry_epoch_id == original_epoch
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
async def test_activation_assigns_a_self_verifying_stable_release() -> None:
    registry = GenerationRegistry()

    snapshot = await registry.activate_bundle(_bundle(), _factory)
    release = registry.stable_release("qwenpaw.system.test")

    assert release is not None
    assert release.channel == "stable"
    assert release.version == "1.0.0"
    assert release.promoted_generation == snapshot.generation
    assert release.capability_ids == ("qwenpaw.system.test.factory",)
    assert release.releases[0].slot == "agent.factory"
    payload = release.model_dump(mode="json")
    payload["release_hash"] = f"sha256:{'0' * 64}"
    with pytest.raises(ValidationError, match="release tag hash"):
        CapabilityReleaseTag.model_validate(payload)


@pytest.mark.asyncio
async def test_provider_rollback_preserves_unrelated_promotions() -> None:
    registry = GenerationRegistry()
    await registry.activate_bundle(_bundle(), _factory)
    first_release = registry.stable_release("qwenpaw.system.test")
    assert first_release is not None

    await registry.activate_bundle(_bundle(version="2.0.0"), _factory)
    replaced_release = registry.stable_release("qwenpaw.system.test")
    assert replaced_release is not None
    replaced_lease = await registry.pin()

    other_id = "qwenpaw.system.other"
    await registry.activate_bundle(
        _bundle(provider_id=other_id),
        _factory_for(other_id),
    )
    rolled_back = await registry.rollback_provider(
        "qwenpaw.system.test",
        expected_release_hash=replaced_release.release_hash,
    )
    current = await registry.pin()

    assert rolled_back.generation == 5
    assert current.resolve("qwenpaw.system.test.factory").version == "1.0.0"
    assert current.resolve("qwenpaw.system.other.factory") is not None
    assert (
        replaced_lease.resolve("qwenpaw.system.test.factory").version
        == "2.0.0"
    )
    restored_release = registry.stable_release("qwenpaw.system.test")
    assert restored_release is not None
    assert restored_release.release_hash == first_release.release_hash
    assert restored_release.promoted_generation == 5
    await current.close()
    await replaced_lease.close()


@pytest.mark.asyncio
async def test_provider_rollback_requires_current_release_fence() -> None:
    registry = GenerationRegistry()
    await registry.activate_bundle(_bundle(), _factory)
    await registry.activate_bundle(_bundle(version="2.0.0"), _factory)
    old_release = registry.stable_release("qwenpaw.system.test")
    assert old_release is not None
    await registry.activate_bundle(_bundle(version="3.0.0"), _factory)

    with pytest.raises(ReleaseRollbackError, match="stable release changed"):
        await registry.rollback_provider(
            "qwenpaw.system.test",
            expected_release_hash=old_release.release_hash,
        )

    current = await registry.pin()
    assert current.resolve("qwenpaw.system.test.factory").version == "3.0.0"
    await current.close()


@pytest.mark.asyncio
async def test_provider_release_cannot_be_rolled_back_twice() -> None:
    registry = GenerationRegistry()
    await registry.activate_bundle(_bundle(), _factory)
    await registry.activate_bundle(_bundle(version="2.0.0"), _factory)
    release = registry.stable_release("qwenpaw.system.test")
    assert release is not None
    await registry.rollback_provider(
        "qwenpaw.system.test",
        expected_release_hash=release.release_hash,
    )

    with pytest.raises(ReleaseRollbackError, match="no rollback point"):
        await registry.rollback_provider(
            "qwenpaw.system.test",
            expected_release_hash=release.release_hash,
        )


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
